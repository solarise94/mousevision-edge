"""Build digit templates and ROI previews from a reference video / frames."""

from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import yaml

from mousevision.reader.template import (
    TemplateReader,
    _binarize_digits,
    _digit_area_gray,
    _normalize_digit,
    _projection_slots,
    decode_seven_seg,
    find_lcd_box,
)


def _load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _iter_images(video: Path | None, frames_dir: Path | None, stride: int):
    if frames_dir is not None:
        for path in sorted(frames_dir.glob("*.jpg")) + sorted(frames_dir.glob("*.png")):
            img = cv2.imread(str(path))
            if img is not None:
                yield path.stem, img
        return
    if video is None:
        raise SystemExit("Provide --video or --frames-dir")
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise SystemExit(f"Cannot open {video}")
    idx = 0
    while True:
        ok, img = cap.read()
        if not ok:
            break
        if idx % stride == 0:
            yield f"frame_{idx:05d}", img
        idx += 1
    cap.release()


def _synthetic_seven_seg_templates(size: tuple[int, int] = (28, 40)) -> dict[str, np.ndarray]:
    segments = {
        "0": "abcdef",
        "1": "bc",
        "2": "abdeg",
        "3": "abcdg",
        "4": "bcfg",
        "5": "acdfg",
        "6": "acdefg",
        "7": "abc",
        "8": "abcdefg",
        "9": "abcdfg",
    }
    tw, th = size
    templates: dict[str, np.ndarray] = {}
    for digit, segs in segments.items():
        canvas = np.zeros((th, tw), dtype=np.uint8)
        t = max(2, th // 12)
        if "a" in segs:
            cv2.rectangle(canvas, (4, 2), (tw - 5, 2 + t), 255, -1)
        if "b" in segs:
            cv2.rectangle(canvas, (tw - 5 - t, 4), (tw - 4, th // 2 - 1), 255, -1)
        if "c" in segs:
            cv2.rectangle(canvas, (tw - 5 - t, th // 2 + 1), (tw - 4, th - 5), 255, -1)
        if "d" in segs:
            cv2.rectangle(canvas, (4, th - 3 - t), (tw - 5, th - 3), 255, -1)
        if "e" in segs:
            cv2.rectangle(canvas, (3, th // 2 + 1), (3 + t, th - 5), 255, -1)
        if "f" in segs:
            cv2.rectangle(canvas, (3, 4), (3 + t, th // 2 - 1), 255, -1)
        if "g" in segs:
            mid = th // 2
            cv2.rectangle(canvas, (4, mid - t // 2), (tw - 5, mid + t // 2), 255, -1)
        templates[digit] = canvas
    templates["dot"] = np.zeros((th, tw), dtype=np.uint8)
    cv2.circle(templates["dot"], (tw // 2, th - 6), 3, 255, -1)
    return templates


def build_templates(images: list[np.ndarray], out_dir: Path) -> dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    synthetic = _synthetic_seven_seg_templates()
    for key, tmpl in synthetic.items():
        cv2.imwrite(str(out_dir / f"{key}.png"), tmpl)

    refined: dict[str, list[np.ndarray]] = defaultdict(list)
    dots: list[np.ndarray] = []

    for image in images:
        box = find_lcd_box(image)
        if box is None:
            continue
        gray = _digit_area_gray(box.crop(image))
        bw = _binarize_digits(gray)
        for a, b in _projection_slots(bw):
            patch = bw[:, a:b]
            rows = np.where(patch.any(axis=1))[0]
            if len(rows):
                patch = patch[rows[0] : rows[-1] + 1, :]
            h = bw.shape[0]
            if patch.shape[0] < h * 0.30 and (b - a) < h * 0.35:
                dots.append(_normalize_digit(patch))
                continue
            char, conf = decode_seven_seg(patch)
            if char.isdigit() and conf >= 0.4:
                refined[char].append(_normalize_digit(patch))

    counts: dict[str, int] = {}
    for key, patches in refined.items():
        stack = np.stack(patches, axis=0).astype(np.float32)
        med = np.median(stack, axis=0)
        _, binary = cv2.threshold(med.astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if np.mean(binary) > 127:
            binary = cv2.bitwise_not(binary)
        cv2.imwrite(str(out_dir / f"{key}.png"), binary)
        counts[key] = len(patches)

    if dots:
        stack = np.stack(dots, axis=0).astype(np.float32)
        med = np.median(stack, axis=0)
        _, binary = cv2.threshold(med.astype(np.uint8), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if np.mean(binary) > 127:
            binary = cv2.bitwise_not(binary)
        cv2.imwrite(str(out_dir / "dot.png"), binary)
        counts["dot"] = len(dots)

    for d in range(10):
        path = out_dir / f"{d}.png"
        if not path.exists():
            cv2.imwrite(str(path), synthetic[str(d)])
            counts[str(d)] = counts.get(str(d), 0)
    if not (out_dir / "dot.png").exists():
        cv2.imwrite(str(out_dir / "dot.png"), synthetic["dot"])
        counts["dot"] = 0
    return counts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Extract ROI preview and digit templates")
    parser.add_argument("--video", type=Path, default=None)
    parser.add_argument("--frames-dir", type=Path, default=None)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--templates-out", type=Path, default=Path("assets/templates"))
    parser.add_argument("--stride", type=int, default=10)
    parser.add_argument("--max-frames", type=int, default=80)
    args = parser.parse_args(argv)

    _ = _load_config(args.config)
    args.out.mkdir(parents=True, exist_ok=True)

    images: list[np.ndarray] = []
    reader = TemplateReader(None, match_threshold=0.4, min_digit_confidence=0.35)
    for i, (name, img) in enumerate(_iter_images(args.video, args.frames_dir, args.stride)):
        if i >= args.max_frames:
            break
        images.append(img)
        box = find_lcd_box(img)
        vis = reader.debug_overlay(img)
        if box is not None:
            gray = _digit_area_gray(box.crop(img))
            bw = _binarize_digits(gray)
            cv2.imwrite(str(args.out / f"{name}_binary.jpg"), bw)
        cv2.imwrite(str(args.out / f"{name}_roi.jpg"), vis)
        weight, conf = reader.read_weight(img)
        print(f"{name}: weight={weight} conf={conf:.3f}")

    counts = build_templates(images, args.templates_out)
    print(f"Wrote templates to {args.templates_out}")
    print("Counts:", dict(sorted(counts.items())))
    print(f"ROI previews: {args.out}")


if __name__ == "__main__":
    main()
