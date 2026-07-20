"""Evaluate digit CNN on test data.

Usage:
    python training/eval_digit_cnn.py --model models/digit_cnn_final.pth --data training_data/0001
    python training/eval_digit_cnn.py --model models/digit_cnn.tflite --data training_data/0001 --tflite
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from training.train_digit_cnn import (  # noqa: E402
    CLASS_NAMES,
    CLASS_TO_IDX,
    NUM_CLASSES,
    _load_gray,
    discover_samples,
    format_confusion,
    load_model,
)

try:
    from sklearn.metrics import classification_report, confusion_matrix
except ImportError:  # pragma: no cover
    classification_report = None  # type: ignore[assignment]
    confusion_matrix = None  # type: ignore[assignment]


def _blank_one_confusion(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    """Highlight the key failure mode: 1 ↔ blank."""
    i_blank = CLASS_TO_IDX["blank"]
    i_one = CLASS_TO_IDX["1"]
    n_blank = int((y_true == i_blank).sum())
    n_one = int((y_true == i_one).sum())
    blank_as_one = int(((y_true == i_blank) & (y_pred == i_one)).sum())
    one_as_blank = int(((y_true == i_one) & (y_pred == i_blank)).sum())
    return {
        "n_blank": n_blank,
        "n_one": n_one,
        "blank_as_one": blank_as_one,
        "one_as_blank": one_as_blank,
        "blank_as_one_rate": blank_as_one / n_blank if n_blank else 0.0,
        "one_as_blank_rate": one_as_blank / n_one if n_one else 0.0,
        "pair_error_rate": (blank_as_one + one_as_blank) / max(1, n_blank + n_one),
    }


class TorchPredictor:
    def __init__(self, model_path: Path, device: str = ""):
        import torch

        if device:
            dev = torch.device(device)
        elif torch.cuda.is_available():
            dev = torch.device("cuda")
        else:
            dev = torch.device("cpu")
        self.device = dev
        self.torch = torch
        self.model = load_model(model_path, dev)

    def predict_batch(self, images: list[np.ndarray]) -> np.ndarray:
        torch = self.torch
        batch = np.stack(
            [(im.astype(np.float32) / 255.0) for im in images],
            axis=0,
        )[:, None, :, :]
        xb = torch.from_numpy(batch).to(self.device)
        with torch.no_grad():
            logits = self.model(xb)
            pred = logits.argmax(dim=1).cpu().numpy()
        return pred.astype(np.int64)


class TFLitePredictor:
    def __init__(self, model_path: Path):
        try:
            import tensorflow as tf  # type: ignore

            self._mode = "tf"
            self.interpreter = tf.lite.Interpreter(model_path=str(model_path))
            self.interpreter.allocate_tensors()
            self.input_details = self.interpreter.get_input_details()
            self.output_details = self.interpreter.get_output_details()
        except Exception:
            try:
                from tflite_runtime.interpreter import Interpreter  # type: ignore

                self._mode = "runtime"
                self.interpreter = Interpreter(model_path=str(model_path))
                self.interpreter.allocate_tensors()
                self.input_details = self.interpreter.get_input_details()
                self.output_details = self.interpreter.get_output_details()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    "TFLite inference requires tensorflow or tflite-runtime"
                ) from exc

    def predict_batch(self, images: list[np.ndarray]) -> np.ndarray:
        preds: list[int] = []
        inp = self.input_details[0]
        scale, zero = 1.0, 0
        if inp["dtype"] == np.uint8 or inp["dtype"] == np.int8:
            scale, zero = inp.get("quantization", (1.0, 0))
        for im in images:
            x = (im.astype(np.float32) / 255.0)[None, None, :, :]  # NCHW
            # TFLite models may be NHWC
            shape = tuple(inp["shape"])
            if len(shape) == 4 and shape[-1] == 1:
                x = np.transpose(x, (0, 2, 3, 1))  # NHWC
            if inp["dtype"] == np.float32:
                x_in = x.astype(np.float32)
            elif inp["dtype"] == np.uint8:
                x_in = np.clip(x / scale + zero, 0, 255).astype(np.uint8)
            elif inp["dtype"] == np.int8:
                x_in = np.clip(x / scale + zero, -128, 127).astype(np.int8)
            else:
                x_in = x.astype(inp["dtype"])
            self.interpreter.set_tensor(inp["index"], x_in)
            self.interpreter.invoke()
            out = self.interpreter.get_tensor(self.output_details[0]["index"])
            out_detail = self.output_details[0]
            if out_detail["dtype"] in (np.uint8, np.int8):
                oscale, ozero = out_detail.get("quantization", (1.0, 0))
                out = (out.astype(np.float32) - ozero) * oscale
            preds.append(int(np.argmax(out.reshape(-1))))
        return np.asarray(preds, dtype=np.int64)


class OnnxPredictor:
    def __init__(self, model_path: Path):
        import onnxruntime as ort

        self.sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        self.input_name = self.sess.get_inputs()[0].name

    def predict_batch(self, images: list[np.ndarray]) -> np.ndarray:
        batch = np.stack([(im.astype(np.float32) / 255.0) for im in images], axis=0)[:, None, :, :]
        out = self.sess.run(None, {self.input_name: batch})[0]
        return np.argmax(out, axis=1).astype(np.int64)


def classic_v2_predict(images: list[np.ndarray]) -> np.ndarray:
    """Baseline: mousevision seven-segment decoder (decode_seven_seg)."""
    try:
        from mousevision.reader.template import decode_seven_seg
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("Cannot import decode_seven_seg for --baseline") from exc

    preds: list[int] = []
    for im in images:
        # decode_seven_seg expects binary-ish patch
        if im.dtype != np.uint8:
            im = np.clip(im, 0, 255).astype(np.uint8)
        # light binarize if not already sparse
        if im.mean() > 30:
            _, bw = cv2.threshold(im, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        else:
            bw = im
        char, _conf = decode_seven_seg(bw)
        if char in ("?", "") or char is None:
            preds.append(CLASS_TO_IDX["blank"])
        elif char in CLASS_TO_IDX:
            preds.append(CLASS_TO_IDX[char])
        else:
            preds.append(CLASS_TO_IDX["blank"])
    return np.asarray(preds, dtype=np.int64)


def run_eval(
    predictor: Any,
    samples: list[tuple[Path, int, str]],
    batch_size: int = 256,
    label: str = "model",
) -> dict[str, Any]:
    y_true: list[int] = []
    y_pred: list[int] = []
    paths = [s[0] for s in samples]
    labels = [s[1] for s in samples]

    for start in range(0, len(paths), batch_size):
        chunk_paths = paths[start : start + batch_size]
        chunk_labels = labels[start : start + batch_size]
        images = [_load_gray(p) for p in chunk_paths]
        preds = predictor.predict_batch(images) if hasattr(predictor, "predict_batch") else predictor(images)
        y_true.extend(chunk_labels)
        y_pred.extend(preds.tolist() if hasattr(preds, "tolist") else list(preds))

    y_true_a = np.asarray(y_true, dtype=np.int64)
    y_pred_a = np.asarray(y_pred, dtype=np.int64)
    acc = float((y_true_a == y_pred_a).mean()) if len(y_true_a) else 0.0

    if confusion_matrix is not None:
        cm = confusion_matrix(y_true_a, y_pred_a, labels=list(range(NUM_CLASSES)))
    else:
        cm = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)
        for t, p in zip(y_true_a, y_pred_a):
            if 0 <= t < NUM_CLASSES and 0 <= p < NUM_CLASSES:
                cm[t, p] += 1

    print(f"\n=== {label} ===")
    print(f"N={len(y_true_a)}  overall accuracy={acc:.4f}")
    if classification_report is not None:
        print(
            classification_report(
                y_true_a,
                y_pred_a,
                labels=list(range(NUM_CLASSES)),
                target_names=CLASS_NAMES,
                digits=3,
                zero_division=0,
            )
        )
    else:
        print("(install scikit-learn for full precision/recall/F1 report)")
        for i, name in enumerate(CLASS_NAMES):
            support = int(cm[i].sum())
            tp = int(cm[i, i])
            if support == 0:
                continue
            print(f"  {name}: acc={tp / support:.3f} support={support}")

    print("Confusion matrix:")
    print(format_confusion(cm))

    conf = _blank_one_confusion(y_true_a, y_pred_a)
    print("\n1 ↔ blank confusion (key failure mode):")
    print(f"  blank→1: {conf['blank_as_one']}/{conf['n_blank']} ({conf['blank_as_one_rate']:.3f})")
    print(f"  1→blank: {conf['one_as_blank']}/{conf['n_one']} ({conf['one_as_blank_rate']:.3f})")
    print(f"  pair error rate: {conf['pair_error_rate']:.3f}")

    return {
        "acc": acc,
        "cm": cm,
        "y_true": y_true_a,
        "y_pred": y_pred_a,
        "blank_one": conf,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", type=Path, required=True, help="Path to .pth / .onnx / .tflite")
    p.add_argument(
        "--data",
        type=str,
        required=True,
        help="Comma-separated dataset roots",
    )
    p.add_argument("--tflite", action="store_true", help="Force TFLite interpreter")
    p.add_argument("--baseline", action="store_true", help="Also run classic_v2 decode_seven_seg")
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--device", type=str, default="")
    return p.parse_args(argv)


def build_predictor(model_path: Path, *, force_tflite: bool, device: str) -> Any:
    model_path = model_path if model_path.is_absolute() else ROOT / model_path
    if not model_path.is_file():
        raise FileNotFoundError(model_path)
    suffix = model_path.suffix.lower()
    if force_tflite or suffix == ".tflite":
        return TFLitePredictor(model_path)
    if suffix == ".onnx":
        return OnnxPredictor(model_path)
    return TorchPredictor(model_path, device=device)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    data_roots = [Path(p.strip()) for p in args.data.split(",") if p.strip()]
    data_roots = [p if p.is_absolute() else ROOT / p for p in data_roots]
    print("Discovering samples...")
    samples = discover_samples(data_roots)
    if not samples:
        print("ERROR: no labeled patches found")
        return 1
    print(f"Total labeled patches: {len(samples)}")

    predictor = build_predictor(args.model, force_tflite=args.tflite, device=args.device)
    cnn_res = run_eval(predictor, samples, batch_size=args.batch_size, label=f"CNN ({args.model})")

    if args.baseline:
        base_res = run_eval(
            classic_v2_predict,
            samples,
            batch_size=args.batch_size,
            label="classic_v2 (decode_seven_seg)",
        )
        print("\n=== Comparison ===")
        print(f"  CNN acc:        {cnn_res['acc']:.4f}")
        print(f"  classic_v2 acc: {base_res['acc']:.4f}")
        print(
            f"  CNN 1↔blank pair err:        {cnn_res['blank_one']['pair_error_rate']:.4f}"
        )
        print(
            f"  classic_v2 1↔blank pair err: {base_res['blank_one']['pair_error_rate']:.4f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
