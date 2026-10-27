# mock-custodian as its own image. Build from mocks/: docker build -f custodian/Dockerfile .
# Only the simulator libs and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/custodian

COPY libs /app/libs
COPY custodian/pyproject.toml custodian/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY custodian ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/custodian/.venv/bin:$PATH" PORT=8060
EXPOSE 8060
CMD ["sh", "-c", "exec uvicorn mock_custodian.app:app --host 0.0.0.0 --port $PORT"]
