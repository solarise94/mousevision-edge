"""Tests for the photo thumbnail compression service."""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np

from mousevision.run import create_run_dir, finish_run
from ui.app import _serve_photo


def _make_photo(path: Path, w: int = 720, h: int = 1280) -> None:
    """Create a test JPEG at the given path."""
    img = np.random.randint(0, 256, (h, w, 3), dtype=np.uint8)
    cv2.imwrite(str(path), img, [int(cv2.IMWRITE_JPEG_QUALITY), 95])


def test_serve_photo_full_returns_original(tmp_path, monkeypatch):
    """size=full should return the original file unchanged."""
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path))
    # Re-import to pick up the new DEFAULT_OUTPUT
    import importlib
    import ui.app as appmod
    importlib.reload(appmod)

    photo = tmp_path / "photo.jpg"
    _make_photo(photo, 400, 300)
    original_size = photo.stat().st_size

    resp = appmod._serve_photo(photo, "full")
    assert resp.headers.get("cache-control") == "max-age=31536000, immutable"
    # FileResponse serves the original path
    assert hasattr(resp, "path")


def test_serve_photo_thumb_creates_cache(tmp_path, monkeypatch):
    """size=thumb should create a cached thumbnail in .thumbs/."""
    import importlib
    import ui.app as appmod
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path))
    importlib.reload(appmod)

    photo = tmp_path / "photo.jpg"
    _make_photo(photo, 720, 1280)  # 150KB+ original
    original_size = photo.stat().st_size

    resp = appmod._serve_photo(photo, "thumb")
    thumb_dir = tmp_path / ".thumbs"
    assert thumb_dir.exists()
    thumbs = list(thumb_dir.glob("*.jpg"))
    assert len(thumbs) == 1
    thumb_size = thumbs[0].stat().st_size
    # Thumbnail must be smaller than original
    assert thumb_size < original_size
    # Thumbnail should be 320px wide
    thumb_img = cv2.imread(str(thumbs[0]))
    assert thumb_img is not None
    assert thumb_img.shape[1] == 320
    # Response has immutable cache header + ETag
    assert "immutable" in resp.headers.get("cache-control", "")
    assert resp.headers.get("etag") is not None


def test_serve_photo_thumb_cache_hit(tmp_path, monkeypatch):
    """Second request with same mtime should reuse cached file (no re-encode)."""
    import importlib
    import ui.app as appmod
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path))
    importlib.reload(appmod)

    photo = tmp_path / "photo.jpg"
    _make_photo(photo, 720, 1280)

    # First call - generates cache
    appmod._serve_photo(photo, "thumb")
    thumb_dir = tmp_path / ".thumbs"
    cached_files = list(thumb_dir.glob("*.jpg"))
    assert len(cached_files) == 1
    first_mtime = cached_files[0].stat().st_mtime_ns

    # Second call - should reuse (same file, same mtime)
    appmod._serve_photo(photo, "thumb")
    cached_files_2 = list(thumb_dir.glob("*.jpg"))
    assert len(cached_files_2) == 1  # no new file created
    # The cached file was not rewritten (mtime unchanged)
    assert cached_files_2[0].stat().st_mtime_ns == first_mtime


def test_serve_photo_404(tmp_path, monkeypatch):
    """Missing photo should raise 404."""
    import importlib
    import ui.app as appmod
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path))
    importlib.reload(appmod)

    from fastapi import HTTPException
    try:
        appmod._serve_photo(tmp_path / "nonexistent.jpg", "thumb")
        assert False, "should have raised"
    except HTTPException as e:
        assert e.status_code == 404


def test_serve_photo_thumb_small_image_no_upscale(tmp_path, monkeypatch):
    """Images smaller than 320px should not be upscaled."""
    import importlib
    import ui.app as appmod
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path))
    importlib.reload(appmod)

    photo = tmp_path / "small.jpg"
    _make_photo(photo, 200, 150)  # smaller than 320
    appmod._serve_photo(photo, "thumb")
    thumb_dir = tmp_path / ".thumbs"
    thumbs = list(thumb_dir.glob("*.jpg"))
    assert len(thumbs) == 1
    thumb_img = cv2.imread(str(thumbs[0]))
    # Should remain at original width (no upscale)
    assert thumb_img.shape[1] == 200
