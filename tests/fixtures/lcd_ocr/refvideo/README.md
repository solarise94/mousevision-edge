# LCD OCR fixtures (RefVideo)

Version-controlled frames for HTTP OCR / decoder A/B (`services/lcd_ocr/accept_refvideo.py`).

Source: `RefVideo/9494224d488d6e735c0f108cc5562a2d.mp4`

Expected sessions: `16.15 / 17.22 / 17.57 / 15.10 / 15.64 / 17.55 / 17.77 / 16.87`

Notes:

- Prefer **PNG** (JPEG compression can turn `15.10` into `11.10`).
- Extract with `tools/extract_refvideo_fixtures.py` (keeps frames where TemplateReader and `classic_v2` agree).
- Soft transition / hard frames only forbid confident bogus `11.x` reads.
