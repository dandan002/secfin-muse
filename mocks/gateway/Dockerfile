# mock-gateway as its own image. Build from mocks/: docker build -f gateway/Dockerfile .
# Only the simulator libs and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/gateway

COPY libs /app/libs
COPY gateway/pyproject.toml gateway/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY gateway ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/gateway/.venv/bin:$PATH" PORT=8020
EXPOSE 8020
CMD ["sh", "-c", "exec uvicorn mock_gateway.app:app --host 0.0.0.0 --port $PORT"]
