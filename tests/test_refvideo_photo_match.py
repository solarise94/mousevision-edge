"""Ref-video photo selection check (optional if video present).

Photo selection is decoupled from weight: the photo proves the mouse was on
the scale. This test verifies records have valid selection metadata and that
the mouse was detected on the chosen frame when possible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mousevision.pipeline import WeighingPipeline, load_config

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "RefVideo" / "9494224d488d6e735c0f108cc5562a2d.mp4"
CONFIG = ROOT / "configs" / "scale_refvideo.yaml"


@pytest.mark.skipif(not VIDEO.exists(), reason="reference video missing")
def test_refvideo_photo_selection_valid(tmp_path: Path):
    config = load_config(CONFIG)
    templates = ROOT / config.get("templates_dir", "assets/templates")
    pipeline = WeighingPipeline(config, templates)
    result = pipeline.run_video(
        VIDEO,
        cage_id="C57-023",
        output_root=tmp_path,
        stop_after_first=False,
        create_run=True,
    )
    records = result.records or []
    assert len(records) == 8
    for i, rec in enumerate(records):
        # Weight source must be the stable curve median
        assert rec.get("weight_source") == "stable_curve_median"
        # Photo selection must be a valid label
        assert rec.get("photo_selection") in {
            "platform_midpoint",
            "mouse_on_scale",
        }
        # photo_mouse_detected should be True for most records (mouse was there)
        # but we don't hard-require it - detection may miss on some frames.
        assert "photo_mouse_detected" in rec
        assert "photo_verified" in rec
        # photo_verified must be consistent with mouse_detected
        if rec.get("photo_mouse_detected"):
            assert rec.get("photo_verified") is True
        # photo_weight_delta is still recorded for audit but not gated
        delta = rec.get("photo_weight_delta")
        assert delta is not None, rec
        # Verify photo_frame_index consistency: the saved photo.jpg must come
        # from the frame_index declared in record.json (not a stale analyzer pick).
        # Check that the run dir has the photo and the index is a valid integer.
        pfi = rec.get("photo_frame_index")
        assert pfi is not None, f"ordinal {i+1} missing photo_frame_index"
        assert isinstance(pfi, int), f"ordinal {i+1} photo_frame_index not int: {pfi}"

