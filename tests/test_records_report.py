"""Tests for POST /api/records/report (device-direct weighing report).

The phone performs weighing judgement locally and POSTs only final
records; the server persists them with the same on-disk layout as
realtime_finalize so they are visible via the registry / collect_records.
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

_TOKEN = "report-secret"


def _write_synthetic_video(
    path: Path, *, n_frames: int = 24, fps: float = 10.0, size=(64, 48)
) -> Path:
    """Write a real, decodable mp4. Each frame a distinct solid color so a
    frame extracted at a given timestamp is unambiguously a real decode."""
    w, h = size
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
    try:
        for i in range(n_frames):
            img = np.full((h, w, 3), (i * 9 + 5) % 250, dtype=np.uint8)
            writer.write(img)
    finally:
        writer.release()
    return path


@pytest.fixture()
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MOUSEVISION_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", _TOKEN)
    import importlib

    import ui.app as app_mod

    importlib.reload(app_mod)
    with TestClient(app_mod.app) as c:
        yield c, app_mod


def _headers(token: str = _TOKEN) -> dict[str, str]:
    return {"X-MouseVision-Token": token}


def _records_payload(n: int = 3, *, with_id: bool = True) -> list[dict]:
    out = []
    for i in range(1, n + 1):
        item = {
            "ordinal": i,
            "weight_g": round(20.0 + i, 2),
        }
        if with_id:
            item["record_id"] = f"rec-{i:03d}"
        out.append(item)
    return out


def _post(
    client: TestClient,
    records,
    *,
    video_bytes=None,
    video_name="v.mp4",
    readings_obj=None,
    photos=None,
    _cage: str = "C57-023",
):
    data = {
        "cage_id": _cage,
        "project_id": "default",
        "device_id": "phone-01",
        "records": json.dumps(records),
    }
    # 统一用 list-of-pairs（TestClient 支持多值文件字段 / 多字段混用）
    files: list = []
    if video_bytes is not None:
        files.append(("video", (video_name, video_bytes, "video/mp4")))
    if readings_obj is not None:
        files.append(
            (
                "readings",
                (
                    "readings.json",
                    json.dumps(readings_obj).encode("utf-8"),
                    "application/json",
                ),
            )
        )
    if photos:
        # photos: list[(record_id, bytes)] → 每个都作为多值字段 photos 追加
        for rid, content in photos:
            files.append(("photos", (f"{rid}.jpg", content, "image/jpeg")))
    return client.post(
        "/api/records/report",
        data=data,
        files=files or None,
        headers=_headers(),
    )


def _jpeg_bytes(size: int = 32) -> bytes:
    """A tiny but real JPEG (JFIF) encoded via cv2 — valid magic bytes."""
    img = np.full((8, 8, 3), 120, dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


def _png_bytes() -> bytes:
    import io

    img = np.full((8, 8, 3), 80, dtype=np.uint8)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


# --------------------------------------------------------------------------- #
# 1. Basic report: 3 records, no video.
# --------------------------------------------------------------------------- #


def test_report_no_video_creates_run_and_records(client):
    c, app_mod = client
    res = _post(c, _records_payload(3))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["count"] == 3
    assert len(body["record_ids"]) == 3
    assert body["photos_extracted"] == 0  # no video -> placeholders
    assert body["skipped"] == []

    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    assert run_dir.is_dir()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["record_count"] == 3
    assert manifest["mode"] == "device_report"
    assert manifest["device_id"] == "phone-01"
    assert manifest["status"] == "device_report"

    for i in range(1, 4):
        mouse_dir = run_dir / f"mouse_{i:03d}"
        assert (mouse_dir / "record.json").is_file()
        assert (mouse_dir / "photo.jpg").is_file()
        rec = json.loads((mouse_dir / "record.json").read_text(encoding="utf-8"))
        assert rec["record_id"] == f"rec-{i:03d}"
        assert rec["ordinal"] == i
        assert rec["weight"] == round(20.0 + i, 2)
        assert rec["weight_source"] == "device_report"
        assert rec["verification_method"] == "设备本地称重上报"
        # photo.jpg is a valid readable JPEG.
        img = cv2.imread(str(mouse_dir / "photo.jpg"))
        assert img is not None


# --------------------------------------------------------------------------- #
# 2. Missing record_id -> server generates uuid.
# --------------------------------------------------------------------------- #


def test_missing_record_id_auto_generated(client):
    c, _ = client
    res = _post(c, _records_payload(2, with_id=False))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["count"] == 2
    for rid in body["record_ids"]:
        # uuid4 hex string
        assert isinstance(rid, str) and len(rid) >= 32


# --------------------------------------------------------------------------- #
# 3. Idempotency: duplicate record_ids -> no new run/mouse dirs.
# --------------------------------------------------------------------------- #


def test_duplicate_record_ids_idempotent(client):
    c, _ = client
    payload = _records_payload(3)

    first = _post(c, payload)
    assert first.status_code == 201

    second = _post(c, payload)
    assert second.status_code == 200
    body = second.json()
    assert body["count"] == 0
    assert body["run_id"] is None
    assert body["run_dir"] is None
    assert sorted(body["skipped"]) == sorted(first.json()["record_ids"])


def test_duplicate_record_ids_does_not_create_extra_run(client, tmp_path):
    c, app_mod = client
    payload = _records_payload(3)
    first = _post(c, payload)
    assert first.status_code == 201

    runs_before = app_mod.registry.list_runs()
    assert len(runs_before) == 1

    second = _post(c, payload)
    assert second.status_code == 200
    assert second.json()["count"] == 0

    runs_after = app_mod.registry.list_runs()
    assert len(runs_after) == 1  # still exactly one run


# --------------------------------------------------------------------------- #
# 4. Invalid weight_g values -> 400.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad_weight",
    [-1.0, float("nan"), 9999.0],
)
def test_invalid_weight_rejected(client, bad_weight):
    c, _ = client
    payload = [{"record_id": "r-bad", "ordinal": 1, "weight_g": bad_weight}]
    res = _post(c, payload)
    assert res.status_code == 400
    assert "weight" in res.json()["detail"].lower()


def test_invalid_ordinal_rejected(client):
    c, _ = client
    payload = [{"record_id": "r", "ordinal": 0, "weight_g": 20.0}]
    res = _post(c, payload)
    assert res.status_code == 400


def test_empty_records_rejected(client):
    c, _ = client
    res = _post(c, [])
    assert res.status_code == 400


def test_bad_records_json_rejected(client):
    c, _ = client
    res = c.post(
        "/api/records/report",
        data={
            "cage_id": "C1",
            "records": "not-json{",
        },
        headers=_headers(),
    )
    assert res.status_code == 400


# --------------------------------------------------------------------------- #
# 5. Auth: no token -> 401.
# --------------------------------------------------------------------------- #


def test_no_token_rejected(client):
    c, _ = client
    res = c.post(
        "/api/records/report",
        data={"cage_id": "C1", "records": "[]"},
    )
    assert res.status_code == 401


def test_wrong_token_rejected(client):
    c, _ = client
    res = c.post(
        "/api/records/report",
        data={"cage_id": "C1", "records": "[]"},
        headers={"X-MouseVision-Token": "nope"},
    )
    assert res.status_code == 401


# --------------------------------------------------------------------------- #
# 6. With video + clip_start_ms -> photo.jpg is a real decoded frame.
# --------------------------------------------------------------------------- #


def test_report_with_video_extracts_real_frame(client, tmp_path):
    c, app_mod = client
    video_file = tmp_path / "weigh.mp4"
    _write_synthetic_video(video_file, n_frames=30, fps=10.0, size=(96, 72))
    video_bytes = video_file.read_bytes()

    payload = [
        {
            "record_id": "rec-v1",
            "ordinal": 1,
            "weight_g": 23.5,
            "clip_start_ms": 1500,  # 1.5s into the 3s video
        }
    ]
    res = _post(c, payload, video_bytes=video_bytes, video_name="weigh.mp4")
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["photos_extracted"] == 1

    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    photo = run_dir / "mouse_001" / "photo.jpg"
    assert photo.is_file()
    img = cv2.imread(str(photo))
    assert img is not None
    # Real frame decoded from a 96x72 video: dimensions match the source.
    assert img.shape[0] == 72 and img.shape[1] == 96
    # The evidence video is stored at run scope.
    assert (run_dir / "source.mp4").is_file()
    rec = json.loads((run_dir / "mouse_001" / "record.json").read_text("utf-8"))
    assert rec.get("evidence_video") == "source.mp4"


def test_report_with_video_no_clip_uses_midpoint(client, tmp_path):
    c, app_mod = client
    video_file = tmp_path / "weigh2.mp4"
    _write_synthetic_video(video_file, n_frames=20, fps=10.0, size=(48, 36))
    res = _post(
        c,
        [{"record_id": "rec-v2", "ordinal": 1, "weight_g": 19.0}],
        video_bytes=video_file.read_bytes(),
        video_name="weigh2.mp4",
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["photos_extracted"] == 1
    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    img = cv2.imread(str(run_dir / "mouse_001" / "photo.jpg"))
    assert img is not None
    assert img.shape[:2] == (36, 48)  # decoded from the source video


# --------------------------------------------------------------------------- #
# 7. Records are visible via collect_records / list_runs.
# --------------------------------------------------------------------------- #


def test_reported_records_visible_via_collect_records(client):
    c, app_mod = client
    res = _post(c, _records_payload(2))
    assert res.status_code == 201

    runs = app_mod.registry.list_runs()
    assert any(r["mode"] == "device_report" for r in runs)
    device_run = next(r for r in runs if r["mode"] == "device_report")

    from ui.records_api import collect_records

    records = collect_records(
        app_mod.registry,
        app_mod.records_meta,
        Path(app_mod.DEFAULT_OUTPUT),
        cage_id="C57-023",
    )
    rids = {r["record_id"] for r in records}
    assert {"rec-001", "rec-002"} <= rids
    # Each visible record carries the device-reported weight.
    by_id = {r["record_id"]: r for r in records}
    assert by_id["rec-001"]["weight"] == 21.0
    assert by_id["rec-002"]["weight"] == 22.0
    # Status defaults to pending (records_meta lazy init).
    assert by_id["rec-001"]["status"] == "pending"


def test_reported_records_visible_via_list_mice(client):
    c, app_mod = client
    res = _post(c, _records_payload(2))
    assert res.status_code == 201
    run_id = res.json()["run_id"]

    mice = app_mod.registry.list_mice(run_id=run_id)
    assert len(mice) == 2
    ordinals = sorted(int(m["ordinal"]) for m in mice)
    assert ordinals == [1, 2]


# --------------------------------------------------------------------------- #
# 10. 上报记录在箱详情（GET /api/boxes/{cage}/records）可见。
# Bug：persist_report_records 只写 registry 磁盘 + upload_queue，从不写 job_store；
# 而箱详情此前只遍历 job_store.list_by_cage → 上报记录对不上 job → items 为空
# （"本箱还没有记录"）。修复：api_box_records 追加扫描 registry 补出有 run 但无 job
# 的上报记录。
# --------------------------------------------------------------------------- #


def test_report_records_visible_in_box_records(client):
    c, app_mod = client
    cage = "C57-023"
    # 上报 3 条不同 ordinal 的记录（含 record_id）
    res = _post(c, _records_payload(3), _cage=cage)
    assert res.status_code == 201, res.text

    # 上报路径确实不写 job_store（与视频分析路径区分），确认没有对应 job。
    assert app_mod.job_store.list_by_cage(cage) == []

    box_res = c.get(f"/api/boxes/{cage}/records", headers=_headers())
    assert box_res.status_code == 200, box_res.text
    items = box_res.json()["items"]
    # 修复前为 0 条；修复后应能列出全部 3 条 completed 上报记录。
    assert len(items) == 3
    completed = [i for i in items if i.get("status") == "completed" and i.get("record_id")]
    assert len(completed) == 3
    rids = sorted(i["record_id"] for i in completed)
    assert rids == ["rec-001", "rec-002", "rec-003"]
    # 排序按 ordinal 升序（合成 item 的 actual_ordinal 来自 mouse，能正常排序）。
    ordinals = [int(i["actual_ordinal"]) for i in items]
    assert ordinals == sorted(ordinals)


# --------------------------------------------------------------------------- #
# Extra: same-batch duplicate record_id is deduped within the request.
# --------------------------------------------------------------------------- #


def test_same_batch_duplicate_record_id_deduped(client):
    c, app_mod = client
    payload = [
        {"record_id": "dup-1", "ordinal": 1, "weight_g": 20.0},
        {"record_id": "dup-1", "ordinal": 2, "weight_g": 21.0},  # dup -> skipped
        {"record_id": "dup-2", "ordinal": 3, "weight_g": 22.0},
    ]
    res = _post(c, payload)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["count"] == 2
    assert body["record_ids"] == ["dup-1", "dup-2"]


# --------------------------------------------------------------------------- #
# Extra: clip_start_ms / clip_end_ms / weight_raw are persisted.
# --------------------------------------------------------------------------- #


def test_optional_fields_persisted(client):
    c, app_mod = client
    payload = [
        {
            "record_id": "rec-opt",
            "ordinal": 1,
            "weight_g": 21.5,
            "weight_raw": [21.4, 21.6],
            "clip_start_ms": 1000.0,
            "clip_end_ms": 3000.0,
            "recorded_at": "2026-08-03T10:00:00",
        }
    ]
    res = _post(c, payload)
    assert res.status_code == 201, res.text
    run_dir = Path(app_mod.DEFAULT_OUTPUT) / res.json()["run_dir"]
    rec = json.loads((run_dir / "mouse_001" / "record.json").read_text("utf-8"))
    assert rec["weight_raw"] == [21.4, 21.6]
    assert rec["clip_start_ms"] == 1000.0
    assert rec["clip_end_ms"] == 3000.0
    assert rec["timestamp"] == "2026-08-03T10:00:00"


# --------------------------------------------------------------------------- #
# dev readings：天平读数时间序列随记录上报（dev 模式采集，供训练识别模型）
# --------------------------------------------------------------------------- #


def _readings_payload(n: int = 3) -> dict:
    """Build a valid readings payload (same shape as H5 getReadingsPayload)."""
    readings = []
    for i in range(n):
        readings.append(
            {
                "t_ms": 100 * i,
                "grams": 20.0 + i,
                "raw": 200 + i,
                "sequence": i + 1,
                "rssi": -60 - i,
                "stable": i % 2 == 0,
                "receivedAtEpochMs": 1000 + 100 * i,
            }
        )
    return {
        "device_id": "phone-01",
        "started_at_epoch_ms": 1000,
        "app": "h5-dev-collect",
        "engine_config": {"stable_min_span_ms": 800},
        "readings": readings,
    }


def test_report_with_valid_readings_persisted(client):
    c, app_mod = client
    payload = _readings_payload(3)
    res = _post(c, _records_payload(2), readings_obj=payload)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["readings_saved"] is True

    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    readings_file = run_dir / "readings.json"
    assert readings_file.is_file(), "readings.json 应落盘到 run_dir"
    saved = json.loads(readings_file.read_text(encoding="utf-8"))
    assert saved["app"] == "h5-dev-collect"
    assert saved["device_id"] == "phone-01"
    assert len(saved["readings"]) == 3
    assert saved["readings"][0]["grams"] == 20.0

    # manifest 标注 readings_file
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["readings_file"] == "readings.json"


def test_report_with_invalid_readings_json_skipped_not_fatal(client):
    c, app_mod = client
    # 直接构造非法 JSON body（绕过 _post 的 json.dumps，模拟客户端发了坏 JSON）
    data = {
        "cage_id": "C57-023",
        "project_id": "default",
        "device_id": "phone-01",
        "records": json.dumps(_records_payload(1)),
    }
    files = {"readings": ("readings.json", b"not-json{", "application/json")}
    res = c.post("/api/records/report", data=data, files=files, headers=_headers())
    # 上报仍应 201（readings 非法不致命）
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["readings_saved"] is False
    assert body["count"] == 1, "记录本身仍正常落盘"

    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    assert not (run_dir / "readings.json").exists(), "非法 readings 不应落盘"
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert "readings_file" not in manifest


def test_report_invalid_readings_shape_skipped(client):
    """readings 是合法 JSON 但非 {readings: list} 形状 → 跳过，上报仍成功。"""
    c, app_mod = client
    res = _post(
        c,
        _records_payload(1),
        readings_obj={"not_a_readings_key": [1, 2, 3]},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["readings_saved"] is False
    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    assert not (run_dir / "readings.json").exists()


def test_report_all_duplicates_with_readings_drains_and_no_run(client):
    c, app_mod = client
    payload = _records_payload(3)
    first = _post(c, payload, readings_obj=_readings_payload(2))
    assert first.status_code == 201
    assert first.json()["readings_saved"] is True

    runs_before = app_mod.registry.list_runs()
    assert len(runs_before) == 1

    # 全部重复上报（带 readings）→ 不建 run，readings 被丢弃
    second = _post(c, payload, readings_obj=_readings_payload(2))
    assert second.status_code == 200
    body = second.json()
    assert body["count"] == 0
    assert body["run_id"] is None
    assert body["readings_saved"] is False
    assert sorted(body["skipped"]) == sorted(first.json()["record_ids"])

    # 仍只有一个 run（readings 未产生新 run）
    runs_after = app_mod.registry.list_runs()
    assert len(runs_after) == 1


def test_report_without_readings_has_no_readings_field(client):
    """非 dev 模式（不带 readings）行为与现状一致：响应 readings_saved=false，无 readings.json。"""
    c, app_mod = client
    res = _post(c, _records_payload(1))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["readings_saved"] is False
    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    assert not (run_dir / "readings.json").exists()


# --------------------------------------------------------------------------- #
# 8. 确认瞬间照片上传：手机端抓帧随记录上报，服务端优先采用（视频抽帧降级兜底）
# --------------------------------------------------------------------------- #


def test_report_with_uploaded_photos_uses_device_capture(client):
    """带 photos 上传 → photo.jpg 用上传内容（非占位）、photo_source 正确、计数。"""
    c, app_mod = client
    photo = _jpeg_bytes()
    payload = [
        {"record_id": "rec-p1", "ordinal": 1, "weight_g": 23.5},
        {"record_id": "rec-p2", "ordinal": 2, "weight_g": 24.0},
    ]
    res = _post(c, payload, photos=[("rec-p1", photo)])
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["photos_uploaded"] == 1
    assert body["photos_extracted"] == 0  # 无视频、无上传 → 只有 1 条被采用，另 1 条占位

    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]

    # 有上传照片的记录：photo.jpg 字节 == 上传内容（非占位）
    p1 = run_dir / "mouse_001" / "photo.jpg"
    assert p1.is_file()
    assert p1.read_bytes() == photo, "photo.jpg 应为手机端上传内容"
    rec1 = json.loads((run_dir / "mouse_001" / "record.json").read_text("utf-8"))
    assert rec1["photo_source"] == "device_capture"

    # 无上传照片的记录：走占位兜底（无视频）
    p2 = run_dir / "mouse_002" / "photo.jpg"
    assert p2.is_file()
    assert p2.read_bytes() != photo
    img2 = cv2.imread(str(p2))
    assert img2 is not None
    rec2 = json.loads((run_dir / "mouse_002" / "record.json").read_text("utf-8"))
    assert rec2["photo_source"] == "placeholder"


def test_report_partial_photos_mixed_paths(client, tmp_path):
    """部分记录有照片、部分没有 → 各自走对应路径（上传 vs 视频抽帧/占位）。"""
    c, app_mod = client
    photo = _jpeg_bytes()
    video_file = tmp_path / "mix.mp4"
    _write_synthetic_video(video_file, n_frames=20, fps=10.0, size=(48, 36))
    payload = [
        {"record_id": "mix-a", "ordinal": 1, "weight_g": 20.0, "clip_start_ms": 500},
        {"record_id": "mix-b", "ordinal": 2, "weight_g": 21.0, "clip_start_ms": 500},
        {"record_id": "mix-c", "ordinal": 3, "weight_g": 22.0},  # 无照片 → 抽帧
    ]
    res = _post(
        c,
        payload,
        video_bytes=video_file.read_bytes(),
        video_name="mix.mp4",
        photos=[("mix-a", photo)],
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["photos_uploaded"] == 1  # mix-a 上传采用
    assert body["photos_extracted"] == 2  # mix-b(抽帧) + mix-c(抽帧)

    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]

    # mix-a：上传内容
    pa = run_dir / "mouse_001" / "photo.jpg"
    assert pa.read_bytes() == photo
    assert json.loads((run_dir / "mouse_001" / "record.json").read_text("utf-8"))["photo_source"] == "device_capture"

    # mix-b、mix-c：视频抽帧（维度匹配源视频 48x36）
    for i in (2, 3):
        p = run_dir / f"mouse_{i:03d}" / "photo.jpg"
        img = cv2.imread(str(p))
        assert img is not None
        assert img.shape[:2] == (36, 48), "应为视频抽帧而非占位"
        assert json.loads((run_dir / f"mouse_{i:03d}" / "record.json").read_text("utf-8"))["photo_source"] == "video_frame"


def test_report_uploaded_png_photo_accepted(client):
    """上传 PNG 照片（魔数合法）→ 服务端采用，photo_source=device_capture。"""
    c, app_mod = client
    png = _png_bytes()
    payload = [{"record_id": "rec-png", "ordinal": 1, "weight_g": 21.0}]
    res = _post(c, payload, photos=[("rec-png", png)])
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["photos_uploaded"] == 1
    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    p = run_dir / "mouse_001" / "photo.jpg"
    assert p.read_bytes() == png
    assert json.loads((run_dir / "mouse_001" / "record.json").read_text("utf-8"))["photo_source"] == "device_capture"


def test_report_invalid_photo_bytes_falls_back(client):
    """伪造非法照片字节 → 魔数校验不通过 → 回退占位/抽帧，不报错。"""
    c, app_mod = client
    payload = [{"record_id": "rec-fake", "ordinal": 1, "weight_g": 20.0}]
    res = _post(c, payload, photos=[("rec-fake", b"not-an-image-at-all")])
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["photos_uploaded"] == 0
    assert body["photos_extracted"] == 0
    run_dir = Path(app_mod.DEFAULT_OUTPUT) / body["run_dir"]
    p = run_dir / "mouse_001" / "photo.jpg"
    assert p.is_file()
    img = cv2.imread(str(p))
    assert img is not None  # 占位图仍是可读 JPEG
    assert json.loads((run_dir / "mouse_001" / "record.json").read_text("utf-8"))["photo_source"] == "placeholder"


def test_report_all_duplicates_with_photos_no_new_run(client):
    """全部重复上报（带 photos）→ 不建 run，照片被 drain。"""
    c, app_mod = client
    payload = _records_payload(1)
    photo = _jpeg_bytes()
    first = _post(c, payload, photos=[("rec-001", photo)])
    assert first.status_code == 201
    runs_before = app_mod.registry.list_runs()
    assert len(runs_before) == 1

    second = _post(c, payload, photos=[("rec-001", photo)])
    assert second.status_code == 200
    body = second.json()
    assert body["count"] == 0
    assert body["photos_uploaded"] == 0
    assert sorted(body["skipped"]) == ["rec-001"]

    assert len(app_mod.registry.list_runs()) == 1


# --------------------------------------------------------------------------- #
# 9. 上报后服务端推进箱子 next_ordinal（防同箱下次录制从旧编号重号）。
# --------------------------------------------------------------------------- #


def test_report_advances_box_next_ordinal(client):
    """上报含 ordinal [3,4] 的记录后，该箱 next_ordinal == 5（max+1）。

    Bug：persist_report_records 持久化记录后不推进 boxes 表 → 下次手机端
    取到的 next_ordinal 还是旧值 → 同一箱多个「第 1 只」。修复后服务端兜底
    推进 next_ordinal = max(本次写入 ordinal) + 1。
    """
    c, app_mod = client
    cage = "C57-ADV-01"
    app_mod.box_registry.create(cage_id=cage)

    payload = [
        {"record_id": "adv-3", "ordinal": 3, "weight_g": 21.0},
        {"record_id": "adv-4", "ordinal": 4, "weight_g": 22.0},
    ]
    res = _post(c, payload, _cage=cage)
    assert res.status_code == 201, res.text

    box = app_mod.box_registry.get(cage)
    assert box is not None
    assert box["next_ordinal"] == 5, "next_ordinal 应推进至本次最大 ordinal + 1"


def test_report_advances_next_ordinal_is_monotonic(client):
    """next_ordinal 推进是单调的：再上报更小 ordinal 不应回退 next_ordinal。"""
    c, app_mod = client
    cage = "C57-ADV-02"
    app_mod.box_registry.create(cage_id=cage)

    # 第一次推进：上报 ordinal 3 → advance(at_least=4) → next_ordinal=4。
    _post(
        c,
        [{"record_id": "mono-3", "ordinal": 3, "weight_g": 21.0}],
        _cage=cage,
    )
    box = app_mod.box_registry.get(cage)
    assert box is not None
    assert box["next_ordinal"] == 4
    # 第二次上报更小 ordinal（2）→ advance(at_least=3) → max(4,3)=4 不回退。
    _post(
        c,
        [{"record_id": "mono-2", "ordinal": 2, "weight_g": 20.0}],
        _cage=cage,
    )
    box = app_mod.box_registry.get(cage)
    assert box is not None
    assert box["next_ordinal"] == 4


def test_report_auto_creates_box_and_advances_next_ordinal(client):
    """箱子不存在时上报 → 自动建箱且 next_ordinal == max+1。

    与 reserve_ordinal 的自动建箱语义一致：未注册的 cage_id 直接兜底建箱，
    不需要手动 create。mouse_no_start 保持默认 1，next_ordinal 推进至 5。
    """
    c, app_mod = client
    cage = "BALB-AUTO-01"
    assert app_mod.box_registry.get(cage) is None  # precondition: 箱子不存在

    payload = [
        {"record_id": "auto-3", "ordinal": 3, "weight_g": 21.0},
        {"record_id": "auto-4", "ordinal": 4, "weight_g": 22.0},
    ]
    res = _post(c, payload, _cage=cage)
    assert res.status_code == 201, res.text

    box = app_mod.box_registry.get(cage)
    assert box is not None, "应自动建箱"
    assert box["next_ordinal"] == 5
    assert box["mouse_no_start"] == 1  # 建箱默认值不被覆盖


def test_report_duplicate_records_does_not_advance_next_ordinal(client):
    """重复上报同一批 record_id（幂等全跳过）时不应推进 next_ordinal。

    全部记录已存在 → 不创建新 run、无新写入 → 不调用 advance_next_ordinal。
    """
    c, app_mod = client
    cage = "C57-ADV-03"
    app_mod.box_registry.create(cage_id=cage)

    payload = [
        {"record_id": "dup-3", "ordinal": 3, "weight_g": 21.0},
        {"record_id": "dup-4", "ordinal": 4, "weight_g": 22.0},
    ]
    first = _post(c, payload, _cage=cage)
    assert first.status_code == 201
    box = app_mod.box_registry.get(cage)
    assert box is not None
    next_after_first = box["next_ordinal"]
    assert next_after_first == 5

    # 重复上报同一批（全部幂等跳过）→ next_ordinal 不应再变。
    second = _post(c, payload, _cage=cage)
    assert second.status_code == 200
    assert second.json()["count"] == 0
    box = app_mod.box_registry.get(cage)
    assert box is not None
    assert box["next_ordinal"] == next_after_first
