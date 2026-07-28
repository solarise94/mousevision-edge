"""Automated acceptance tests for the scale time-sync MVP.

Covers spec §9 (``docs/SCALE_TIME_SYNC_MVP.md`` §9 验收清单 / 自动化测试):

* baseline CSV parses to exactly 10 readings, correct first/last rows;
* original SHA-256 matches §6.1 and bytes are preserved verbatim;
* synthetic two-anchor model math matches expected rate/offset/drift;
* rejects: time reversal, cross-import matching, bad CSV, oversized CSV,
  unauthenticated writes;
* calculated-session readings preview is monotonic and marks out-of-window
  readings unavailable.

The store + API are exercised through FastAPI's TestClient against an isolated
``tmp_path`` DB (no real OCR / video / existing tables touched).
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ui.scale_sync_api as scale_sync_api
from mousevision import scale_sync as ss

FIXTURE = Path(__file__).parent / "fixtures" / "scale_usb" / "260520.CSV"
EXPECTED_SHA256 = "0103f34c6ddfcd3eb640202d90861f09d81c9ee8c7e99861407da5833ca6f58a"
TOKEN = "test-token-abc"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An app with the scale-sync router bound to an isolated output dir.

    A token is configured so we can also assert that unauthenticated writes are
    rejected (spec §9 — 无权限写入均被拒绝).
    """
    monkeypatch.setenv("MOUSEVISION_API_TOKEN", TOKEN)
    scale_sync_api.configure(str(tmp_path))
    app = FastAPI()
    app.include_router(scale_sync_api.router)
    return TestClient(app)


def _auth() -> dict[str, str]:
    return {"X-MouseVision-Token": TOKEN}


def _baseline_bytes() -> bytes:
    return FIXTURE.read_bytes()


def _make_session(client: TestClient, **kw) -> str:
    r = client.post("/api/scale-sync/sessions", json=kw, headers=_auth())
    assert r.status_code == 200, r.text
    return r.json()["session_id"]


def _put_anchor(client: TestClient, sid: str, kind: str, epoch_ms: int, **extra) -> dict:
    body = {"client_epoch_ms": epoch_ms, "client_timezone": "Asia/Shanghai"}
    body.update(extra)
    r = client.put(f"/api/scale-sync/sessions/{sid}/anchors/{kind}", json=body, headers=_auth())
    assert r.status_code == 200, r.text
    return r.json()


