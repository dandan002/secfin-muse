# mock-wematch as its own image. Build from mocks/: docker build -f wematch/Dockerfile .
# Only the simulator libs and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/wematch

COPY libs /app/libs
COPY wematch/pyproject.toml wematch/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY wematch ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/wematch/.venv/bin:$PATH" PORT=8040
EXPOSE 8040
CMD ["sh", "-c", "exec uvicorn mock_wematch.app:app --host 0.0.0.0 --port $PORT"]
