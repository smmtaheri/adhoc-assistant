FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY adhoc_assistant ./adhoc_assistant
COPY main.py adhoc_config.toml ./

RUN mkdir -p /data /app/output

ENTRYPOINT ["uv", "run", "python", "main.py"]
CMD ["--db", "/data/adhoc_history.sqlite3", "--csv", "/app/output/adhoc_schedule.csv", "--html", "/app/output/adhoc_schedule.html", "--image", "/app/output/adhoc_schedule.svg"]
