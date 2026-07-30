"""K797 BLE scale advertisement parser.

A K797 scale broadcasts *non-connectable* BLE advertisements. The HarmonyOS
app captures them, extracts the Local Name / Manufacturer ID / Manufacturer
Data, and forwards them here for parsing into a :class:`ScaleReading`.

The parser is pure (no I/O, no exceptions for protocol rejects). A protocol
mismatch (wrong name / id / prefix / length) is reported as a
:class:`ParseReject` carrying a :class:`ParseRejectReason`; the caller decides
whether to log / ignore / surface it. Only genuinely bad Python arguments
(``None`` inputs) raise ``TypeError`` so callers cannot silently feed garbage.

Protocol (confirmed):
  * BLE Local Name == ``"K797"``
  * Manufacturer ID == ``0x0000``
  * Manufacturer Data (payload) starts with the 9-byte prefix
    ``CA E8 03 28 08 95 CA 02 10``
  * Minimum payload length 18 bytes
  * Weight field: ``payload[9] | payload[10] << 8`` (little-endian uint16);
    grams = raw / 10.0
  * bytes [11..17] are unconfirmed and MUST NOT affect parsing
  * raw == 0 is a genuine zero reading (0.0 g), distinct from "no broadcast"
"""

from __future__ import annotations

import datetime as _dt
import enum
import hmac
from dataclasses import dataclass
from typing import Union

# --------------------------------------------------------------------- #
# Protocol constants
# --------------------------------------------------------------------- #

#: 9-byte manufacturer-data prefix every K797 payload must start with.
K797_PREFIX: bytes = bytes.fromhex("CA E8 03 28 08 95 CA 02 10")
#: Expected BLE Local Name for a K797 scale advertisement.
K797_LOCAL_NAME: str = "K797"
#: Expected BLE Manufacturer ID for a K797 scale advertisement.
K797_MANUFACTURER_ID: int = 0x0000
#: Minimum manufacturer-data payload length (prefix + 2 weight bytes + 7 more).
K797_MIN_PAYLOAD_LEN: int = 18

#: Maximum representable grams for a uint16 raw value (65535 / 10.0).
K797_MAX_GRAMS: float = 6553.5
#: Maximum raw uint16 value.
K797_MAX_RAW: int = 0xFFFF

#: Stable schema version stamped on every emitted reading.
_SCHEMA_VERSION: int = 1
#: Stable device-key string (prefix hex, lowercase, id-prefixed).
_DEVICE_KEY: str = "k797:0000:cae803280895ca0210"


class ParseRejectReason(str, enum.Enum):
    """Why an advertisement was rejected as not-a-K797 reading.

    The string value is the wire identifier logged / surfaced to clients.
    Validation order matters: the reason reported is the *first* check that
    fails, so callers can rely on e.g. ``wrong_prefix`` never appearing for a
    payload that is also too short.
    """

    WRONG_NAME = "wrong_name"
    WRONG_MANUFACTURER_ID = "wrong_manufacturer_id"
    PAYLOAD_TOO_SHORT = "payload_too_short"
    WRONG_PREFIX = "wrong_prefix"
    WEIGHT_OUT_OF_PROTOCOL_RANGE = "weight_out_of_protocol_range"


# --------------------------------------------------------------------- #
# Result dataclasses
# --------------------------------------------------------------------- #


@dataclass(frozen=True)
class ParseReject:
    """A rejected advertisement (not a valid K797 reading).

    ``reason`` is the first failing :class:`ParseRejectReason`. No weight is
    decoded — the caller must treat this as "no broadcast", not as 0.0 g.
    """

    reason: ParseRejectReason

    def to_dict(self) -> dict[str, object]:
        return {"reject": True, "reason": self.reason.value}


@dataclass(frozen=True)
class ScaleReading:
    """A successfully parsed K797 weight reading.

    Frozen so a reading can be safely cached / shared across threads. All
    timestamps are absolute (epoch ms / ISO UTC) so a reading remains
    meaningful regardless of when it is consumed.
    """

    schema_version: int
    device: str
    device_key: str
    grams: float
    raw: int
    rssi: int | None
    received_at: str  # ISO-8601 UTC string
    received_at_epoch_ms: int
    sequence: int  # caller-supplied; monotonicity is the caller's job
    source: str
    payload_hex: str  # space-separated UPPER hex

    def to_dict(self) -> dict[str, object]:
        """Produce the camelCase JSON contract documented in the plan.

        Keys exactly match the wire contract: schemaVersion, device,
        deviceKey, grams, raw, rssi, receivedAt, receivedAtEpochMs, sequence,
        source, payloadHex.
        """
        return {
            "schemaVersion": self.schema_version,
            "device": self.device,
            "deviceKey": self.device_key,
            "grams": self.grams,
            "raw": self.raw,
            "rssi": self.rssi,
            "receivedAt": self.received_at,
            "receivedAtEpochMs": self.received_at_epoch_ms,
            "sequence": self.sequence,
            "source": self.source,
            "payloadHex": self.payload_hex,
        }


# A parse result is either a reading or a protocol reject.
ParseResult = Union[ScaleReading, ParseReject]


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #


