"""Tests for the K797 BLE advertisement parser.

Drives the shared fixture manifest (``tests/fixtures/k797_ble/manifest.json``)
plus standalone checks for caller-contract guards, determinism, bytearray
support, raw=0 semantics, and the camelCase JSON contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mousevision.scale_k797 import (
    K797_LOCAL_NAME,
    K797_MANUFACTURER_ID,
    K797_MIN_PAYLOAD_LEN,
    K797_PREFIX,
    ParseReject,
    ParseRejectReason,
    ScaleReading,
    parse_k797_advertisement,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "k797_ble"
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"


# --------------------------------------------------------------------- #
# Fixture loading
# --------------------------------------------------------------------- #


def _load_manifest() -> list[dict]:
    with MANIFEST_PATH.open(encoding="utf-8") as fh:
        data = json.load(fh)
    assert isinstance(data, list) and data, "manifest.json must be a non-empty array"
    return data


def _payload_for(case: dict) -> bytes:
    """Resolve a case's payload from file or inline_hex."""
    if case.get("inline_hex"):
        return bytes.fromhex(case["inline_hex"].replace(" ", ""))
    assert case.get("file"), f"case {case['name']} has neither file nor inline_hex"
    text = (FIXTURES_DIR / case["file"]).read_text(encoding="utf-8").strip()
    return bytes.fromhex(text.replace(" ", ""))


def _case_ids() -> list[str]:
    return [c["name"] for c in _load_manifest()]


@pytest.fixture(params=_load_manifest(), ids=_case_ids())
def case(request) -> dict:
    return request.param


# --------------------------------------------------------------------- #
# Manifest-driven tests
# --------------------------------------------------------------------- #


def test_manifest_cases(case: dict) -> None:
    """Each manifest case must parse to the expected reading or reject."""
    payload = _payload_for(case)
    result = parse_k797_advertisement(
        local_name=case["local_name"],
        manufacturer_id=case["manufacturer_id"],
        payload=payload,
        rssi=case.get("rssi"),
        received_at_epoch_ms=1785393390194,
        sequence=1248,
    )

    expect = case.get("expect")
    reject = case.get("reject")
    assert (expect is None) ^ (reject is None), (
        f"case {case['name']}: exactly one of expect/reject must be set"
    )

    if expect is not None:
        assert isinstance(result, ScaleReading), (
            f"case {case['name']}: expected ScaleReading, got reject {result}"
        )
        assert result.raw == expect["raw"], (
            f"case {case['name']}: raw {result.raw} != {expect['raw']}"
        )
        assert abs(result.grams - expect["grams"]) < 1e-9, (
            f"case {case['name']}: grams {result.grams} != {expect['grams']}"
        )
        # RSSI is echoed verbatim.
        assert result.rssi == case.get("rssi")
    else:
        assert isinstance(result, ParseReject), (
            f"case {case['name']}: expected ParseReject, got {result!r}"
        )
        assert result.reason == ParseRejectReason(reject), (
            f"case {case['name']}: reason {result.reason.value} != {reject}"
        )


# --------------------------------------------------------------------- #
# raw=0 semantics
# --------------------------------------------------------------------- #


def test_raw_zero_is_genuine_zero_not_reject() -> None:
    """raw=0 must yield grams==0.0 and must NOT be a reject (no broadcast != 0g)."""
    result = parse_k797_advertisement(
        local_name=K797_LOCAL_NAME,
        manufacturer_id=K797_MANUFACTURER_ID,
        payload=bytes.fromhex("CA E8 03 28 08 95 CA 02 10 00 00 00 00 00 00 00 00 00"),
        rssi=-60,
        received_at_epoch_ms=1785393390194,
        sequence=1,
    )
    assert isinstance(result, ScaleReading)
    assert result.raw == 0
    assert result.grams == 0.0
    assert result.grams == 0.0  # distinct from "no broadcast" — this IS a reading


# --------------------------------------------------------------------- #
# bytearray support
# --------------------------------------------------------------------- #


def test_payload_as_bytearray_works() -> None:
    """payload as bytearray (not bytes) must parse identically."""
    payload_bytes = bytes.fromhex("CA E8 03 28 08 95 CA 02 10 07 01 00 00 00 00 00 00 00")
    payload_ba = bytearray(payload_bytes)
    r1 = parse_k797_advertisement(
        K797_LOCAL_NAME, K797_MANUFACTURER_ID, payload_bytes, -49, 1785393390194, 5
    )
    r2 = parse_k797_advertisement(
        K797_LOCAL_NAME, K797_MANUFACTURER_ID, payload_ba, -49, 1785393390194, 5
    )
    assert isinstance(r1, ScaleReading) and isinstance(r2, ScaleReading)
    assert r1 == r2


