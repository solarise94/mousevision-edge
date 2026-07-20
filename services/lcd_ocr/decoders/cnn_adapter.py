"""CNN digit decoder: TFLite/ONNX inference on 28×40 slot patches.

Drop-in replacement for ClassicV2Decoder. Reuses the same quality gates
(zero-display detection, strip quality assessment) but replaces per-slot
seven-segment geometric decoding with a learned CNN classifier.

Model input:  (1, 1, 40, 28) float32 in [0, 1] — grayscale, ink=white.
Model output: (1, 11) logits — classes: blank, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9.

Usage:
    LCD_OCR_DECODER=cnn LCD_OCR_CNN_MODEL=models/digit_cnn.tflite
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from quality import assess_strip_quality, tall_glyph_ranges
from sevenseg_classic import compose_weight

from .base import DecoderResult

CLASS_NAMES = ["blank", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
IMG_H, IMG_W = 40, 28

# ---------------------------------------------------------------------------
# Patch normalization (mirrors tools/collect_digit_patches.py)
# ---------------------------------------------------------------------------


def _normalize_patch(patch: np.ndarray) -> np.ndarray:
    """Resize to 28×40, center on zero canvas, Otsu binarize, ink=white."""
    if patch is None or patch.size == 0:
        return np.zeros((IMG_H, IMG_W), dtype=np.uint8)
    if patch.ndim == 3:
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
    else:
        gray = patch
    h, w = gray.shape[:2]
    if h < 1 or w < 1:
        return np.zeros((IMG_H, IMG_W), dtype=np.uint8)
    scale = min(IMG_W / w, IMG_H / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((IMG_H, IMG_W), dtype=np.uint8)
    y0 = (IMG_H - nh) // 2
    x0 = (IMG_W - nw) // 2
    canvas[y0 : y0 + nh, x0 : x0 + nw] = resized
    _, binary = cv2.threshold(canvas, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if float(np.mean(binary)) > 127.0:
        binary = 255 - binary
    return binary


def _prepare_input(patch: np.ndarray) -> np.ndarray:
    """Normalize patch → float32 tensor (1, 1, 40, 28) in [0, 1]."""
    img = _normalize_patch(patch)
    return img.astype(np.float32)[np.newaxis, np.newaxis, :, :] / 255.0


# ---------------------------------------------------------------------------
# Backend loaders
# ---------------------------------------------------------------------------


class _TFLiteBackend:
    def __init__(self, model_path: str):
        import tflite_runtime.interpreter as tflite  # type: ignore

        self.interp = tflite.Interpreter(model_path=model_path)
        self.interp.allocate_tensors()
        self.input_idx = self.interp.get_input_details()[0]["index"]
        self.output_idx = self.interp.get_output_details()[0]["index"]

    def infer(self, x: np.ndarray) -> np.ndarray:
        self.interp.set_tensor(self.input_idx, x)
        self.interp.invoke()
        return self.interp.get_tensor(self.output_idx)


class _ONNXBackend:
    def __init__(self, model_path: str):
        import onnxruntime as ort

        self.sess = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"]
        )
        self.input_name = self.sess.get_inputs()[0].name

    def infer(self, x: np.ndarray) -> np.ndarray:
        return self.sess.run(None, {self.input_name: x})[0]


class _TorchBackend:
    """Fallback: load .pth checkpoint directly with PyTorch."""

    def __init__(self, model_path: str):
        import torch
        import torch.nn as nn

        class _DigitCNN(nn.Module):
            def __init__(self, num_classes: int = 11):
                super().__init__()
                self.features = nn.Sequential(
                    nn.Conv2d(1, 16, 3, padding=1), nn.BatchNorm2d(16), nn.ReLU(),
                    nn.MaxPool2d(2),
                    nn.Conv2d(16, 32, 3, padding=1), nn.BatchNorm2d(32), nn.ReLU(),
                    nn.MaxPool2d(2),
                    nn.Conv2d(32, 64, 3, padding=1), nn.BatchNorm2d(64), nn.ReLU(),
                    nn.AdaptiveAvgPool2d(1),
                )
                self.classifier = nn.Sequential(nn.Dropout(0.3), nn.Linear(64, num_classes))

            def forward(self, x):
                x = self.features(x)
                x = x.view(x.size(0), -1)
                return self.classifier(x)

        self.torch = torch
        ckpt = torch.load(model_path, map_location="cpu", weights_only=False)
        state = ckpt["model_state_dict"] if isinstance(ckpt, dict) and "model_state_dict" in ckpt else ckpt
        self.model = _DigitCNN()
        self.model.load_state_dict(state)
        self.model.eval()

    def infer(self, x: np.ndarray) -> np.ndarray:
        t = self.torch.from_numpy(x)
        with self.torch.no_grad():
            return self.model(t).numpy()


def _load_backend(model_path: str):
    suffix = Path(model_path).suffix.lower()
    if suffix == ".tflite":
        return _TFLiteBackend(model_path)
    if suffix == ".onnx":
        return _ONNXBackend(model_path)
    if suffix in {".pth", ".pt"}:
        return _TorchBackend(model_path)
    raise ValueError(f"Unsupported model format: {suffix} (expected .tflite/.onnx/.pth)")


def _softmax(logits: np.ndarray) -> np.ndarray:
    e = np.exp(logits - logits.max(axis=-1, keepdims=True))
    return e / e.sum(axis=-1, keepdims=True)


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


class CnnDecoder:
    """CNN-based digit decoder implementing the DigitDecoder protocol."""

    name = "cnn"

    def __init__(self, model_path: str | None = None):
        path = model_path or os.environ.get("LCD_OCR_CNN_MODEL", "")
        if not path:
            # Search default locations
            root = Path(__file__).resolve().parents[2]
            candidates = [
                root / "models" / "digit_cnn.tflite",
                root / "models" / "digit_cnn.onnx",
                root / "models" / "digit_cnn.pth",
            ]
            for c in candidates:
                if c.is_file():
                    path = str(c)
                    break
        if not path or not Path(path).is_file():
            raise FileNotFoundError(
                f"CNN model not found. Set LCD_OCR_CNN_MODEL or place model in models/. "
                f"Tried: {path!r}"
            )
        self._backend = _load_backend(path)
        self._model_path = path

    def _classify_slot(self, patch: np.ndarray) -> tuple[str, float, np.ndarray]:
        """Run CNN on one slot patch. Returns (char, confidence, probs)."""
        x = _prepare_input(patch)
        logits = self._backend.infer(x)
        probs = _softmax(logits)[0]  # shape (11,)
        idx = int(np.argmax(probs))
        char = CLASS_NAMES[idx]
        conf = float(probs[idx])
        return char, conf, probs

    def read(self, normalized_strip: Any, slot_patches: list[Any]) -> DecoderResult:
        if len(slot_patches) != 4:
            return DecoderResult(
                digits=[],
                digit_confidences=[],
                weight=None,
                status="unreadable",
                quality=0.0,
                evidence={"error": "need_4_slots"},
            )

        # --- Quality gates (same as classic_v2) ---
        # Zero-display: three intact zero glyphs
        zero_ranges = tall_glyph_ranges(normalized_strip)
        if len(zero_ranges) == 3:
            # Delegate to classic per-glyph check for zero confirmation
            from sevenseg_classic import decode_slot_patch

            zero_decoded = []
            for x0, x1 in zero_ranges:
                pad = max(1, int(0.05 * (x1 - x0)))
                p = normalized_strip[:, max(0, x0 - pad) : min(normalized_strip.shape[1], x1 + pad)]
                zero_decoded.append(decode_slot_patch(p))
            if all(d.char == "0" for d in zero_decoded):
                return DecoderResult(
                    digits=["0", "0", "0"],
                    digit_confidences=[float(d.confidence) for d in zero_decoded],
                    weight=0.0,
                    status="zero_display",
                    quality=float(np.mean([d.confidence for d in zero_decoded])),
                    evidence={"quality_gate": "three_zero_glyphs", "glyph_ranges": zero_ranges},
                )

        q = assess_strip_quality(normalized_strip, slot_patches)
        if q.status == "zero_display":
            return DecoderResult(
                digits=["0", "0", "0", "0"],
                digit_confidences=[0.9, 0.9, 0.9, 0.9],
                weight=0.0,
                status="zero_display",
                quality=0.85,
                evidence={"quality_gate": q.reason, **q.evidence},
            )
        if q.status in {"transition", "unreadable"}:
            return DecoderResult(
                digits=["invalid"] * 4,
                digit_confidences=[0.0] * 4,
                weight=None,
                status=q.status,
                quality=0.0,
                evidence={"quality_gate": q.reason, **q.evidence},
            )

        # --- CNN inference per slot ---
        chars: list[str] = []
        confs: list[float] = []
        all_probs: list[list[float]] = []
        for patch in slot_patches:
            char, conf, probs = self._classify_slot(patch)
            chars.append(char)
            confs.append(conf)
            all_probs.append([round(float(p), 4) for p in probs])

        evidence: dict[str, Any] = {
            "quality_gate": "ok",
            "ink_ratio": q.ink_ratio,
            "cnn_probs": all_probs,
            "model": self._model_path,
            **q.evidence,
        }

        # Map "blank" to display semantics: blank in leading position is fine,
        # but blank in non-leading position means unreadable.
        if any(c == "invalid" for c in chars):
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=None,
                status="unreadable",
                quality=float(np.mean(confs)),
                evidence=evidence,
            )

        quality = float(np.mean(confs))

        # Low-confidence guard: if average confidence is very low, mark transition
        if quality < 0.40:
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=None,
                status="transition",
                quality=quality,
                evidence={**evidence, "quality_gate": "low_cnn_confidence"},
            )

        weight = compose_weight(chars)
        if weight is None:
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=None,
                status="unreadable",
                quality=quality,
                evidence=evidence,
            )

        # Zero-weight detection
        zeroish = sum(1 for c in chars if c in {"blank", "0"}) >= 3
        if weight <= 0.05 or (weight < 0.10 and zeroish):
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=0.0,
                status="zero_display",
                quality=quality,
                evidence=evidence,
            )

        # Out-of-range ceiling
        if weight > 50.0:
            return DecoderResult(
                digits=chars,
                digit_confidences=confs,
                weight=None,
                status="unreadable",
                quality=quality * 0.5,
                evidence={**evidence, "quality_gate": "weight_ceiling"},
            )

        return DecoderResult(
            digits=chars,
            digit_confidences=confs,
            weight=weight,
            status="readable",
            quality=quality,
            evidence=evidence,
        )
