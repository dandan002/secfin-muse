# mock-ngt as its own image. Build from mocks/: docker build -f ngt/Dockerfile .
# Only the simulator libs and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/ngt

COPY libs /app/libs
COPY ngt/pyproject.toml ngt/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY ngt ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/ngt/.venv/bin:$PATH" PORT=8030
EXPOSE 8030 9878
CMD ["sh", "-c", "exec uvicorn mock_ngt.app:app --host 0.0.0.0 --port $PORT"]
