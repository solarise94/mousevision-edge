"""Ref-video photo/weight consistency check (optional if video present)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mousevision.pipeline import WeighingPipeline, load_config

ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / "RefVideo" / "9494224d488d6e735c0f108cc5562a2d.mp4"
CONFIG = ROOT / "configs" / "scale_refvideo.yaml"


@pytest.mark.skipif(not VIDEO.exists(), reason="reference video missing")
def test_refvideo_photo_weight_within_tolerance(tmp_path: Path):
    config = load_config(CONFIG)
    tol = float(config.get("photo_match_tol", 0.02))
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
    for rec in records:
        delta = rec.get("photo_weight_delta")
        assert delta is not None, rec
        assert float(delta) <= tol + 1e-9, (
            f"ordinal={rec.get('ordinal')} weight={rec.get('weight')} "
            f"photo={rec.get('photo_observed_weight')} delta={delta}"
        )
        assert rec.get("photo_selection") in {
            "closest_stable_weight",
            "closest_high_conf",
            "closest_in_platform",
        }
