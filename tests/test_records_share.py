"""Tests for POST /api/records/share (public data-sharing, local edition opt-in).

The endpoint mirrors /api/records/report's multipart shape but:
- requires the independent MOUSEVISION_SHARE_TOKEN (not the lab API token);
- persists into <output_root>/shared/ (isolated area);
- does NOT write to the registry / records_meta / upload_queue, so shared
  data is fully separated from lab records.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SHARE_TOKEN = "share-secret"
_API_TOKEN = "lab-secret"


@pytest.fixture()
def make_client(tmp_path: Path, monkeypatch):
    """Factory: returns a configured TestClient + app_mod.

    ``share_env=None`` un-sets MOUSEVISION_SHARE_TOKEN (channel disabled).
    The lab API token is always set so it stays available for contrast.
    """
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", _API_TOKEN)

    import ui.app as app_mod

    def _build(share_env: str | None = _SHARE_TOKEN):
        if share_env is None:
            monkeypatch.delenv("MOUSEVISION_SHARE_TOKEN", raising=False)
        else:
            monkeypatch.setenv("MOUSEVISION_SHARE_TOKEN", share_env)
        importlib.reload(app_mod)
        return app_mod

    return _build


def _records_payload(n: int = 2, *, prefix: str = "shr") -> list[dict]:
    out = []
    for i in range(1, n + 1):
        out.append({"record_id": f"{prefix}-{i:03d}", "ordinal": i, "weight_g": round(20.0 + i, 2)})
    return out


def _headers(token: str = _SHARE_TOKEN) -> dict[str, str]:
    return {"X-MouseVision-Token": token}


def _post_share(client, records, *, token=_SHARE_TOKEN, app_version=None, files=None):
    data = {"cage_id": "SHARE-01", "project_id": "default", "device_id": "phone-share"}
    if app_version is not None:
        data["app_version"] = app_version
    data["records"] = json.dumps(records)
    headers = _headers(token) if token else {}
    return client.post(
        "/api/records/share",
        data=data,
        files=files or None,
        headers=headers,
    )


# --------------------------------------------------------------------------- #
# 1. Auth: wrong token -> 401; unset env -> 403.
# --------------------------------------------------------------------------- #


def test_share_wrong_token_rejected(make_client):
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        res = _post_share(c, _records_payload(1), token="nope")
        assert res.status_code == 401, res.text


def test_share_no_token_header_rejected(make_client):
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        res = _post_share(c, _records_payload(1), token=None)
        assert res.status_code == 401, res.text


def test_share_unconfigured_env_rejected(make_client):
    app_mod = make_client(share_env=None)
    with TestClient(app_mod.app) as c:
        # even a "correct-looking" token must be rejected: channel is disabled
        res = _post_share(c, _records_payload(1), token=_SHARE_TOKEN)
        assert res.status_code == 403, res.text


def test_share_uses_independent_token_not_lab_token(make_client):
    """The lab API token must NOT grant access to the share endpoint."""
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        res = _post_share(c, _records_payload(1), token=_API_TOKEN)
        assert res.status_code == 401, res.text


# --------------------------------------------------------------------------- #
# 2. Normal share -> lands in <output_root>/shared/, isolated from registry.
# --------------------------------------------------------------------------- #


def test_share_persists_into_shared_area(make_client):
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        res = _post_share(c, _records_payload(2), app_version="0.3.0")
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["ok"] is True
        assert body["count"] == 2
        assert len(body["record_ids"]) == 2

        run_dir = Path(app_mod.DEFAULT_OUTPUT) / "shared" / body["run_dir"]
        assert run_dir.is_dir()
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["record_count"] == 2
        assert manifest["mode"] == "public_share"
        assert manifest["weight_source"] == "public_share"
        assert manifest["shared"] is True
        assert manifest["app_version"] == "0.3.0"
        assert manifest["status"] == "public_share"

        for i in range(1, 3):
            mouse_dir = run_dir / f"mouse_{i:03d}"
            assert (mouse_dir / "record.json").is_file()
            assert (mouse_dir / "photo.jpg").is_file()
            rec = json.loads((mouse_dir / "record.json").read_text(encoding="utf-8"))
            assert rec["record_id"] == f"shr-{i:03d}"
            assert rec["weight_source"] == "public_share"


def test_share_run_not_in_registry(make_client):
    """Shared data must never appear in the lab registry / list_runs."""
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        res = _post_share(c, _records_payload(1))
        assert res.status_code == 201, res.text
        runs = app_mod.registry.list_runs()
        assert runs == [], "共享数据不得进入实验室 registry"
        assert app_mod.upload_queue is not None
        assert app_mod.records_meta is not None


def test_share_does_not_pollute_lab_run_dirs(make_client):
    """The <output_root> root (lab area) stays empty; only shared/ is used."""
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        res = _post_share(c, _records_payload(1))
        assert res.status_code == 201
        # No run_* dirs directly under output root.
        lab_runs = [p for p in Path(app_mod.DEFAULT_OUTPUT).glob("run_*") if p.is_dir()]
        assert lab_runs == []
        assert (Path(app_mod.DEFAULT_OUTPUT) / "shared").is_dir()


# --------------------------------------------------------------------------- #
# 3. Photos + video + readings flow through the shared core.
# --------------------------------------------------------------------------- #


def _jpeg_bytes(size: int = 32) -> bytes:
    import cv2
    import numpy as np

    img = np.full((8, 8, 3), 120, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _tiny_video_bytes(n_frames: int = 12, fps: float = 10.0, size=(48, 36)) -> bytes:
    """A tiny but real, decodable mp4 (for share/report video-upload tests)."""
    import cv2
    import numpy as np

    w, h = size
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    buf_path = None
    # 用临时文件写视频再用 cv2 读回字节（VideoWriter 需要文件后缀）。
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        buf_path = tf.name
    writer = cv2.VideoWriter(buf_path, fourcc, fps, (w, h))
    try:
        for i in range(n_frames):
            img = np.full((h, w, 3), (i * 11 + 5) % 250, dtype=np.uint8)
            writer.write(img)
    finally:
        writer.release()
    from pathlib import Path

    return Path(buf_path).read_bytes()


def test_share_with_uploaded_photo(make_client):
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        photo = _jpeg_bytes()
        payload = [{"record_id": "shr-p", "ordinal": 1, "weight_g": 21.0}]
        files = [("photos", ("shr-p.jpg", photo, "image/jpeg"))]
        res = _post_share(c, payload, files=files)
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["photos_uploaded"] == 1
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / "shared" / body["run_dir"]
        p = run_dir / "mouse_001" / "photo.jpg"
        assert p.is_file()
        assert p.read_bytes() == photo
        rec = json.loads((run_dir / "mouse_001" / "record.json").read_text("utf-8"))
        assert rec["photo_source"] == "device_capture"


def test_share_with_readings_persisted(make_client):
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        readings = {
            "device_id": "phone-share",
            "readings": [{"t_ms": 0, "grams": 20.0, "raw": 200, "sequence": 1}],
        }
        files = [
            (
                "readings",
                ("readings.json", json.dumps(readings).encode("utf-8"), "application/json"),
            )
        ]
        res = _post_share(c, _records_payload(1), files=files)
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["readings_saved"] is True
        run_dir = Path(app_mod.DEFAULT_OUTPUT) / "shared" / body["run_dir"]
        assert (run_dir / "readings.json").is_file()


# --------------------------------------------------------------------------- #
# 4. Idempotency: duplicate record_ids -> skipped, no new shared run.
# --------------------------------------------------------------------------- #


def test_share_duplicate_record_ids_idempotent(make_client):
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        payload = _records_payload(2)
        first = _post_share(c, payload)
        assert first.status_code == 201
        shared_before = list((Path(app_mod.DEFAULT_OUTPUT) / "shared").glob("run_*"))
        assert len(shared_before) == 1

        second = _post_share(c, payload)
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["count"] == 0
        assert body["run_id"] is None
        assert sorted(body["skipped"]) == sorted(first.json()["record_ids"])

        shared_after = list((Path(app_mod.DEFAULT_OUTPUT) / "shared").glob("run_*"))
        assert len(shared_after) == 1


def test_share_idempotency_is_isolated_from_lab_report(make_client):
    """A record uploaded to /api/records/report must NOT be treated as already
    present in the share area (different storage areas)."""
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        payload = [{"record_id": "cross-1", "ordinal": 1, "weight_g": 21.0}]
        # Lab report first
        lab = c.post(
            "/api/records/report",
            data={"cage_id": "LAB-01", "records": json.dumps(payload)},
            headers={"X-MouseVision-Token": _API_TOKEN},
        )
        assert lab.status_code == 201, lab.text

        # Same record_id to share -> not skipped, lands in shared area.
        shr = _post_share(c, payload)
        assert shr.status_code == 201, shr.text
        assert shr.json()["count"] == 1
        assert shr.json()["skipped"] == []


# --------------------------------------------------------------------------- #
# 5. Regression: 带 video 共享时，shared/ 目录预先不存在 → 仍成功（曾 500）。
# --------------------------------------------------------------------------- #


def test_share_with_video_when_shared_dir_missing(make_client, tmp_path):
    """Bug 复现：新部署首次「带视频共享」必 500。

    根因：persist_report_records 在 create_run_dir（负责 mkdir）之前就向
    ``output_root/.report_upload_*`` 写临时视频；share 端点的 output_root 是
    ``<root>/shared/``，新部署时该目录尚不存在 → FileNotFoundError → 500。
    修复：写临时视频前先 ``output_root.mkdir(parents=True, exist_ok=True)``。
    """
    app_mod = make_client(_SHARE_TOKEN)
    # 显式断言 precondition：shared/ 目录尚未存在（模拟新部署）。
    shared_root = Path(app_mod.DEFAULT_OUTPUT) / "shared"
    assert not shared_root.exists(), "precondition: shared/ 不应预存在"

    video_bytes = _tiny_video_bytes()

    with TestClient(app_mod.app) as c:
        payload = [{"record_id": "shr-vid-1", "ordinal": 1, "weight_g": 21.0}]
        files = [("video", ("v.mp4", video_bytes, "video/mp4"))]
        res = _post_share(c, payload, files=files)
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["ok"] is True
        assert body["count"] == 1

        # run 落在 shared/ 下，证据视频落到 run 目录里。
        run_dir = shared_root / body["run_dir"]
        assert run_dir.is_dir()
        assert (run_dir / "source.mp4").is_file()
        # 无残留 .report_upload_* 临时文件。
        leftovers = list(shared_root.glob(".report_upload_*"))
        assert leftovers == [], f"残留临时视频文件: {leftovers}"


def test_share_with_video_then_idempotent_rereport_no_temp_leak(make_client, tmp_path):
    """带视频共享成功后，重复上报同 record_id（幂等全跳过）不应残留临时文件，
    也不应在 shared/ 下残留 .report_upload_*。"""
    app_mod = make_client(_SHARE_TOKEN)
    shared_root = Path(app_mod.DEFAULT_OUTPUT) / "shared"
    video_bytes = _tiny_video_bytes()

    with TestClient(app_mod.app) as c:
        payload = [{"record_id": "shr-vid-2", "ordinal": 1, "weight_g": 21.0}]
        files = [("video", ("v.mp4", video_bytes, "video/mp4"))]
        first = _post_share(c, payload, files=files)
        assert first.status_code == 201

        # 重复上报（带同一 video）→ 全部跳过，不写 run；video 应被 drain 掉，
        # 不应在 shared/ 下残留 .report_upload_*。
        second = _post_share(c, payload, files=files)
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["count"] == 0
        assert body["run_id"] is None

        leftovers = list(shared_root.glob(".report_upload_*"))
        assert leftovers == [], f"残留临时视频文件: {leftovers}"


def test_share_does_not_touch_boxes_table(make_client):
    """即使 report_api.configure 传入了 boxes_store，share 端点也不推进箱子编号。

    共享数据与实验室箱子编号完全隔离：共享记录上报后 boxes 表的 next_ordinal
    不应被推进。
    """
    app_mod = make_client(_SHARE_TOKEN)
    with TestClient(app_mod.app) as c:
        # 预先建一个箱子，记录其 next_ordinal。
        cage = "SHARE-BOX-01"
        app_mod.box_registry.create(cage_id=cage)
        before = app_mod.box_registry.get(cage)
        assert before is not None
        next_before = before["next_ordinal"]

        # 共享上报含 ordinal [1,2] 的记录。
        payload = [
            {"record_id": f"shr-box-{i}", "ordinal": i, "weight_g": 20.0 + i}
            for i in (1, 2)
        ]
        res = _post_share(c, payload)
        assert res.status_code == 201, res.text

        # boxes 表的 next_ordinal 未变（共享不应推进箱子编号）。
        after = app_mod.box_registry.get(cage)
        assert after is not None
        assert after["next_ordinal"] == next_before
