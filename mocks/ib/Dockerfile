# mock-ib as its own image. Build from mocks/: docker build -f ib/Dockerfile .
# Only the simulator libs and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/ib

COPY libs /app/libs
COPY ib/pyproject.toml ib/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY ib ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/ib/.venv/bin:$PATH" PORT=8010
EXPOSE 8010
CMD ["sh", "-c", "exec uvicorn mock_ib.app:app --host 0.0.0.0 --port $PORT"]
