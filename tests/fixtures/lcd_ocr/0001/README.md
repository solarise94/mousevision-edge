# LCD OCR acceptance fixtures (0001)

Version-controlled critical frames for `services/lcd_ocr/accept_0001.py`.

| File | Approx. truth | Notes |
|------|---------------|-------|
| `mouse_004_photo.jpg` | 23.79 | was misread ~8.38 |
| `scan5/t46500_24.18.jpg` | 24.18 | platform |
| `scan5/t48300_24.14.jpg` | 24.14 | localization / slots |
| `mouse_003_photo.jpg` | 23.66 | non-critical |
| `mouse_006_photo.jpg` | 23.47 | non-critical |
| `frames/m4_t32300.jpg` | ~23.69–23.79 | mid-run frame |
| `frames/m2_photo_21.60.jpg` | 21.60 | narrow `1` blooming → `2` regression |

Source video for full e2e remains outside git (`tmp_ocr_acceptance/0001/source.mp4`).
