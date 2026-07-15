"""LCD OCR HTTP service — classic seven-seg on warped fixed slots."""

from __future__ import annotations

import base64
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from engine import LcdOcrEngine
from profile import load_scale_profile
from schemas import ReadResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("lcd_ocr")

engine: LcdOcrEngine | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global engine
    engine = LcdOcrEngine(scale_profile=load_scale_profile())
    engine.warmup()
    logger.info(
        "model=%s device=%s profile=%s digit_roi=%s",
        engine.model_version,
        engine.device,
        engine.scale_profile_name,
        list(engine.norm_cfg.digit_roi),
    )
    yield
    engine = None


app = FastAPI(title="MouseVision LCD OCR", version="0.2.0", lifespan=lifespan)


class LcdBoxIn(BaseModel):
    x: int
    y: int
    w: int
    h: int


class ReadJsonRequest(BaseModel):
    image_base64: str = Field(..., description="JPEG/PNG base64, optional data: URL prefix")
    lcd_box: LcdBoxIn | None = None
    quad_hint: list[list[float]] | None = None
    return_debug: bool = False
    run_audit: bool = False


class LatencyOut(BaseModel):
    locate: float = 0.0
    warp: float = 0.0
    infer: float = 0.0
    total: float = 0.0


class ReadResponse(BaseModel):
    weight: float | None
    confidence: float
    digits: list[str]
    digit_confidences: list[float] = Field(default_factory=list)
    quality: float = 0.0
    status: str = "unreadable"
    raw_text: str = ""
    locator: str | None = None
    locator_confidence: float = 0.0
    screen_quad: list[list[float]] | None = None
    lcd_box: dict[str, int] | None = None
    model_version: str = ""
    device: str = "CPU"
    latency_ms: LatencyOut | float = 0.0
    debug: dict[str, Any] | None = None


class BatchItem(BaseModel):
    image_base64: str
    lcd_box: LcdBoxIn | None = None
    quad_hint: list[list[float]] | None = None


class BatchRequest(BaseModel):
    items: list[BatchItem]
    return_debug: bool = False


class BatchResponse(BaseModel):
    results: list[ReadResponse]
    vote_weight: float | None = None
    vote_confidence: float | None = None


def _decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=400, detail="invalid image bytes")
    return img


def _decode_b64(s: str) -> np.ndarray:
    raw = s.strip()
    if "," in raw and raw.lower().startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid base64: {exc}") from exc
    return _decode_image(data)


def _parse_quad_hint(raw: str | None) -> list[list[float]] | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, list) or len(data) != 4:
        return None
    out: list[list[float]] = []
    for pt in data:
        if not isinstance(pt, (list, tuple)) or len(pt) != 2:
            return None
        out.append([float(pt[0]), float(pt[1])])
    return out


def _to_response(r: ReadResult) -> ReadResponse:
    lat = r.latency.to_dict()
    return ReadResponse(
        weight=r.weight,
        confidence=round(float(r.confidence), 4),
        digits=list(r.digits),
        digit_confidences=[round(float(c), 4) for c in r.digit_confidences],
        quality=round(float(r.quality), 4),
        status=r.status,
        raw_text=r.raw_text,
        locator=r.locator,
        locator_confidence=round(float(r.locator_confidence), 4),
        screen_quad=r.screen_quad,
        lcd_box=r.lcd_box,
        model_version=r.model_version,
        device=r.device,
        latency_ms=LatencyOut(**lat),
        debug=r.debug,
    )


def _require_engine() -> LcdOcrEngine:
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    return engine


@app.get("/health")
def health() -> dict[str, Any]:
    eng = _require_engine()
    return eng.health_stats()


@app.post("/v1/lcd/read", response_model=ReadResponse)
async def read_multipart(
    file: UploadFile | None = File(None),
    image_base64: str | None = Form(None),
    return_debug: bool = Form(False),
    run_audit: bool = Form(False),
    lcd_x: int | None = Form(None),
    lcd_y: int | None = Form(None),
    lcd_w: int | None = Form(None),
    lcd_h: int | None = Form(None),
    quad_hint: str | None = Form(None),
) -> ReadResponse:
    eng = _require_engine()
    if file is not None:
        img = _decode_image(await file.read())
    elif image_base64:
        img = _decode_b64(image_base64)
    else:
        raise HTTPException(status_code=400, detail="provide file or image_base64")

    box = None
    if None not in (lcd_x, lcd_y, lcd_w, lcd_h):
        box = {"x": int(lcd_x), "y": int(lcd_y), "w": int(lcd_w), "h": int(lcd_h)}
    hint = _parse_quad_hint(quad_hint)
    return _to_response(
        eng.read(img, lcd_box=box, quad_hint=hint, return_debug=return_debug, run_audit=run_audit)
    )


@app.post("/v1/lcd/read-json", response_model=ReadResponse)
def read_json(body: ReadJsonRequest) -> ReadResponse:
    eng = _require_engine()
    img = _decode_b64(body.image_base64)
    box = body.lcd_box.model_dump() if body.lcd_box else None
    return _to_response(
        eng.read(
            img,
            lcd_box=box,
            quad_hint=body.quad_hint,
            return_debug=body.return_debug,
            run_audit=body.run_audit,
        )
    )


@app.post("/v1/lcd/read-batch", response_model=BatchResponse)
def read_batch(body: BatchRequest) -> BatchResponse:
    """Offline re-analysis — each item is independent (stateless)."""
    eng = _require_engine()
    results: list[ReadResponse] = []
    weights: list[float] = []
    confs: list[float] = []
    hint: list[list[float]] | None = None
    for item in body.items:
        img = _decode_b64(item.image_base64)
        box = item.lcd_box.model_dump() if item.lcd_box else None
        use_hint = item.quad_hint if item.quad_hint is not None else hint
        r = eng.read(img, lcd_box=box, quad_hint=use_hint, return_debug=body.return_debug)
        results.append(_to_response(r))
        if r.screen_quad is not None:
            hint = r.screen_quad
        if r.weight is not None and r.status == "readable":
            weights.append(float(r.weight))
            confs.append(float(r.confidence))
    vote_w = None
    vote_c = None
    if weights:
        rounded = [round(w, 2) for w in weights]
        vote_w = max(set(rounded), key=rounded.count)
        vote_c = float(sum(confs) / len(confs))
    return BatchResponse(results=results, vote_weight=vote_w, vote_confidence=vote_c)


def main() -> None:
    import uvicorn

    host = os.environ.get("LCD_OCR_HOST", "0.0.0.0")
    port = int(os.environ.get("LCD_OCR_PORT", "8768"))
    uvicorn.run("app:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
