FROM pytorch/pytorch:2.12.0-cuda13.0-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STEM_STUDIO_DATA=/data \
    GRADIO_ANALYTICS_ENABLED=False \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libsndfile1 python3-venv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN python -m venv --system-site-packages /opt/venv \
    && python -m pip install --upgrade pip \
    && python -m pip install \
        "audio-separator==0.44.3" \
        "gradio==6.20.0" \
        "onnxruntime==1.27.0"

COPY README.md ./
COPY src ./src
RUN python -m pip install --no-deps .

RUN mkdir -p /data/models /data/outputs /data/temp /data/live/inbox /data/live/outbox /data/live/work /data/live/failed

EXPOSE 7860
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/', timeout=3)" || exit 1

CMD ["python", "-m", "stemstudio.app"]
