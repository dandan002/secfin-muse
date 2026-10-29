# mock-analytics as its own image. Build from mocks/: docker build -f analytics/Dockerfile .
# Only the simulator libs and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/analytics

COPY libs /app/libs
COPY analytics/pyproject.toml analytics/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY analytics ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/analytics/.venv/bin:$PATH" PORT=8070
EXPOSE 8070
CMD ["sh", "-c", "exec uvicorn mock_analytics.app:app --host 0.0.0.0 --port $PORT"]