def _import_csv(client: TestClient, sid: str, raw: bytes, name: str = "260520.CSV") -> str:
    r = client.post(
        f"/api/scale-sync/sessions/{sid}/imports",
        files={"file": (name, raw, "text/csv")},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    return r.json()["import_id"]


# --------------------------------------------------------------------------- #
# §9: CSV parsing of the baseline fixture
# --------------------------------------------------------------------------- #


def test_baseline_parses_to_exactly_ten_readings() -> None:
    parsed = ss.parse_scale_csv(_baseline_bytes(), "Asia/Shanghai")
    assert parsed.count == 10
    first = parsed.readings[0]
    last = parsed.readings[-1]
    # spec §9: first 2026-05-20 12:49:18, 500.40 g
    assert first.scale_dt_iso == "2026-05-20 12:49:18"
    assert first.weight_g == pytest.approx(500.40, abs=1e-6)
    # spec §9: last 2026-05-20 12:55:40, 500.36 g
    assert last.scale_dt_iso == "2026-05-20 12:55:40"
    assert last.weight_g == pytest.approx(500.36, abs=1e-6)
    assert first.unit == "g"
    # The GBK header row is reported as a warning, not a reading.
    assert any("第 1 行" in w for w in parsed.warnings)


def test_baseline_sha256_matches_spec_section_6_1() -> None:
    raw = _baseline_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPECTED_SHA256


# --------------------------------------------------------------------------- #
# §9: synthetic two-point model math
# --------------------------------------------------------------------------- #


def test_synthetic_two_point_model_math() -> None:
    # Construct integer anchor times whose ratio is exact, then assert the
    # derived rate/offset/drift and that phone_ms(S) reconstructs the anchors.
    #
    #   scale span  = 1,000,000 ms
    #   phone span  =   999,990 ms  ⇒ rate = 0.99999, drift = -10 ppm
    S1 = 1_785_218_703_000
    S2 = S1 + 1_000_000
    P1 = S1 + 83_400  # phone 83.4 s ahead of scale at the start
    P2 = P1 + 999_990

    res = ss.compute_two_point_model(
        scale_start_ms=S1, scale_end_ms=S2,
        phone_start_ms=P1, phone_end_ms=P2,
        phone_start_tz="Asia/Shanghai", phone_end_tz="Asia/Shanghai",
        session_tz="Asia/Shanghai",
    )
    m = res.model
    assert m.rate == pytest.approx(0.99999, abs=1e-12)
    assert m.start_offset_ms == pytest.approx(83_400.0, abs=1e-6)
    assert m.drift_ppm == pytest.approx(-10.0, abs=1e-6)
    # phone_ms(S) must reconstruct both anchors exactly.
    assert (m.rate * S1 + m.offset_ms) == pytest.approx(P1, abs=1e-3)
    assert (m.rate * S2 + m.offset_ms) == pytest.approx(P2, abs=1e-3)
    assert res.level == "green"


def test_high_drift_is_red_warning() -> None:
    S1 = 1_000_000
    S2 = S1 + 1_000_000
    P1 = S1
    P2 = P1 + 1_000_000 - 60_000  # ~6% drift ⇒ huge ppm
    res = ss.compute_two_point_model(
        scale_start_ms=S1, scale_end_ms=S2,
        phone_start_ms=P1, phone_end_ms=P2,
        phone_start_tz="Asia/Shanghai", phone_end_tz="Asia/Shanghai",
        session_tz="Asia/Shanghai",
    )
    assert res.level == "red"
    assert abs(res.model.drift_ppm) > 5000


def test_short_anchor_gap_is_yellow() -> None:
    S1 = 1_000_000
    S2 = S1 + 30_000  # 30 s < 60 s threshold
    res = ss.compute_two_point_model(
        scale_start_ms=S1, scale_end_ms=S2,
        phone_start_ms=S1, phone_end_ms=S2,
        phone_start_tz="Asia/Shanghai", phone_end_tz="Asia/Shanghai",
        session_tz="Asia/Shanghai",
    )
    assert res.level == "yellow"
    assert any("间隔" in w for w in res.warnings)


def test_reversed_time_is_rejected() -> None:
    with pytest.raises(ss.CalculationError):
        ss.compute_two_point_model(
            scale_start_ms=2000, scale_end_ms=1000,
            phone_start_ms=1000, phone_end_ms=2000,
            phone_start_tz="Asia/Shanghai", phone_end_tz="Asia/Shanghai",
            session_tz="Asia/Shanghai",
        )


# --------------------------------------------------------------------------- #
# §9: end-to-end import preserves bytes + SHA + round-trip
# --------------------------------------------------------------------------- #


def test_import_preserves_raw_bytes_verbatim_and_sha(client: TestClient, tmp_path: Path) -> None:
    raw = _baseline_bytes()
    sid = _make_session(client, scale_timezone="Asia/Shanghai")
    r = client.post(
        f"/api/scale-sync/sessions/{sid}/imports",
        files={"file": ("260520.CSV", raw, "text/csv")},
        headers=_auth(),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sha256"] == EXPECTED_SHA256
    assert body["byte_count"] == len(raw)
    assert body["summary"]["row_count"] == 10
    # Stored file is byte-identical (no transcoding/rewriting).
    stored = tmp_path / "scale_sync" / sid / body["import_id"] / "source.csv"
    assert stored.read_bytes() == raw


def test_readings_endpoint_returns_all_rows_sorted(client: TestClient) -> None:
    sid = _make_session(client)
    iid = _import_csv(client, sid, _baseline_bytes())
    r = client.get(f"/api/scale-sync/sessions/{sid}/imports/{iid}/readings", headers=_auth())
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 10
    epochs = [it["scale_epoch_ms"] for it in items]
    assert epochs == sorted(epochs)  # monotonic non-decreasing
    assert items[0]["weight_g"] == pytest.approx(500.40, abs=1e-6)


# --------------------------------------------------------------------------- #
# §9: full happy path → calculate → monotonic preview + window flag
# --------------------------------------------------------------------------- #


def test_full_calculate_flow_preview_monotonic_and_window_flag(client: TestClient) -> None:
    raw = _baseline_bytes()
    sid = _make_session(client, scale_timezone="Asia/Shanghai")
    # Both anchors sit 90 s ahead of their scale rows (constant offset ⇒ rate 1).
    S1 = 1779252558000  # row line 2: 12:49:18
    S2 = 1779252940000  # row line 11: 12:55:40
    _put_anchor(client, sid, "start", S1 + 90_000)
    _put_anchor(client, sid, "end", S2 + 90_000)
    iid = _import_csv(client, sid, raw)

    # Match anchors to the baseline rows (physical line numbers 2 and 11).
    for kind, line in (("start", 2), ("end", 11)):
        r = client.put(
            f"/api/scale-sync/sessions/{sid}/anchors/{kind}/match",
            json={"import_id": iid, "source_line_no": line},
            headers=_auth(),
        )
        assert r.status_code == 200, r.text

    r = client.post(f"/api/scale-sync/sessions/{sid}/calculate", headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "calculated"
    assert body["model"]["rate"] == pytest.approx(1.0, abs=1e-9)
    assert body["model"]["start_offset_ms"] == pytest.approx(90_000.0, abs=1e-6)

    # Full session carries a readings preview whose phone_ms is monotonic and
    # all preview rows are flagged within the valid window.
    r = client.get(f"/api/scale-sync/sessions/{sid}", headers=_auth())
    sess = r.json()
    preview = sess["readings_preview"]
    assert len(preview) >= 1
    phone_ms = [row["phone_epoch_ms"] for row in preview]
    assert phone_ms == sorted(phone_ms)
    assert all(row["within_window"] for row in preview)


def test_calculate_marks_out_of_window_readings_unavailable(client: TestClient) -> None:
    """A reading outside [S1, S2] must be flagged unavailable (spec §7)."""
    # Build a CSV with one extra reading BEFORE the matched window.
    # Window rows are line 2 (12:49:18) .. line 11 (12:55:40); add an earlier
    # 12:40:00 row so it falls outside.
    extra = (
        "  9,26-05-20,12:40:00,      0, 499.99,g\r"
    ).encode("gb18030")
    raw = extra + _baseline_bytes()
    sid = _make_session(client, scale_timezone="Asia/Shanghai")
    iid = _import_csv(client, sid, raw)
    S1 = 1779252558000
    S2 = 1779252940000
    _put_anchor(client, sid, "start", S1 + 90_000)
    _put_anchor(client, sid, "end", S2 + 90_000)
    # The early row is now physical line 2 (we prepended one line).
    client.put(
        f"/api/scale-sync/sessions/{sid}/anchors/start/match",
        json={"import_id": iid, "source_line_no": 3},  # original first reading shifted
        headers=_auth(),
    )
    client.put(
        f"/api/scale-sync/sessions/{sid}/anchors/end/match",
        json={"import_id": iid, "source_line_no": 12},  # original last reading shifted
        headers=_auth(),
    )
    client.post(f"/api/scale-sync/sessions/{sid}/calculate", headers=_auth())
    sess = client.get(f"/api/scale-sync/sessions/{sid}", headers=_auth()).json()
    preview = sess["readings_preview"]
    # The prepended 12:40:00 reading must be marked outside the window.
    assert any(not row["within_window"] for row in preview)


# --------------------------------------------------------------------------- #
# §9: reject paths
# --------------------------------------------------------------------------- #


def test_reject_cross_import_matching(client: TestClient) -> None:
    raw = _baseline_bytes()
    sid = _make_session(client)
    iid1 = _import_csv(client, sid, raw, name="a.CSV")
    iid2 = _import_csv(client, sid, raw, name="b.CSV")
    S1 = 1779252558000
    S2 = 1779252940000
    _put_anchor(client, sid, "start", S1 + 90_000)
    _put_anchor(client, sid, "end", S2 + 90_000)
    client.put(f"/api/scale-sync/sessions/{sid}/anchors/start/match",
               json={"import_id": iid1, "source_line_no": 2}, headers=_auth())
    client.put(f"/api/scale-sync/sessions/{sid}/anchors/end/match",
               json={"import_id": iid2, "source_line_no": 11}, headers=_auth())
    r = client.post(f"/api/scale-sync/sessions/{sid}/calculate", headers=_auth())
    assert r.status_code == 400
    assert "同一导入文件" in r.json()["detail"]


def test_reject_time_reversed_anchors(client: TestClient) -> None:
    sid = _make_session(client)
    iid = _import_csv(client, sid, _baseline_bytes())
    # end anchor earlier than start anchor (phone time)
    _put_anchor(client, sid, "start", 2_000_000_000_000)
    _put_anchor(client, sid, "end", 1_000_000_000_000)
    client.put(f"/api/scale-sync/sessions/{sid}/anchors/start/match",
               json={"import_id": iid, "source_line_no": 2}, headers=_auth())
    client.put(f"/api/scale-sync/sessions/{sid}/anchors/end/match",
               json={"import_id": iid, "source_line_no": 11}, headers=_auth())
    r = client.post(f"/api/scale-sync/sessions/{sid}/calculate", headers=_auth())
    assert r.status_code == 400
    assert "倒序" in r.json()["detail"] or "递增" in r.json()["detail"]


def test_reject_bad_csv(client: TestClient) -> None:
    sid = _make_session(client)
    # No date/weight columns at all — every line unparseable.
    bad = b"hello,world\nfoo,bar\n"
    r = client.post(
        f"/api/scale-sync/sessions/{sid}/imports",
        files={"file": ("bad.CSV", bad, "text/csv")},
        headers=_auth(),
    )
    assert r.status_code == 400
    assert "有效数据" in r.json()["detail"]


def test_reject_empty_csv(client: TestClient) -> None:
    sid = _make_session(client)
    r = client.post(
        f"/api/scale-sync/sessions/{sid}/imports",
        files={"file": ("empty.CSV", b"", "text/csv")},
        headers=_auth(),
    )
    assert r.status_code == 400


def test_reject_non_csv_extension(client: TestClient) -> None:
    sid = _make_session(client)
    r = client.post(
        f"/api/scale-sync/sessions/{sid}/imports",
        files={"file": ("data.txt", b"stuff", "text/plain")},
        headers=_auth(),
    )
    assert r.status_code == 400


def test_reject_oversized_csv(client: TestClient) -> None:
    sid = _make_session(client)
    big = b"0,26-05-20,12:49:18,0,1.00,g\r" * ((ss.MAX_IMPORT_BYTES // 25) + 10)
    r = client.post(
        f"/api/scale-sync/sessions/{sid}/imports",
        files={"file": ("big.CSV", big, "text/csv")},
        headers=_auth(),
    )
    assert r.status_code == 413


def test_reject_unauthenticated_writes(client: TestClient) -> None:
    # No X-MouseVision-Token header → 401.
    r = client.post("/api/scale-sync/sessions", json={"scale_timezone": "Asia/Shanghai"})
    assert r.status_code == 401
    sid = _make_session(client)
    r = client.put(
        f"/api/scale-sync/sessions/{sid}/anchors/start",
        json={"client_epoch_ms": 1, "client_timezone": "Asia/Shanghai"},
    )
    assert r.status_code == 401


def test_calculate_rejected_before_anchors_matched(client: TestClient) -> None:
    sid = _make_session(client)
    r = client.post(f"/api/scale-sync/sessions/{sid}/calculate", headers=_auth())
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Session restore semantics (spec §4.1)
# --------------------------------------------------------------------------- #


def test_session_survives_lookup_after_anchor_set(client: TestClient) -> None:
    sid = _make_session(client)
    _put_anchor(client, sid, "start", 1_700_000_000_000)
    sess = client.get(f"/api/scale-sync/sessions/{sid}", headers=_auth()).json()
    assert sess["session_id"] == sid
    assert len(sess["anchors"]) == 1
    assert sess["anchors"][0]["kind"] == "start"
    assert sess["anchors"][0]["client_epoch_ms"] == 1_700_000_000_000


def test_anchor_replace_resets_match(client: TestClient) -> None:
    sid = _make_session(client)
    iid = _import_csv(client, sid, _baseline_bytes())
    _put_anchor(client, sid, "start", 1_700_000_000_000)
    client.put(f"/api/scale-sync/sessions/{sid}/anchors/start/match",
               json={"import_id": iid, "source_line_no": 2}, headers=_auth())
    # Re-record the start anchor → prior match must be cleared.
    _put_anchor(client, sid, "start", 1_800_000_000_000)
    sess = client.get(f"/api/scale-sync/sessions/{sid}", headers=_auth()).json()
    start = next(a for a in sess["anchors"] if a["kind"] == "start")
    assert start["matched_row"] is None
    assert start["client_epoch_ms"] == 1_800_000_000_000
