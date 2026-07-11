"""Template reader tests against extracted RefVideo frames."""

from pathlib import Path

import cv2
import pytest

from mousevision.reader.template import TemplateReader, find_lcd_box

ROOT = Path(__file__).resolve().parents[1]
FRAMES = ROOT / "tmp_frames"
TEMPLATES = ROOT / "assets" / "templates"


@pytest.fixture(scope="module")
def reader() -> TemplateReader:
    if not TEMPLATES.exists() or not any(TEMPLATES.glob("*.png")):
        pytest.skip("templates not built yet")
    return TemplateReader(TEMPLATES, match_threshold=0.45, min_digit_confidence=0.40)


def test_find_lcd_on_empty_frame():
    path = FRAMES / "frame_001.jpg"
    if not path.exists():
        pytest.skip("tmp_frames missing")
    img = cv2.imread(str(path))
    box = find_lcd_box(img)
    assert box is not None
    assert box.w > 200 and box.h > 50


@pytest.mark.parametrize(
    "frame_name,expected,tol",
    [
        ("frame_001.jpg", 0.00, 0.05),
        ("frame_044.jpg", 0.00, 0.05),
    ],
)
def test_read_zero(reader: TemplateReader, frame_name: str, expected: float, tol: float):
    path = FRAMES / frame_name
    if not path.exists():
        pytest.skip("tmp_frames missing")
    img = cv2.imread(str(path))
    weight, conf = reader.read_weight(img)
    assert weight is not None, f"unreadable {frame_name} conf={conf}"
    assert abs(weight - expected) <= tol


@pytest.mark.parametrize(
    "frame_name,lo,hi",
    [
        ("frame_010.jpg", 4.0, 5.5),
        ("frame_020.jpg", 14.0, 16.5),
        ("frame_030.jpg", 16.0, 19.0),
        ("frame_040.jpg", 15.5, 18.5),
    ],
)
def test_read_nonzero_range(reader: TemplateReader, frame_name: str, lo: float, hi: float):
    path = FRAMES / frame_name
    if not path.exists():
        pytest.skip("tmp_frames missing")
    img = cv2.imread(str(path))
    weight, conf = reader.read_weight(img)
    assert weight is not None, f"unreadable {frame_name} conf={conf}"
    assert lo <= weight <= hi, f"{frame_name}: got {weight}"
