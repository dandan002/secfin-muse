# mock-control as its own image. Build from mocks/: docker build -f control/Dockerfile .
# Only the simulator libs and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/control

COPY libs /app/libs
COPY control/pyproject.toml control/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY control ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/control/.venv/bin:$PATH" PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn mock_control.app:app --host 0.0.0.0 --port $PORT"]
