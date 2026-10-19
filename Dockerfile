FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-noto-core fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH
# VOICE=1 adds CPU speaker embeddings (diarization + voiceprints, ~1 GB). Without it all speech is one speaker.
ARG VOICE=1
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project $([ "$VOICE" = "1" ] && echo --extra voice)
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev $([ "$VOICE" = "1" ] && echo --extra voice)

RUN useradd -m -u 1000 mm && mkdir -p /data && chown mm /data
USER mm
ENV MM_DATA_DIR=/data MM_COMPANY_CONFIG=/config/company.yaml HF_HOME=/data/models
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["mm", "serve"]
