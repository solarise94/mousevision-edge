"""Generate synthetic seven-segment digit patches for CNN pretraining.

Usage:
    python training/gen_synthetic.py --output-dir training_data/synthetic --count 5000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Canvas size: width=28, height=40 (stored as H×W = 40×28)
WIDTH = 28
HEIGHT = 40

CLASS_NAMES = ["blank", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]

# Standard seven-segment maps (a,b,c,d,e,f,g)
SEGMENT_MAPS: dict[str, tuple[str, ...]] = {
    "blank": (),
    "0": ("a", "b", "c", "d", "e", "f"),
    "1": ("b", "c"),
    "2": ("a", "b", "d", "e", "g"),
    "3": ("a", "b", "c", "d", "g"),
    "4": ("b", "c", "f", "g"),
    "5": ("a", "c", "d", "f", "g"),
    "6": ("a", "c", "d", "e", "f", "g"),
    "7": ("a", "b", "c"),
    "8": ("a", "b", "c", "d", "e", "f", "g"),
    "9": ("a", "b", "c", "d", "f", "g"),
}


def _segment_rects(
    w: int,
    h: int,
    thickness: int,
    jitter_x: int,
    jitter_y: int,
) -> dict[str, tuple[int, int, int, int]]:
    """Return axis-aligned segment boxes (x0, y0, x1, y1) in image coords.

    Layout (normalized to canvas with margin):
         _a_
        |   |
        f   b
        |_g_|
        |   |
        e   c
        |_d_|
    """
    margin_x = max(2, w // 8)
    margin_y = max(2, h // 10)
    t = max(1, thickness)

    left = margin_x + jitter_x
    right = w - margin_x - 1 + jitter_x
    top = margin_y + jitter_y
    bottom = h - margin_y - 1 + jitter_y
    mid_y = (top + bottom) // 2

    # Clamp into canvas
    left = int(np.clip(left, 0, w - 2))
    right = int(np.clip(right, left + 2, w - 1))
    top = int(np.clip(top, 0, h - 2))
    bottom = int(np.clip(bottom, top + 2, h - 1))
    mid_y = int(np.clip(mid_y, top + 1, bottom - 1))

    def hbar(y_center: int) -> tuple[int, int, int, int]:
        y0 = max(0, y_center - t // 2)
        y1 = min(h - 1, y0 + t)
        x0 = left
        x1 = right
        return x0, y0, x1, y1

    def vbar(x_center: int, y0: int, y1: int) -> tuple[int, int, int, int]:
        x0 = max(0, x_center - t // 2)
        x1 = min(w - 1, x0 + t)
        y0 = max(0, y0)
        y1 = min(h - 1, y1)
        return x0, y0, x1, y1

    return {
        "a": hbar(top),
        "d": hbar(bottom),
        "g": hbar(mid_y),
        "f": vbar(left, top + t // 2, mid_y - t // 2),
        "b": vbar(right, top + t // 2, mid_y - t // 2),
        "e": vbar(left, mid_y + t // 2, bottom - t // 2),
        "c": vbar(right, mid_y + t // 2, bottom - t // 2),
    }


def _draw_segment(
    img: np.ndarray,
    rect: tuple[int, int, int, int],
    value: float,
) -> None:
    x0, y0, x1, y1 = rect
    if x1 <= x0 or y1 <= y0:
        return
    img[y0 : y1 + 1, x0 : x1 + 1] = np.clip(
        img[y0 : y1 + 1, x0 : x1 + 1].astype(np.float32) + value,
        0,
        255,
    ).astype(np.uint8)


def render_digit(
    class_name: str,
    rng: np.random.Generator,
    *,
    apply_aug: bool = True,
) -> np.ndarray:
    """Render one synthetic seven-segment digit patch (HEIGHT×WIDTH, uint8)."""
    thickness = int(rng.integers(2, 6)) if apply_aug else 3
    jitter_x = int(rng.integers(-2, 3)) if apply_aug else 0
    jitter_y = int(rng.integers(-2, 3)) if apply_aug else 0
    brightness = float(rng.uniform(180, 255)) if apply_aug else 230.0

    img = np.zeros((HEIGHT, WIDTH), dtype=np.uint8)
    rects = _segment_rects(WIDTH, HEIGHT, thickness, jitter_x, jitter_y)
    active = list(SEGMENT_MAPS[class_name])

    for name in active:
        # Partial segment dropout (~5%) simulates glare removing a segment.
        if apply_aug and rng.random() < 0.05:
            continue
        _draw_segment(img, rects[name], brightness * float(rng.uniform(0.85, 1.0)))

    if not apply_aug:
        return img

    # Slight rotation ±2°
    angle = float(rng.uniform(-2.0, 2.0))
    if abs(angle) > 0.05:
        m = cv2.getRotationMatrix2D((WIDTH / 2.0, HEIGHT / 2.0), angle, 1.0)
        img = cv2.warpAffine(
            img,
            m,
            (WIDTH, HEIGHT),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )

    # Random blur (kernel 1–3; odd sizes only for Gaussian)
    k = int(rng.choice([0, 1, 3]))
    if k >= 3:
        img = cv2.GaussianBlur(img, (k, k), 0)

    # Gaussian noise
    if rng.random() < 0.8:
        noise = rng.normal(0.0, float(rng.uniform(3.0, 18.0)), img.shape)
        img = np.clip(img.astype(np.float32) + noise, 0, 255).astype(np.uint8)

    # Salt & pepper
    if rng.random() < 0.5:
        sp = rng.random(img.shape)
        img = img.copy()
        img[sp < 0.01] = 0
        img[sp > 0.99] = 255

    # Global brightness scale
    scale = float(rng.uniform(0.75, 1.15))
    img = np.clip(img.astype(np.float32) * scale, 0, 255).astype(np.uint8)

    # Optional binarize-ish threshold jitter (LCD patches are often binarized)
    if rng.random() < 0.35:
        thr = int(rng.integers(40, 120))
        img = np.where(img >= thr, img, 0).astype(np.uint8)

    return img


def generate(
    output_dir: Path,
    count_per_class: int,
    seed: int = 42,
) -> dict:
    """Generate balanced synthetic dataset and write PNGs + manifest."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)

    counts: dict[str, int] = {}
    for class_name in CLASS_NAMES:
        class_dir = output_dir / class_name
        class_dir.mkdir(parents=True, exist_ok=True)
        for i in range(count_per_class):
            patch = render_digit(class_name, rng, apply_aug=True)
            path = class_dir / f"{i:05d}.png"
            # Ensure parent exists; write grayscale PNG via OpenCV
            ok = cv2.imwrite(str(path), patch)
            if not ok:
                raise RuntimeError(f"Failed to write {path}")
        counts[class_name] = count_per_class
        print(f"  {class_name}: {count_per_class} images -> {class_dir}")

    manifest = {
        "layout": "class_folder",
        "width": WIDTH,
        "height": HEIGHT,
        "num_classes": len(CLASS_NAMES),
        "class_names": CLASS_NAMES,
        "counts": counts,
        "total": sum(counts.values()),
        "seed": seed,
        "description": "Synthetic seven-segment digit patches for CNN pretraining",
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"Wrote manifest: {manifest_path} (total={manifest['total']})")
    return manifest


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "training_data" / "synthetic",
        help="Output directory for class folders and manifest.json",
    )
    p.add_argument(
        "--count",
        type=int,
        default=5000,
        help="Number of samples per class (balanced)",
    )
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(f"Generating synthetic digits -> {args.output_dir}")
    print(f"  classes={len(CLASS_NAMES)}, count_per_class={args.count}, seed={args.seed}")
    generate(args.output_dir, args.count, seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