def _format_payload_hex(payload: bytes) -> str:
    """Space-separated UPPERCASE hex (e.g. ``"CA E8 03 ..."``)."""
    return " ".join(f"{b:02X}" for b in payload)


def _iso_utc(epoch_ms: int) -> str:
    """Render an epoch-ms value as an ISO-8601 UTC string.

    Uses ``datetime.fromtimestamp`` with millisecond precision and a ``Z``
    suffix. Falls back to a plain render if the timestamp is out of range.
    """
    epoch_seconds = epoch_ms / 1000.0
    try:
        dt = _dt.datetime.fromtimestamp(epoch_seconds, tz=_dt.timezone.utc)
        # timespec="milliseconds" → 3 fractional digits.
        return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return f"{epoch_ms}"


# --------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------- #


def parse_k797_advertisement(
    local_name: str | None,
    manufacturer_id: int | None,
    payload: bytes | bytearray | None,
    rssi: int | None,
    received_at_epoch_ms: int,
    sequence: int,
) -> ParseResult:
    """Parse a captured BLE advertisement into a :class:`ScaleReading`.

    Protocol rejects (wrong name / id / length / prefix / weight range) are
    returned as :class:`ParseReject` — never raised. ``None`` inputs are a
    caller bug and raise ``TypeError`` so they cannot masquerade as rejects.

    Args:
        local_name: BLE advertised Local Name (must equal ``"K797"``).
        manufacturer_id: BLE Manufacturer ID (must equal ``0x0000``).
        payload: Manufacturer Data bytes (prefix + weight + trailing).
        rssi: Received signal strength (dBm), or ``None`` if absent. Stored
            verbatim; never gates parsing.
        received_at_epoch_ms: Wall-clock capture time in epoch milliseconds.
        sequence: Caller-supplied monotonic sequence number. The parser only
            echoes it — enforcing monotonicity is the caller's job.

    Returns:
        A :class:`ScaleReading` on success, or a :class:`ParseReject` whose
        ``reason`` is the first failing protocol check.

    Raises:
        TypeError: If ``local_name``/``manufacturer_id``/``payload`` is
            ``None`` (these are caller errors, not protocol rejects), or if
            ``received_at_epoch_ms``/``sequence`` are not ints.
    """
    # --- Caller-contract guards (not protocol rejects) ----------------- #
    if local_name is None:
        raise TypeError("local_name must not be None")
    if manufacturer_id is None:
        raise TypeError("manufacturer_id must not be None")
    if payload is None:
        raise TypeError("payload must not be None")
    if not isinstance(local_name, str):
        raise TypeError(f"local_name must be str, got {type(local_name).__name__}")
    if isinstance(manufacturer_id, bool) or not isinstance(manufacturer_id, int):
        raise TypeError(
            f"manufacturer_id must be int, got {type(manufacturer_id).__name__}"
        )
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError(f"payload must be bytes/bytearray, got {type(payload).__name__}")
    if isinstance(received_at_epoch_ms, bool) or not isinstance(
        received_at_epoch_ms, int
    ):
        raise TypeError(
            f"received_at_epoch_ms must be int, got {type(received_at_epoch_ms).__name__}"
        )
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise TypeError(f"sequence must be int, got {type(sequence).__name__}")

    # --- Protocol validation (ordered; first failure wins) ------------- #
    # 1) Local name.
    if local_name != K797_LOCAL_NAME:
        return ParseReject(ParseRejectReason.WRONG_NAME)

    # 2) Manufacturer id.
    if manufacturer_id != K797_MANUFACTURER_ID:
        return ParseReject(ParseRejectReason.WRONG_MANUFACTURER_ID)

    # 3) Length (prefix + weight field needs >= 11 bytes; spec requires >= 18).
    payload_bytes = bytes(payload)
    if len(payload_bytes) < K797_MIN_PAYLOAD_LEN:
        return ParseReject(ParseRejectReason.PAYLOAD_TOO_SHORT)

    # 4) Prefix — constant-time compare so a streaming attacker cannot learn
    #    how many leading bytes match by timing the comparison.
    prefix = payload_bytes[: len(K797_PREFIX)]
    if not hmac.compare_digest(prefix, K797_PREFIX):
        return ParseReject(ParseRejectReason.WRONG_PREFIX)

    # 5) Weight field (little-endian uint16). raw is uint16 by construction
    #    so it is always in [0, 65535]; the grams range check is defensive.
    raw = payload_bytes[9] | (payload_bytes[10] << 8)
    grams = raw / 10.0
    if not (0.0 <= grams <= K797_MAX_GRAMS) or not (0 <= raw <= K797_MAX_RAW):
        return ParseReject(ParseRejectReason.WEIGHT_OUT_OF_PROTOCOL_RANGE)

    return ScaleReading(
        schema_version=_SCHEMA_VERSION,
        device=K797_LOCAL_NAME,
        device_key=_DEVICE_KEY,
        grams=grams,
        raw=raw,
        rssi=rssi,
        received_at=_iso_utc(received_at_epoch_ms),
        received_at_epoch_ms=received_at_epoch_ms,
        sequence=sequence,
        source="ble",
        payload_hex=_format_payload_hex(payload_bytes),
    )
