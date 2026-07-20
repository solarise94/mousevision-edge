"""Train LCD digit CNN classifier.

Usage:
    # Phase 1: pretrain on synthetic
    python training/train_digit_cnn.py --data training_data/synthetic --epochs 30 --output models/digit_cnn_pretrained.pth

    # Phase 2: fine-tune on real data
    python training/train_digit_cnn.py --data training_data/refvideo,training_data/0001 --pretrained models/digit_cnn_pretrained.pth --epochs 50 --lr 1e-4 --output models/digit_cnn_final.pth

    # Export TFLite
    python training/train_digit_cnn.py --export-tflite models/digit_cnn_final.pth --output models/digit_cnn.tflite
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset, WeightedRandomSampler

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CLASS_NAMES = ["blank", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]
CLASS_TO_IDX = {c: i for i, c in enumerate(CLASS_NAMES)}
NUM_CLASSES = len(CLASS_NAMES)
IMG_H, IMG_W = 40, 28  # PyTorch NCHW: (B, 1, 40, 28)

# Filename pattern for flat patches: {session:02d}_{frame:06d}_slot{i}.png
FLAT_NAME_RE = re.compile(
    r"^(?P<session>\d+)_(?P<frame>\d+)_slot(?P<slot>\d+)\.(?:png|jpg|jpeg|bmp)$",
    re.IGNORECASE,
)


class DigitCNN(nn.Module):
    """Small CNN for 28×40 grayscale digit patches (~15K params)."""

    def __init__(self, num_classes: int = NUM_CLASSES):
        super().__init__()
        self.features = nn.Sequential(
            nn.Conv2d(1, 16, 3, padding=1),
            nn.BatchNorm2d(16),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),  # 20×14
            nn.Conv2d(16, 32, 3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(inplace=True),
            nn.MaxPool2d(2),  # 10×7
            nn.Conv2d(32, 64, 3, padding=1),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=True),
            nn.AdaptiveAvgPool2d(1),  # 1×1
        )
        self.classifier = nn.Sequential(
            nn.Dropout(0.3),
            nn.Linear(64, num_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.features(x)
        x = x.view(x.size(0), -1)
        return self.classifier(x)


def count_parameters(model: nn.Module) -> int:
    return sum(p.numel() for p in model.parameters() if p.requires_grad)


def _load_gray(path: Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise FileNotFoundError(f"Failed to read image: {path}")
    if img.shape[0] != IMG_H or img.shape[1] != IMG_W:
        img = cv2.resize(img, (IMG_W, IMG_H), interpolation=cv2.INTER_AREA)
    return img


def _normalize_label(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("", "none", "null", "nan"):
        return None
    if s in ("invalid", "?", "unknown", "x", "-"):
        return "blank"
    if s in CLASS_TO_IDX:
        return s
    if s.isdigit() and s in CLASS_TO_IDX:
        return s
    return None


def _discover_class_folder_samples(root: Path) -> list[tuple[Path, int, str]]:
    """Return list of (path, label_idx, session_key)."""
    samples: list[tuple[Path, int, str]] = []
    for class_name in CLASS_NAMES:
        class_dir = root / class_name
        if not class_dir.is_dir():
            # Also accept uppercase folder names
            alt = root / class_name.upper()
            class_dir = alt if alt.is_dir() else class_dir
        if not class_dir.is_dir():
            continue
        label = CLASS_TO_IDX[class_name]
        for path in sorted(class_dir.rglob("*")):
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
                continue
            # session key: class + parent stem keeps synthetic split stable
            session = f"{root.name}:{class_name}:{path.stem[:3]}"
            samples.append((path, label, session))
    return samples


def _label_from_flat_manifest(
    root: Path,
    filename: str,
    manifest: dict[str, Any],
) -> tuple[int, str] | None:
    """Map flat patch filename to (label_idx, session_key) using manifest."""
    m = FLAT_NAME_RE.match(Path(filename).name)
    if m is None:
        # Fallback: direct mapping dicts
        labels_map = manifest.get("labels") or manifest.get("patches") or {}
        if isinstance(labels_map, dict) and filename in labels_map:
            lab = _normalize_label(labels_map[filename])
            if lab is None:
                return None
            return CLASS_TO_IDX[lab], f"{root.name}:flat"
        # try stem
        stem = Path(filename).stem
        if isinstance(labels_map, dict) and stem in labels_map:
            lab = _normalize_label(labels_map[stem])
            if lab is None:
                return None
            return CLASS_TO_IDX[lab], f"{root.name}:flat"
        return None

    session_id = int(m.group("session"))
    slot = int(m.group("slot"))
    session_key = f"{root.name}:s{session_id:02d}"

    # Prefer sessions list with label_digits
    sessions = manifest.get("sessions")
    if isinstance(sessions, list):
        for sess in sessions:
            if not isinstance(sess, dict):
                continue
            sid = sess.get("ordinal", sess.get("session", sess.get("id", sess.get("session_id"))))
            try:
                sid_i = int(sid)
            except (TypeError, ValueError):
                continue
            if sid_i != session_id:
                continue
            digits = sess.get("label_digits") or sess.get("digits") or sess.get("labels")
            if digits is None:
                return None
            if isinstance(digits, str):
                # e.g. "12.34" or "blank,1,2,3"
                if "," in digits:
                    parts = [p.strip() for p in digits.split(",")]
                else:
                    parts = list(digits.replace(".", ""))
            else:
                parts = list(digits)
            if slot < 0 or slot >= len(parts):
                return None
            lab = _normalize_label(parts[slot])
            if lab is None:
                return None
            return CLASS_TO_IDX[lab], session_key

    # Global label_digits for single-session roots
    digits = manifest.get("label_digits")
    if digits is not None:
        if isinstance(digits, str):
            parts = list(digits.replace(".", "")) if "," not in digits else [
                p.strip() for p in digits.split(",")
            ]
        else:
            parts = list(digits)
        if 0 <= slot < len(parts):
            lab = _normalize_label(parts[slot])
            if lab is not None:
                return CLASS_TO_IDX[lab], session_key

    # per-file entries
    entries = manifest.get("entries") or manifest.get("items") or []
    if isinstance(entries, list):
        for e in entries:
            if not isinstance(e, dict):
                continue
            name = e.get("file") or e.get("filename") or e.get("path")
            if name is None:
                continue
            if Path(name).name != Path(filename).name:
                continue
            lab = _normalize_label(e.get("label") or e.get("digit") or e.get("class"))
            if lab is None:
                return None
            return CLASS_TO_IDX[lab], session_key

    return None


def _discover_flat_samples(root: Path) -> list[tuple[Path, int, str]]:
    """Flat layout: patches/ + manifest.json with session label_digits."""
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        # Maybe patches live at root with manifest
        patch_dirs = [root / "patches", root]
    else:
        patch_dirs = [root / "patches", root]

    if not manifest_path.is_file():
        return []

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"WARNING: invalid manifest {manifest_path}: {exc}")
        return []

    samples: list[tuple[Path, int, str]] = []
    seen: set[Path] = set()
    for patch_dir in patch_dirs:
        if not patch_dir.is_dir():
            continue
        for path in sorted(patch_dir.iterdir()):
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
                continue
            if path in seen:
                continue
            mapped = _label_from_flat_manifest(root, path.name, manifest)
            if mapped is None:
                continue
            label, session = mapped
            samples.append((path, label, session))
            seen.add(path)
    return samples


def discover_samples(data_roots: list[Path]) -> list[tuple[Path, int, str]]:
    """Discover samples from one or more dataset roots."""
    all_samples: list[tuple[Path, int, str]] = []
    for root in data_roots:
        root = Path(root)
        if not root.exists():
            print(f"WARNING: data root missing: {root}")
            continue
        # Class-folder if any class subdir exists
        has_class_dirs = any((root / c).is_dir() for c in CLASS_NAMES)
        if has_class_dirs:
            found = _discover_class_folder_samples(root)
            print(f"  {root}: class-folder layout, {len(found)} patches")
            all_samples.extend(found)
            continue
        found = _discover_flat_samples(root)
        if found:
            print(f"  {root}: flat+manifest layout, {len(found)} patches")
            all_samples.extend(found)
            continue
        # Last resort: recurse class folders under children
        for child in sorted(p for p in root.iterdir() if p.is_dir()):
            if any((child / c).is_dir() for c in CLASS_NAMES):
                found = _discover_class_folder_samples(child)
                print(f"  {child}: class-folder layout, {len(found)} patches")
                all_samples.extend(found)
            else:
                found = _discover_flat_samples(child)
                if found:
                    print(f"  {child}: flat+manifest layout, {len(found)} patches")
                    all_samples.extend(found)
    return all_samples


def session_split(
    samples: list[tuple[Path, int, str]],
    val_ratio: float = 0.2,
    seed: int = 42,
) -> tuple[list[int], list[int]]:
    """80/20 split by session key to avoid leakage from adjacent frames."""
    by_session: dict[str, list[int]] = defaultdict(list)
    for idx, (_, _, session) in enumerate(samples):
        by_session[session].append(idx)

    sessions = sorted(by_session.keys())
    rng = random.Random(seed)
    rng.shuffle(sessions)

    if len(sessions) == 1:
        # Fall back to stratified index split when only one session
        indices = list(range(len(samples)))
        rng.shuffle(indices)
        n_val = max(1, int(len(indices) * val_ratio)) if len(indices) > 1 else 0
        return indices[n_val:], indices[:n_val]

    n_val_sessions = max(1, int(round(len(sessions) * val_ratio)))
    val_sessions = set(sessions[:n_val_sessions])
    train_idx: list[int] = []
    val_idx: list[int] = []
    for s, idxs in by_session.items():
        if s in val_sessions:
            val_idx.extend(idxs)
        else:
            train_idx.extend(idxs)
    if not train_idx or not val_idx:
        # Degenerate: re-split by index
        indices = list(range(len(samples)))
        rng.shuffle(indices)
        n_val = max(1, int(len(indices) * val_ratio))
        return indices[n_val:], indices[:n_val]
    return train_idx, val_idx


class DigitPatchDataset(Dataset):
    """Load patches from class-folder or flat+manifest layouts.

    Directory layouts:
      1. Class-folder: {root}/{class_name}/*.png
      2. Flat + manifest: {root}/patches/*.png + {root}/manifest.json
    """

    def __init__(
        self,
        samples: list[tuple[Path, int, str]],
        *,
        augment: bool = False,
        seed: int = 0,
    ):
        self.samples = samples
        self.augment = augment
        self.rng = np.random.default_rng(seed)

    def __len__(self) -> int:
        return len(self.samples)

    def _augment(self, img: np.ndarray) -> np.ndarray:
        out = img.astype(np.float32)
        # Brightness
        if self.rng.random() < 0.8:
            out *= float(self.rng.uniform(0.7, 1.3))
            out += float(self.rng.uniform(-15, 15))
        # Slight rotation
        if self.rng.random() < 0.5:
            angle = float(self.rng.uniform(-3.0, 3.0))
            m = cv2.getRotationMatrix2D((IMG_W / 2.0, IMG_H / 2.0), angle, 1.0)
            out = cv2.warpAffine(
                np.clip(out, 0, 255).astype(np.uint8),
                m,
                (IMG_W, IMG_H),
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            ).astype(np.float32)
        # Noise
        if self.rng.random() < 0.5:
            out = out + self.rng.normal(0.0, float(self.rng.uniform(2.0, 12.0)), out.shape)
        # Shift ±1 px
        if self.rng.random() < 0.4:
            dx = int(self.rng.integers(-1, 2))
            dy = int(self.rng.integers(-1, 2))
            m = np.float32([[1, 0, dx], [0, 1, dy]])
            out = cv2.warpAffine(
                np.clip(out, 0, 255).astype(np.uint8),
                m,
                (IMG_W, IMG_H),
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            ).astype(np.float32)
        return np.clip(out, 0, 255).astype(np.uint8)

    def __getitem__(self, idx: int) -> tuple[torch.Tensor, int]:
        path, label, _ = self.samples[idx]
        img = _load_gray(path)
        if self.augment:
            img = self._augment(img)
        # float tensor in [0, 1], shape (1, H, W)
        tensor = torch.from_numpy(img.astype(np.float32) / 255.0).unsqueeze(0)
        return tensor, label


def class_weights_from_labels(labels: list[int], num_classes: int = NUM_CLASSES) -> torch.Tensor:
    """Inverse-frequency class weights (normalized to mean 1)."""
    counts = np.bincount(np.asarray(labels, dtype=np.int64), minlength=num_classes).astype(np.float64)
    counts = np.maximum(counts, 1.0)
    inv = 1.0 / counts
    inv = inv * (num_classes / inv.sum())
    return torch.tensor(inv, dtype=torch.float32)


@torch.no_grad()
def evaluate(
    model: nn.Module,
    loader: DataLoader,
    device: torch.device,
    criterion: nn.Module | None = None,
) -> dict[str, Any]:
    model.eval()
    total_loss = 0.0
    n = 0
    y_true: list[int] = []
    y_pred: list[int] = []
    for xb, yb in loader:
        xb = xb.to(device, non_blocking=True)
        yb = yb.to(device, non_blocking=True)
        logits = model(xb)
        if criterion is not None:
            total_loss += float(criterion(logits, yb).item()) * xb.size(0)
        pred = logits.argmax(dim=1)
        y_true.extend(yb.detach().cpu().tolist())
        y_pred.extend(pred.detach().cpu().tolist())
        n += xb.size(0)

    y_true_a = np.asarray(y_true, dtype=np.int64)
    y_pred_a = np.asarray(y_pred, dtype=np.int64)
    correct = int((y_true_a == y_pred_a).sum()) if len(y_true_a) else 0
    acc = correct / max(1, len(y_true_a))
    cm = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=np.int64)
    for t, p in zip(y_true_a, y_pred_a):
        if 0 <= t < NUM_CLASSES and 0 <= p < NUM_CLASSES:
            cm[t, p] += 1
    per_class = {}
    for i, name in enumerate(CLASS_NAMES):
        support = int(cm[i].sum())
        tp = int(cm[i, i])
        per_class[name] = {
            "support": support,
            "correct": tp,
            "acc": (tp / support) if support else 0.0,
        }
    return {
        "loss": total_loss / max(1, n),
        "acc": acc,
        "n": len(y_true_a),
        "cm": cm,
        "per_class": per_class,
        "y_true": y_true_a,
        "y_pred": y_pred_a,
    }


def format_confusion(cm: np.ndarray) -> str:
    header = "true\\pred".ljust(10) + "".join(c.rjust(6) for c in CLASS_NAMES)
    lines = [header]
    for i, name in enumerate(CLASS_NAMES):
        row = name.ljust(10) + "".join(str(int(v)).rjust(6) for v in cm[i])
        lines.append(row)
    return "\n".join(lines)


def train_one_epoch(
    model: nn.Module,
    loader: DataLoader,
    optimizer: torch.optim.Optimizer,
    criterion: nn.Module,
    device: torch.device,
    scaler: torch.cuda.amp.GradScaler | None,
) -> dict[str, float]:
    model.train()
    total_loss = 0.0
    correct = 0
    n = 0
    use_amp = scaler is not None and device.type == "cuda"
    for xb, yb in loader:
        xb = xb.to(device, non_blocking=True)
        yb = yb.to(device, non_blocking=True)
        optimizer.zero_grad(set_to_none=True)
        if use_amp:
            with torch.cuda.amp.autocast():
                logits = model(xb)
                loss = criterion(logits, yb)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()
        else:
            logits = model(xb)
            loss = criterion(logits, yb)
            loss.backward()
            optimizer.step()
        total_loss += float(loss.item()) * xb.size(0)
        correct += int((logits.argmax(1) == yb).sum().item())
        n += xb.size(0)
    return {"loss": total_loss / max(1, n), "acc": correct / max(1, n)}


def save_checkpoint(
    path: Path,
    model: DigitCNN,
    epoch: int,
    val_acc: float,
    args: argparse.Namespace,
) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "model_state_dict": model.state_dict(),
        "epoch": epoch,
        "val_acc": val_acc,
        "class_names": CLASS_NAMES,
        "img_h": IMG_H,
        "img_w": IMG_W,
        "num_classes": NUM_CLASSES,
        "args": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
    }
    torch.save(payload, path)
    print(f"  saved checkpoint -> {path} (val_acc={val_acc:.4f})")


def load_model(path: Path, device: torch.device) -> DigitCNN:
    ckpt = torch.load(path, map_location=device, weights_only=False)
    model = DigitCNN(num_classes=NUM_CLASSES)
    if isinstance(ckpt, dict) and "model_state_dict" in ckpt:
        model.load_state_dict(ckpt["model_state_dict"])
    else:
        model.load_state_dict(ckpt)
    model.to(device)
    model.eval()
    return model


def export_tflite(pth_path: str | Path, output_path: str | Path) -> Path:
    """Export trained model to TFLite (best-effort) or ONNX fallback.

    Primary: ai_edge_torch.export
    Fallback: ONNX via torch.onnx.export + optional onnx2tf instructions
    """
    pth_path = Path(pth_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    device = torch.device("cpu")
    model = load_model(pth_path, device)
    model.eval()
    example = torch.zeros(1, 1, IMG_H, IMG_W, dtype=torch.float32)

    # TorchScript trace (sanity + intermediate)
    try:
        traced = torch.jit.trace(model, example)
        ts_path = output_path.with_suffix(".pt")
        traced.save(str(ts_path))
        print(f"TorchScript saved: {ts_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"TorchScript trace failed: {exc}")

    # ONNX export
    onnx_path = output_path.with_suffix(".onnx")
    try:
        torch.onnx.export(
            model,
            example,
            str(onnx_path),
            input_names=["input"],
            output_names=["logits"],
            dynamic_axes={"input": {0: "batch"}, "logits": {0: "batch"}},
            opset_version=13,
            do_constant_folding=True,
        )
        print(f"ONNX saved: {onnx_path}")
        try:
            import onnxruntime as ort

            sess = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
            out = sess.run(None, {"input": example.numpy()})[0]
            with torch.no_grad():
                ref = model(example).numpy()
            max_diff = float(np.max(np.abs(out - ref)))
            print(f"ONNXRuntime verify max|diff|={max_diff:.6g}")
        except Exception as exc:  # noqa: BLE001
            print(f"ONNXRuntime verify skipped/failed: {exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"ONNX export failed: {exc}")
        onnx_path = None  # type: ignore[assignment]

    # Try ai_edge_torch
    try:
        import ai_edge_torch  # type: ignore

        edge_model = ai_edge_torch.convert(model, (example,))
        tflite_path = output_path if output_path.suffix == ".tflite" else output_path.with_suffix(".tflite")
        edge_model.export(str(tflite_path))
        print(f"TFLite (ai_edge_torch) saved: {tflite_path}")
        return tflite_path
    except Exception as exc:  # noqa: BLE001
        print(f"ai_edge_torch export unavailable/failed: {exc}")

    # Try onnx2tf path instructions / best effort
    if onnx_path is not None and Path(onnx_path).is_file():
        try:
            import subprocess

            tflite_path = output_path if output_path.suffix == ".tflite" else output_path.with_suffix(".tflite")
            # onnx2tf typically writes into an output directory
            out_dir = tflite_path.parent / (tflite_path.stem + "_onnx2tf")
            out_dir.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable,
                "-m",
                "onnx2tf",
                "-i",
                str(onnx_path),
                "-o",
                str(out_dir),
            ]
            print(f"Trying onnx2tf: {' '.join(cmd)}")
            subprocess.run(cmd, check=False)
            candidates = list(out_dir.glob("*.tflite"))
            if candidates:
                import shutil

                shutil.copy2(candidates[0], tflite_path)
                print(f"TFLite (onnx2tf) saved: {tflite_path}")
                return tflite_path
        except Exception as exc:  # noqa: BLE001
            print(f"onnx2tf path failed: {exc}")

    print(
        "\nTFLite conversion not completed automatically.\n"
        "Manual options:\n"
        "  1) pip install ai-edge-torch  then re-run --export-tflite\n"
        "  2) pip install onnx2tf tensorflow  then:\n"
        f"       onnx2tf -i {onnx_path} -o /tmp/tflite_out\n"
        "  3) Deploy ONNX with onnxruntime on edge if TFLite is not required.\n"
    )
    return Path(onnx_path) if onnx_path is not None else output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument(
        "--data",
        type=str,
        default="",
        help="Comma-separated dataset roots (class-folder or flat+manifest)",
    )
    p.add_argument("--epochs", type=int, default=30)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--weight-decay", type=float, default=1e-4)
    p.add_argument("--val-ratio", type=float, default=0.2)
    p.add_argument("--patience", type=int, default=10, help="Early stopping patience (epochs)")
    p.add_argument("--num-workers", type=int, default=4)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--pretrained", type=Path, default=None, help="Warm-start checkpoint")
    p.add_argument(
        "--output",
        type=Path,
        default=ROOT / "models" / "digit_cnn.pth",
        help="Output checkpoint (.pth) or TFLite path when exporting",
    )
    p.add_argument("--export-tflite", type=Path, default=None, help="Export this .pth to TFLite/ONNX")
    p.add_argument("--device", type=str, default="", help="cuda|cpu|mps (default: auto)")
    p.add_argument("--no-amp", action="store_true", help="Disable mixed precision")
    p.add_argument("--augment", action="store_true", default=True, help="Augment training set (default on)")
    p.add_argument("--no-augment", action="store_true", help="Disable train augmentation")
    return p.parse_args(argv)


def resolve_device(name: str) -> torch.device:
    if name:
        return torch.device(name)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.export_tflite is not None:
        # Prefer --output when user passes a .tflite/.onnx path; else sibling of the .pth.
        if args.output.suffix.lower() in {".tflite", ".onnx"}:
            out = args.output
        elif args.output != (ROOT / "models" / "digit_cnn.pth"):
            out = args.output
        else:
            out = Path(args.export_tflite).with_suffix(".tflite")
        if not out.is_absolute():
            out = ROOT / out
        pth = args.export_tflite if args.export_tflite.is_absolute() else ROOT / args.export_tflite
        export_tflite(pth, out)
        return 0

    if not args.data:
        print("ERROR: --data is required for training (or pass --export-tflite)")
        return 2

    device = resolve_device(args.device)
    print(f"Device: {device}")
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    random.seed(args.seed)

    data_roots = [Path(p.strip()) for p in args.data.split(",") if p.strip()]
    data_roots = [p if p.is_absolute() else ROOT / p for p in data_roots]
    print("Discovering samples...")
    samples = discover_samples(data_roots)
    if not samples:
        print("ERROR: no labeled patches found")
        return 1

    label_counts = Counter(lab for _, lab, _ in samples)
    print("Class counts:")
    for i, name in enumerate(CLASS_NAMES):
        print(f"  {name}: {label_counts.get(i, 0)}")

    train_idx, val_idx = session_split(samples, val_ratio=args.val_ratio, seed=args.seed)
    print(f"Split: train={len(train_idx)} val={len(val_idx)} (by session)")

    do_aug = args.augment and not args.no_augment
    train_ds = DigitPatchDataset([samples[i] for i in train_idx], augment=do_aug, seed=args.seed)
    val_ds = DigitPatchDataset([samples[i] for i in val_idx], augment=False, seed=args.seed + 1)

    train_labels = [samples[i][1] for i in train_idx]
    weights = class_weights_from_labels(train_labels)
    weight_map = {CLASS_NAMES[i]: float(weights[i]) for i in range(NUM_CLASSES)}
    print("Class weights:", {k: round(v, 3) for k, v in weight_map.items()})

    # Optional weighted sampler for severe imbalance
    sample_w = [float(weights[lab]) for lab in train_labels]
    sampler = WeightedRandomSampler(sample_w, num_samples=len(sample_w), replacement=True)

    pin = device.type == "cuda"
    train_loader = DataLoader(
        train_ds,
        batch_size=args.batch_size,
        sampler=sampler,
        num_workers=args.num_workers,
        pin_memory=pin,
        drop_last=False,
    )
    val_loader = DataLoader(
        val_ds,
        batch_size=args.batch_size,
        shuffle=False,
        num_workers=args.num_workers,
        pin_memory=pin,
    )

    model = DigitCNN(num_classes=NUM_CLASSES).to(device)
    print(f"Model params: {count_parameters(model):,}")

    if args.pretrained is not None:
        pret = args.pretrained if args.pretrained.is_absolute() else ROOT / args.pretrained
        print(f"Loading pretrained: {pret}")
        ckpt = torch.load(pret, map_location=device, weights_only=False)
        state = ckpt["model_state_dict"] if isinstance(ckpt, dict) and "model_state_dict" in ckpt else ckpt
        model.load_state_dict(state, strict=True)

    criterion = nn.CrossEntropyLoss(weight=weights.to(device))
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(
        optimizer, T_0=max(5, args.epochs // 3), T_mult=2, eta_min=args.lr * 0.01
    )

    use_amp = (not args.no_amp) and device.type == "cuda"
    scaler = torch.cuda.amp.GradScaler() if use_amp else None
    if use_amp:
        print("Mixed precision: enabled (cuda amp)")

    best_acc = -1.0
    best_epoch = -1
    patience_left = args.patience
    out_path = args.output if args.output.is_absolute() else ROOT / args.output

    t0 = time.time()
    for epoch in range(1, args.epochs + 1):
        tr = train_one_epoch(model, train_loader, optimizer, criterion, device, scaler)
        va = evaluate(model, val_loader, device, criterion)
        scheduler.step(epoch)
        lr_now = optimizer.param_groups[0]["lr"]
        print(
            f"Epoch {epoch:03d}/{args.epochs}  "
            f"train_loss={tr['loss']:.4f} train_acc={tr['acc']:.4f}  "
            f"val_loss={va['loss']:.4f} val_acc={va['acc']:.4f}  lr={lr_now:.2e}"
        )
        print("  per-class val acc:")
        for name, stats in va["per_class"].items():
            if stats["support"] == 0:
                continue
            print(f"    {name:>5s}: {stats['acc']:.3f} (n={stats['support']})")
        print("  confusion matrix:")
        print(format_confusion(va["cm"]))

        if va["acc"] > best_acc:
            best_acc = va["acc"]
            best_epoch = epoch
            patience_left = args.patience
            save_checkpoint(out_path, model, epoch, best_acc, args)
        else:
            patience_left -= 1
            print(f"  no improvement (patience left={patience_left})")
            if patience_left <= 0:
                print(f"Early stopping at epoch {epoch} (best={best_epoch}, acc={best_acc:.4f})")
                break

    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s. Best val_acc={best_acc:.4f} @ epoch {best_epoch} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
