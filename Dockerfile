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
RUN uv sync --frozen --no-dev

COPY adhoc_assistant ./adhoc_assistant
COPY main.py adhoc_config.toml bot_config.toml ./

RUN mkdir -p /app/data /app/output && chmod 0777 /app/data /app/output

ENTRYPOINT ["uv", "run"]
CMD ["python", "-m", "adhoc_assistant.telegram_bot", "--config", "/app/bot_config.toml"]
