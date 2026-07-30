# K797 BLE advertisement fixtures

Shared fixtures for the K797 BLE scale parser (`mousevision/scale_k797.py`)
and its tests (`tests/test_k797_parser.py`). They also document the on-disk
capture format used to archive real broadcasts.

## File layout

### `*.hex` — raw manufacturer-data payloads

Single-line, space-separated **UPPERCASE** hex bytes, one advertisement's
manufacturer data per file:

```
CA E8 03 28 08 95 CA 02 10 07 01 00 00 00 00 00 00 00
```

| File                | Bytes | Decodes to        |
|---------------------|-------|-------------------|
| `zero.hex`          | 18    | 0.0 g (raw 0)     |
| `26_3g.hex`         | 18    | 26.3 g (raw 263)  |
| `wrong_prefix.hex`  | 18    | reject: wrong_prefix (first byte `CB`) |
| `short_payload.hex` | 10    | reject: payload_too_short |

The 9-byte prefix `CA E8 03 28 08 95 CA 02 10` is constant. Weight is the
little-endian uint16 at bytes `[9..10]` (`grams = raw / 10.0`). Bytes
`[11..17]` are unconfirmed and must not affect parsing.

### `manifest.json` — parametrized test cases

A JSON array. Each case drives one parser test. Fields:

- `name` — case identifier.
- `file` — path to a `.hex` fixture, **or** `null` if `inline_hex` is used.
- `inline_hex` — inline space-separated hex, used for cases without a
  dedicated `.hex` file (e.g. `little_endian_guard`, `trailing_bytes_ignored`).
  Mutually exclusive with `file`.
- `local_name` — BLE Local Name to pass to the parser.
- `manufacturer_id` — BLE Manufacturer ID (int).
- `rssi` — RSSI in dBm (int or null).
- `expect` — `{"grams": <float>, "raw": <int>}` for accepted cases, else
  `null`.
- `reject` — one of the `ParseRejectReason` wire strings for rejected cases,
  else `null`.

Exactly one of `expect` / `reject` must be non-null.

### `captured_raw.jsonl` — real-capture archive format

JSON Lines; one full advertisement capture per line:

```json
{"local_name": "K797", "manufacturer_id": 0,
 "payload_hex": "CA E8 03 28 08 95 CA 02 10 07 01 ...",
 "rssi": -49, "received_at_epoch_ms": 1785393390194,
 "observed_display_g": 26.3}
```

`observed_display_g` is what the scale's own LCD showed at capture time —
the ground truth used to validate the parser against real hardware.

The five lines currently in the file are **synthetic placeholders** (see the
`_note` field on each line). They exist only to document the format. **Real
K797 captures are appended here after desensitization** (strip any device
identifiers / location data) once a device is available.
