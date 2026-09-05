"""Raw BLE scale capture endpoint (for offline engine tuning).

Receives the raw K797 reading stream recorded by the phone
(``POST /api/scale-capture``) and persists it verbatim as a JSON file under
``output/scale_captures/``. The capture is meant for offline replay into the
weighing engine (``ui/static/weigh-engine.js``) so stability thresholds can be
tuned against real scale behaviour.

路由分类（合同 §5 / §15-B3）：``platform_tool`` —— ``scale_captures/`` 留在
全局总根，仅平台/研发使用，**不按子账号开放、不进租户目录**；鉴权保持
token 级（legacy 共享令牌可用；open mode 已在 B4 关闭）。

Endpoint contract (multipart/form-data):

  - ``payload``    JSON string, REQUIRED. Must decode to an object containing a
                   ``readings`` array. Stored verbatim (pretty-printed).
  - ``device_id``  optional device label (defaults to ``"unknown"``).
  - ``note``       optional human note stored alongside.

Response JSON: ``{ok, capture_id, path, count}``.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from ui.auth import require_api_token

log = logging.getLogger("capture_api")

router = APIRouter(tags=["scale-capture"])

# --------------------------------------------------------------------------- #
# Wiring: configure() is called once by ui/app.py to hand over the output root.
# Mirrors ui/scale_sync_api.py's configure() pattern.
# --------------------------------------------------------------------------- #
_output_root: Path = Path(".")
_captures_dir: Path = _output_root / "scale_captures"


def configure(output_root: str | Path) -> None:
    global _output_root, _captures_dir
    _output_root = Path(output_root)
    _captures_dir = _output_root / "scale_captures"


def _ensure_dir() -> Path:
    """Create and return the captures directory.

    Lazy-create at request time (not at configure()) so the directory only
    appears when the first capture actually lands.
    """
    _captures_dir.mkdir(parents=True, exist_ok=True)
    return _captures_dir


def _legacy_token_gate(
    request: Request,
    x_mousevision_token: str | None = Header(None, alias="X-MouseVision-Token"),
) -> None:
    """require_api_token + legacy deprecation 观测标记（Review S4）。

    本模块鉴权只认 X-MouseVision-Token 头上的 legacy 共享令牌（require_api_token
    语义）；通过即视为 legacy_token 通道，打 ``request.state.mv_legacy_token``
    （app 级中间件转成 X-MV-Deprecated-Token 响应头；不记录 token 本身）。
    """
    import os

    require_api_token(x_mousevision_token)
    expected = os.getenv("MOUSEVISION_API_TOKEN", "").strip()
    supplied = (x_mousevision_token or "").strip()
    if expected and supplied and secrets.compare_digest(supplied, expected):
        request.state.mv_legacy_token = True


@router.post("/api/scale-capture", dependencies=[Depends(_legacy_token_gate)])
async def receive_scale_capture(
    payload: str = Form(...),
    device_id: str = Form("unknown"),
    note: str = Form(""),
) -> JSONResponse:
    """Persist one raw scale capture session to disk.

    Returns ``{ok, capture_id, path, count}`` on success. ``count`` is the
    number of readings in the payload (0 is allowed — empty captures are still
    stored so the operator sees the upload happened).
    """
    device = (device_id or "unknown").strip() or "unknown"

    try:
        parsed: Any = json.loads(payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400, detail=f"payload 不是合法 JSON: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400, detail="payload 必须是 JSON 对象"
        )

    readings = parsed.get("readings")
    if not isinstance(readings, list):
        raise HTTPException(
            status_code=400, detail="payload.readings 必须是数组"
        )

    # Stamp provenance the phone cannot easily know (server-side receipt time),
    # then store the whole payload verbatim so it can be replayed as-is.
    parsed.setdefault("app", "h5-scale-capture")
    parsed.setdefault("device_id", device)
    parsed["received_at_epoch_ms"] = int(time.time() * 1000)
    if note:
        parsed["note"] = note

    capture_dir = _ensure_dir()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    short_id = secrets.token_hex(3)  # 6 hex chars, plenty for one folder/time
    filename = f"capture_{stamp}_{short_id}.json"
    out_path = capture_dir / filename

    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(parsed, fh, indent=2, ensure_ascii=False)

    capture_id = out_path.stem  # capture_<stamp>_<shortid>

    log.info(
        "scale_capture: stored device=%s count=%d -> %s",
        device, len(readings), out_path,
    )

    return JSONResponse(
        {
            "ok": True,
            "capture_id": capture_id,
            "path": str(out_path.relative_to(_output_root)),
            "count": len(readings),
        },
        status_code=201,
    )
