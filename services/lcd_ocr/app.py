"""LCD OCR HTTP service — RapidOCR + OpenVINO (GPU prefer / CPU fallback)."""

from __future__ import annotations

import base64
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import cv2
import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from engine import LcdOcrEngine, ReadResult

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("lcd_ocr")

engine: LcdOcrEngine | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global engine
    engine = LcdOcrEngine()
    logger.info("model=%s device=%s", engine.model_name, engine.device)
    yield
    engine = None


app = FastAPI(title="MouseVision LCD OCR", version="0.1.0", lifespan=lifespan)


class LcdBoxIn(BaseModel):
    x: int
    y: int
    w: int
    h: int


class ReadJsonRequest(BaseModel):
    image_base64: str = Field(..., description="JPEG/PNG base64, optional data: URL prefix")
    lcd_box: LcdBoxIn | None = None
    return_debug: bool = False


class ReadResponse(BaseModel):
    weight: float | None
    confidence: float
    raw_text: str
    digits: list[str]
    lcd_box: dict[str, int] | None
    device: str
    latency_ms: float
    debug: dict[str, Any] | None = None


class BatchItem(BaseModel):
    image_base64: str
    lcd_box: LcdBoxIn | None = None


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


def _to_response(r: ReadResult) -> ReadResponse:
    return ReadResponse(
        weight=r.weight,
        confidence=round(float(r.confidence), 4),
        raw_text=r.raw_text,
        digits=r.digits,
        lcd_box=r.lcd_box,
        device=r.device,
        latency_ms=round(float(r.latency_ms), 2),
        debug=r.debug,
    )


def _require_engine() -> LcdOcrEngine:
    if engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    return engine


@app.get("/health")
def health() -> dict[str, Any]:
    eng = _require_engine()
    # Tiny synthetic latency probe: empty path returns quickly without OCR.
    return {
        "ok": True,
        "device": eng.device,
        "model": eng.model_name,
        "latency_ms": 0.0,
    }


@app.post("/v1/lcd/read", response_model=ReadResponse)
async def read_multipart(
    file: UploadFile | None = File(None),
    image_base64: str | None = Form(None),
    return_debug: bool = Form(False),
    lcd_x: int | None = Form(None),
    lcd_y: int | None = Form(None),
    lcd_w: int | None = Form(None),
    lcd_h: int | None = Form(None),
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
    return _to_response(eng.read(img, lcd_box=box, return_debug=return_debug))


@app.post("/v1/lcd/read-json", response_model=ReadResponse)
def read_json(body: ReadJsonRequest) -> ReadResponse:
    eng = _require_engine()
    img = _decode_b64(body.image_base64)
    box = body.lcd_box.model_dump() if body.lcd_box else None
    return _to_response(eng.read(img, lcd_box=box, return_debug=body.return_debug))


@app.post("/v1/lcd/read-batch", response_model=BatchResponse)
def read_batch(body: BatchRequest) -> BatchResponse:
    """Offline re-analysis / future batch decode only — not used by live state machine."""
    eng = _require_engine()
    results: list[ReadResponse] = []
    weights: list[float] = []
    confs: list[float] = []
    for item in body.items:
        img = _decode_b64(item.image_base64)
        box = item.lcd_box.model_dump() if item.lcd_box else None
        r = eng.read(img, lcd_box=box, return_debug=body.return_debug)
        results.append(_to_response(r))
        if r.weight is not None:
            weights.append(float(r.weight))
            confs.append(float(r.confidence))
    vote_w = None
    vote_c = None
    if weights:
        # Mode-ish: round to 0.01 and pick most common; confidence = mean of voters.
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
