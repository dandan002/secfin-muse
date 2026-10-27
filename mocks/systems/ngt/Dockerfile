# mock-ngt as its own image. Build from mocks/: docker build -f systems/ngt/Dockerfile .
# Only the simulator libs and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/systems/ngt

COPY libs /app/libs
COPY systems/ngt/pyproject.toml systems/ngt/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY systems/ngt ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/systems/ngt/.venv/bin:$PATH" PORT=8030
EXPOSE 8030 9878
CMD ["sh", "-c", "exec uvicorn mock_ngt.app:app --host 0.0.0.0 --port $PORT"]
