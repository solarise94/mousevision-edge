"""LCD screen localization: quad_hint → fixed ROI → HSV quad."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class LocateResult:
    screen_quad: list[tuple[float, float]]
    confidence: float
    method: str  # quad_hint | fixed_roi | hsv_quad
    orientation: str  # upright | invalid


def _order_quad(pts: np.ndarray) -> list[tuple[float, float]]:
    """Order 4 points as TL, TR, BR, BL."""
    pts = np.asarray(pts, dtype=np.float32).reshape(4, 2)
    s = pts.sum(axis=1)
    diff = np.diff(pts, axis=1).reshape(-1)
    tl = pts[int(np.argmin(s))]
    br = pts[int(np.argmax(s))]
    tr = pts[int(np.argmin(diff))]
    bl = pts[int(np.argmax(diff))]
    return [
        (float(tl[0]), float(tl[1])),
        (float(tr[0]), float(tr[1])),
        (float(br[0]), float(br[1])),
        (float(bl[0]), float(bl[1])),
    ]


def _quad_from_rect(x: float, y: float, w: float, h: float) -> list[tuple[float, float]]:
    return [
        (x, y),
        (x + w, y),
        (x + w, y + h),
        (x, y + h),
    ]


def quad_to_bbox(quad: list[tuple[float, float]] | list[list[float]]) -> dict[str, int]:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    x0, y0 = int(min(xs)), int(min(ys))
    x1, y1 = int(max(xs)), int(max(ys))
    return {"x": x0, "y": y0, "w": max(1, x1 - x0), "h": max(1, y1 - y0)}


def _quad_area(quad: list[tuple[float, float]]) -> float:
    pts = np.asarray(quad, dtype=np.float32)
    return float(abs(cv2.contourArea(pts)))


def _quad_aspect(quad: list[tuple[float, float]]) -> float:
    (tl, tr, br, bl) = quad
    top = float(np.linalg.norm(np.array(tr) - np.array(tl)))
    bottom = float(np.linalg.norm(np.array(br) - np.array(bl)))
    left = float(np.linalg.norm(np.array(bl) - np.array(tl)))
    right = float(np.linalg.norm(np.array(br) - np.array(tr)))
    width = max(1e-3, 0.5 * (top + bottom))
    height = max(1e-3, 0.5 * (left + right))
    return width / height


def _blue_coverage(
    image: np.ndarray,
    quad: list[tuple[float, float]],
    *,
    hsv_low: tuple[int, int, int],
    hsv_high: tuple[int, int, int],
) -> float:
    h, w = image.shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    pts = np.asarray(quad, dtype=np.int32).reshape(-1, 1, 2)
    cv2.fillConvexPoly(mask, pts, 255)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, np.array(hsv_low), np.array(hsv_high))
    inside = cv2.bitwise_and(blue, blue, mask=mask)
    area = float(np.count_nonzero(mask))
    if area < 1:
        return 0.0
    return float(np.count_nonzero(inside)) / area


def validate_hint(
    image: np.ndarray,
    hint: list[list[float]] | list[tuple[float, float]],
    *,
    hsv_low: tuple[int, int, int] = (90, 40, 80),
    hsv_high: tuple[int, int, int] = (130, 255, 255),
    min_coverage: float = 0.35,
    min_aspect: float = 2.0,
    max_aspect: float = 8.0,
) -> LocateResult | None:
    """Accept previous-frame quad if it still covers blue LCD reasonably."""
    if hint is None or len(hint) != 4:
        return None
    h, w = image.shape[:2]
    try:
        quad = _order_quad(np.asarray(hint, dtype=np.float32))
    except Exception:  # noqa: BLE001
        return None
    for x, y in quad:
        if x < -20 or y < -20 or x > w + 20 or y > h + 20:
            return None
    aspect = _quad_aspect(quad)
    if aspect < min_aspect or aspect > max_aspect:
        return None
    coverage = _blue_coverage(image, quad, hsv_low=hsv_low, hsv_high=hsv_high)
    if coverage < min_coverage:
        return None
    conf = float(np.clip(0.55 + 0.45 * coverage, 0.0, 0.98))
    return LocateResult(screen_quad=quad, confidence=conf, method="quad_hint", orientation="upright")


def locate_fixed_roi(
    image: np.ndarray,
    roi: dict,
    *,
    expand: float = 0.08,
) -> LocateResult | None:
    h, w = image.shape[:2]
    try:
        x, y, rw, rh = float(roi["x"]), float(roi["y"]), float(roi["w"]), float(roi["h"])
    except (KeyError, TypeError, ValueError):
        return None
    pad_x, pad_y = rw * expand, rh * expand
    x0 = max(0.0, x - pad_x)
    y0 = max(0.0, y - pad_y)
    x1 = min(float(w), x + rw + pad_x)
    y1 = min(float(h), y + rh + pad_y)
    if x1 - x0 < 40 or y1 - y0 < 20:
        return None
    quad = _quad_from_rect(x0, y0, x1 - x0, y1 - y0)
    return LocateResult(
        screen_quad=quad,
        confidence=0.70,
        method="fixed_roi",
        orientation="upright",
    )


def locate_hsv_quad(
    image: np.ndarray,
    *,
    hsv_low: tuple[int, int, int] = (90, 40, 80),
    hsv_high: tuple[int, int, int] = (130, 255, 255),
    min_area: int = 8_000,
    min_width: int = 150,
    min_height: int = 40,
    prefer_axis_bbox: bool = True,
) -> LocateResult | None:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array(hsv_low), np.array(hsv_high))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    best: LocateResult | None = None
    best_score = -1.0
    img_area = float(image.shape[0] * image.shape[1])
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_area:
            continue

        # Default Stage B: axis-aligned bbox. approxPolyDP corners are often
        # unstable (e.g. BR drifts into the button row) and poison digit crops.
        if prefer_axis_bbox:
            x, y, bw, bh = cv2.boundingRect(contour)
            if bw < min_width or bh < min_height:
                continue
            quad = _quad_from_rect(float(x), float(y), float(bw), float(bh))
            method = "hsv_bbox"
        else:
            peri = cv2.arcLength(contour, True)
            approx = cv2.approxPolyDP(contour, 0.04 * peri, True)
            if len(approx) == 4:
                quad = _order_quad(approx.reshape(4, 2))
            else:
                rect = cv2.minAreaRect(contour)
                box = cv2.boxPoints(rect)
                quad = _order_quad(box)
            method = "hsv_quad"

        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        bw = max(xs) - min(xs)
        bh = max(ys) - min(ys)
        if bw < min_width or bh < min_height:
            continue
        aspect = _quad_aspect(quad)
        if aspect < 2.0 or aspect > 8.0:
            continue
        coverage = _blue_coverage(image, quad, hsv_low=hsv_low, hsv_high=hsv_high)
        extent = area / max(1.0, img_area)
        score = coverage * 2.0 + min(1.0, extent * 20.0) + min(1.0, aspect / 4.0)
        if score > best_score:
            best_score = score
            conf = float(np.clip(0.50 + 0.50 * coverage, 0.0, 0.99))
            best = LocateResult(
                screen_quad=quad,
                confidence=conf,
                method=method,
                orientation="upright",
            )
    return best


def _quad_center(quad: list[tuple[float, float]] | list[list[float]]) -> tuple[float, float]:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return (sum(xs) / 4.0, sum(ys) / 4.0)


def _quad_wh(quad: list[tuple[float, float]] | list[list[float]]) -> tuple[float, float]:
    xs = [float(p[0]) for p in quad]
    ys = [float(p[1]) for p in quad]
    return (max(xs) - min(xs), max(ys) - min(ys))


def _hint_diverges_from_hsv(
    hinted: LocateResult,
    hsv: LocateResult,
    *,
    image_shape: tuple[int, ...],
    max_center_frac: float = 0.06,
    max_size_ratio: float = 1.35,
) -> bool:
    """True when sticky hint drifted away from a fresh HSV locate."""
    h, w = int(image_shape[0]), int(image_shape[1])
    diag = float(np.hypot(h, w))
    hc = _quad_center(hinted.screen_quad)
    ec = _quad_center(hsv.screen_quad)
    dist = float(np.hypot(hc[0] - ec[0], hc[1] - ec[1]))
    if dist > max(25.0, max_center_frac * diag):
        return True
    hw, hh = _quad_wh(hinted.screen_quad)
    ew, eh = _quad_wh(hsv.screen_quad)
    if hw <= 1 or hh <= 1 or ew <= 1 or eh <= 1:
        return True
    wr = max(hw / ew, ew / hw)
    hr = max(hh / eh, eh / hh)
    return wr > max_size_ratio or hr > max_size_ratio


def locate_screen(
    image: np.ndarray,
    *,
    quad_hint: list[list[float]] | None = None,
    fixed_roi: dict | None = None,
    hsv_low: tuple[int, int, int] = (90, 40, 80),
    hsv_high: tuple[int, int, int] = (130, 255, 255),
    min_area: int = 8_000,
    min_width: int = 150,
    min_height: int = 40,
    min_locator_confidence: float = 0.55,
) -> LocateResult | None:
    """Formal fallback: compare hint vs HSV when both exist; prefer HSV on drift."""
    hsv = locate_hsv_quad(
        image,
        hsv_low=hsv_low,
        hsv_high=hsv_high,
        min_area=min_area,
        min_width=min_width,
        min_height=min_height,
    )

    if quad_hint is not None:
        hinted = validate_hint(image, quad_hint, hsv_low=hsv_low, hsv_high=hsv_high)
        if hinted is not None and hinted.confidence >= min_locator_confidence:
            if (
                hsv is not None
                and hsv.confidence >= min_locator_confidence
                and _hint_diverges_from_hsv(hinted, hsv, image_shape=image.shape)
            ):
                return hsv
            return hinted

    if fixed_roi is not None:
        fixed = locate_fixed_roi(image, fixed_roi)
        if fixed is not None:
            if hsv is not None and hsv.confidence >= min_locator_confidence:
                return hsv
            return fixed

    return hsv