# --------------------------------------------------------------------- #
# Caller-contract guards (None inputs raise TypeError, not rejects)
# --------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs",
    [
        # None local_name
        dict(local_name=None, manufacturer_id=0, payload=b"\x00" * 18),
        # None manufacturer_id
        dict(local_name="K797", manufacturer_id=None, payload=b"\x00" * 18),
        # None payload
        dict(local_name="K797", manufacturer_id=0, payload=None),
    ],
)
def test_none_inputs_raise_typeerror(kwargs) -> None:
    with pytest.raises(TypeError):
        parse_k797_advertisement(
            rssi=-49, received_at_epoch_ms=1785393390194, sequence=1, **kwargs
        )


# --------------------------------------------------------------------- #
# Sequence echo (monotonicity is the caller's job)
# --------------------------------------------------------------------- #


@pytest.mark.parametrize("seq", [0, 1, 1248, 2_000_000_000])
def test_sequence_is_echoed_not_enforced(seq: int) -> None:
    """The parser echoes the caller-supplied sequence; it does not check
    monotonicity (that is the caller's responsibility)."""
    result = parse_k797_advertisement(
        K797_LOCAL_NAME,
        K797_MANUFACTURER_ID,
        bytes.fromhex("CA E8 03 28 08 95 CA 02 10 07 01 00 00 00 00 00 00 00"),
        -49,
        1785393390194,
        seq,
    )
    assert isinstance(result, ScaleReading)
    assert result.sequence == seq


# --------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------- #


def test_parse_is_deterministic() -> None:
    """The same bytes must parse to the same reading on repeated calls."""
    payload = bytes.fromhex("CA E8 03 28 08 95 CA 02 10 07 01 00 00 00 00 00 00 00")
    r1 = parse_k797_advertisement(
        K797_LOCAL_NAME, K797_MANUFACTURER_ID, payload, -49, 1785393390194, 7
    )
    r2 = parse_k797_advertisement(
        K797_LOCAL_NAME, K797_MANUFACTURER_ID, payload, -49, 1785393390194, 7
    )
    assert r1 == r2


# --------------------------------------------------------------------- #
# JSON contract (camelCase keys exactly match the plan)
# --------------------------------------------------------------------- #


def test_to_dict_camelcase_keys_match_contract() -> None:
    """to_dict must produce exactly the documented camelCase keys."""
    result = parse_k797_advertisement(
        K797_LOCAL_NAME,
        K797_MANUFACTURER_ID,
        bytes.fromhex("CA E8 03 28 08 95 CA 02 10 07 01 00 00 00 00 00 00 00"),
        -49,
        1785393390194,
        1248,
    )
    assert isinstance(result, ScaleReading)
    d = result.to_dict()
    assert set(d.keys()) == {
        "schemaVersion",
        "device",
        "deviceKey",
        "grams",
        "raw",
        "rssi",
        "receivedAt",
        "receivedAtEpochMs",
        "sequence",
        "source",
        "payloadHex",
    }
    assert d["schemaVersion"] == 1
    assert d["device"] == "K797"
    assert d["deviceKey"] == "k797:0000:cae803280895ca0210"
    assert d["grams"] == 26.3
    assert d["raw"] == 263
    assert d["rssi"] == -49
    assert d["receivedAtEpochMs"] == 1785393390194
    assert d["sequence"] == 1248
    assert d["source"] == "ble"
    # payloadHex is space-separated UPPER hex.
    assert d["payloadHex"] == "CA E8 03 28 08 95 CA 02 10 07 01 00 00 00 00 00 00 00"
    # receivedAt is an ISO string ending with Z.
    assert isinstance(d["receivedAt"], str)
    assert d["receivedAt"].endswith("Z")


# --------------------------------------------------------------------- #
# Protocol constant sanity (guards against accidental edits)
# --------------------------------------------------------------------- #


def test_protocol_constants() -> None:
    assert K797_LOCAL_NAME == "K797"
    assert K797_MANUFACTURER_ID == 0x0000
    assert K797_MIN_PAYLOAD_LEN == 18
    assert K797_PREFIX == bytes.fromhex("CA E8 03 28 08 95 CA 02 10")
    assert len(K797_PREFIX) == 9


def test_parse_reject_reason_values() -> None:
    """The 5 reject reasons must have the documented wire strings."""
    expected = {
        "wrong_name",
        "wrong_manufacturer_id",
        "payload_too_short",
        "wrong_prefix",
        "weight_out_of_protocol_range",
    }
    actual = {r.value for r in ParseRejectReason}
    assert actual == expected


# --------------------------------------------------------------------- #
# captured_raw.jsonl format sanity
# --------------------------------------------------------------------- #


def test_captured_raw_jsonl_lines_are_valid_json() -> None:
    """Every line of captured_raw.jsonl must be valid JSON with the documented
    fields. (Values are synthetic placeholders — this only checks shape.)"""
    path = FIXTURES_DIR / "captured_raw.jsonl"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 5
    required = {
        "local_name",
        "manufacturer_id",
        "payload_hex",
        "rssi",
        "received_at_epoch_ms",
        "observed_display_g",
    }
    for ln in lines:
        obj = json.loads(ln)
        assert required.issubset(obj.keys()), f"missing keys: {required - set(obj)}"
        # Each placeholder line carries a _note flagging it as synthetic.
        assert obj.get("_note"), "placeholder lines must carry a _note field"
