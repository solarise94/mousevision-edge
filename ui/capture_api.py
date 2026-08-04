"""Raw BLE scale capture endpoint (for offline engine tuning).

Receives the raw K797 reading stream recorded by the phone
(``POST /api/scale-capture``) and persists it verbatim as a JSON file under
``output/scale_captures/``. The capture is meant for offline replay into the
weighing engine (``ui/static/weigh-engine.js``) so stability thresholds can be
tuned against real scale behaviour.

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

from fastapi import APIRouter, Depends, Form, HTTPException
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


@router.post("/api/scale-capture", dependencies=[Depends(require_api_token)])
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
