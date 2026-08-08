"""Public data-sharing endpoint (local edition opt-in upload).

The local edition normally keeps all weighing data on-device. When the user
opts in ("共享数据以改善应用"), the device POSTs the same records (including
photos + video) here so we can improve the recognition models.

This endpoint is deliberately ISOLATED from the lab data path:
- it requires a separate token (``MOUSEVISION_SHARE_TOKEN``) rather than the
  lab ``MOUSEVISION_API_TOKEN``;
- data lands under ``<output_root>/shared/`` (a distinct area from the lab
  runs under ``<output_root>/``);
- it does NOT write to the registry / records_meta / upload_queue singletons,
  so shared data never appears in lab analysis pages. It is meant for later
  manual/scripted offline analysis.

The persistence logic is shared with ``/api/records/report`` via
``report_api.persist_report_records``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from ui.report_api import persist_report_records

router = APIRouter()

_output_root: Path = Path(".")


def configure(output_root: str | Path) -> None:
    """Hand over the base output root; shared data lands in <root>/shared."""
    global _output_root
    _output_root = Path(output_root)


def share_token() -> str:
    return os.getenv("MOUSEVISION_SHARE_TOKEN", "").strip()


def require_share_token(
    x_mousevision_token: str | None = None,
) -> None:
    """Public share gate: must match the independent MOUSEVISION_SHARE_TOKEN.

    When the env var is unset the channel is disabled (reject with 403);
    a provided token that doesn't match is a 401. Styled after ui.auth's
    ``require_api_token``.
    """
    expected = share_token()
    if not expected:
        raise HTTPException(
            status_code=403,
            detail="共享通道未配置（MOUSEVISION_SHARE_TOKEN 未设置）",
        )
    if x_mousevision_token != expected:
        raise HTTPException(status_code=401, detail="无效或缺少共享令牌")


@router.post("/api/records/share")
async def share_records(
    x_mousevision_token: str | None = Header(None, alias="X-MouseVision-Token"),
    cage_id: str = Form(...),
    project_id: str = Form("default"),
    device_id: str = Form("unknown"),
    strain: str | None = Form(None),
    records: str = Form(...),
    app_version: str | None = Form(None),
    video: UploadFile | None = File(None),
    readings: UploadFile | None = File(None),
    photos: list[UploadFile] = File(default_factory=list),
) -> JSONResponse:
    """Receive an opted-in public data share (records + photos + video).

    Multipart shape mirrors ``/api/records/report`` but lands in the isolated
    ``<output_root>/shared/`` area, with ``weight_source`` forced to
    ``"public_share"`` and the manifest flagged ``shared: true``.
    """
    # 令牌校验放在最前：未配置共享通道或令牌不符 → 直接拒绝，不消费 body。
    require_share_token(x_mousevision_token)

    cage = (cage_id or "").strip()
    if not cage:
        raise HTTPException(status_code=400, detail="cage_id 不能为空")
    project = (project_id or "default").strip() or "default"
    device = (device_id or "unknown").strip() or "unknown"

    extra_manifest: dict[str, Any] = {"shared": True}
    if app_version and app_version.strip():
        extra_manifest["app_version"] = app_version.strip()

    return await persist_report_records(
        output_root=_output_root / "shared",
        registry=None,
        upload_queue=None,
        cage=cage,
        project=project,
        device=device,
        wsrc="public_share",
        strain=strain,
        records=records,
        video=video,
        readings=readings,
        photos=photos,
        mode="public_share",
        finish_status="public_share",
        extra_manifest=extra_manifest,
        log_prefix="share_api",
    )
