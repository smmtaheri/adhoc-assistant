FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        librsvg2-bin \
        fontconfig \
        fonts-noto-core \
        fonts-farsiweb \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
RUN UV_CACHE_DIR=/tmp/build-uv-cache XDG_CACHE_HOME=/tmp/build-xdg-cache HOME=/tmp uv sync --frozen --no-dev \
    && rm -rf /tmp/build-uv-cache /tmp/build-xdg-cache

COPY adhoc_assistant ./adhoc_assistant
COPY main.py adhoc_config.toml ./

RUN mkdir -p /app/data /app/output

ENV UV_CACHE_DIR=/tmp/runtime-uv-cache \
    XDG_CACHE_HOME=/tmp/runtime-xdg-cache \
    HOME=/tmp \
    UV_PROJECT_ENVIRONMENT=/app/.venv

ENTRYPOINT ["uv", "run"]
CMD ["python", "-m", "adhoc_assistant.telegram_bot"]
