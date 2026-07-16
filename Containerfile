FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MOUSEVISION_HOST=0.0.0.0 \
    MOUSEVISION_PORT=8766 \
    MOUSEVISION_OUTPUT_DIR=/app/output \
    MOUSEVISION_MAX_UPLOAD_MB=250 \
    MOUSEVISION_VIDEO_BACKEND=ffmpeg

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        libglib2.0-0 \
        libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY mousevision ./mousevision
COPY ui ./ui
COPY tools ./tools
COPY assets ./assets
COPY configs ./configs
COPY RefVideo ./RefVideo

RUN mkdir -p /app/output/job_uploads

EXPOSE 8766

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail http://127.0.0.1:8766/api/health || exit 1

CMD ["python", "-m", "ui.app"]
