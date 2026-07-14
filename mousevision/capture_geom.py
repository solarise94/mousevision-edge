"""Shared capture geometry helpers (mirrored in ui/static/mobile.js).

Canvas recording crops the camera source to a fixed 9:16 buffer (720x1280).
These helpers keep the Python tests aligned with the browser drawImage crop.
"""

from __future__ import annotations

import json


def center_crop_source_rect(
    src_w: int | float,
    src_h: int | float,
    dst_w: int | float = 720,
    dst_h: int | float = 1280,
) -> dict[str, float]:
    """Return the centered source rectangle that covers ``dst_w:dst_h``.

    Same semantics as CSS object-fit:cover into a destination of size
    (dst_w, dst_h): the source is cropped so the destination aspect is filled
    without letterboxing. Returns ``{sx, sy, sw, sh}`` in source pixels.
    """
    sw0 = float(src_w)
    sh0 = float(src_h)
    if sw0 <= 0 or sh0 <= 0:
        raise ValueError("source dimensions must be positive")
    dw = float(dst_w)
    dh = float(dst_h)
    if dw <= 0 or dh <= 0:
        raise ValueError("destination dimensions must be positive")
    src_aspect = sw0 / sh0
    dst_aspect = dw / dh
    if src_aspect > dst_aspect:
        # Source wider than destination: crop left/right.
        sh = sh0
        sw = sh0 * dst_aspect
        sx = (sw0 - sw) / 2.0
        sy = 0.0
    else:
        # Source taller (or equal): crop top/bottom.
        sw = sw0
        sh = sw0 / dst_aspect
        sx = 0.0
        sy = (sh0 - sh) / 2.0
    return {"sx": sx, "sy": sy, "sw": sw, "sh": sh}


# Fixed guide boxes as fractions of the 720x1280 canvas (match mobile.css).
# mouse-guide: top 6%, height 48%, left/right 7%
# weight-guide: top 64%, height 25%, left/right 20%
GUIDE_MOUSE = {"x": 0.07, "y": 0.06, "w": 0.86, "h": 0.48}
GUIDE_WEIGHT = {"x": 0.20, "y": 0.64, "w": 0.60, "h": 0.25}

CANVAS_WIDTH = 720
CANVAS_HEIGHT = 1280
CANVAS_ASPECT = CANVAS_WIDTH / CANVAS_HEIGHT  # 9:16
# Tight tolerance: ~1% of aspect (~7px at 720 width). Larger drift means the
# upload is not a true canvas capture and must not be stretched into ROI space.
CANVAS_ASPECT_TOLERANCE = 0.01


def is_near_canvas_aspect(
    width: int | float,
    height: int | float,
    *,
    tolerance: float = CANVAS_ASPECT_TOLERANCE,
) -> bool:
    """True when decoded frame aspect is within ``tolerance`` of 9:16 portrait."""
    w = float(width)
    h = float(height)
    if w <= 0 or h <= 0:
        return False
    aspect = w / h
    return abs(aspect - CANVAS_ASPECT) <= tolerance


def parse_capture_meta(raw: object) -> dict[str, object] | None:
    """Decode ``capture_meta`` JSON; require canvas_width/height == 720x1280."""
    if not raw:
        return None
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    try:
        cw = int(obj.get("canvas_width"))
        ch = int(obj.get("canvas_height"))
    except (TypeError, ValueError):
        return None
    if cw != CANVAS_WIDTH or ch != CANVAS_HEIGHT:
        return None
    return obj


def validate_canvas_video_geometry(
    width: int | float,
    height: int | float,
    *,
    capture_meta: object = None,
) -> None:
    """Raise ``ValueError`` when a claimed canvas upload is not 9:16 portrait.

    Requires valid structured capture_meta with canvas_width/height 720x1280,
    and a decoded frame aspect near 9:16. Callers should not stretch arbitrary
    landscape clips into the reference geometry.
    """
    meta = parse_capture_meta(capture_meta)
    if meta is None:
        raise ValueError(
            "Canvas 录像元数据无效或缺少 720×1280 声明，请用网页重新录制"
        )
    if not is_near_canvas_aspect(width, height):
        raise ValueError(
            f"Canvas 录像尺寸异常（{int(width)}×{int(height)}，期望约 9:16），请重录"
        )


def guide_pixel_roi(
    guide: dict[str, float],
    *,
    frame_w: int = 720,
    frame_h: int = 1280,
) -> tuple[int, int, int, int]:
    """Convert a normalized guide box to inclusive pixel (x, y, w, h)."""
    x = int(round(float(guide["x"]) * frame_w))
    y = int(round(float(guide["y"]) * frame_h))
    w = int(round(float(guide["w"]) * frame_w))
    h = int(round(float(guide["h"]) * frame_h))
    x = max(0, min(x, frame_w))
    y = max(0, min(y, frame_h))
    w = max(0, min(w, frame_w - x))
    h = max(0, min(h, frame_h - y))
    return x, y, w, h
