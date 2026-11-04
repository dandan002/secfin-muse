# The desk's API as an image. Build from the repo root: docker build -f platform/api/Dockerfile .
# Only the reference-data libs (the security-master feed, see pyproject.toml) and this project go in.
FROM python:3.13.1-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app/platform/api

COPY mocks/libs /app/mocks/libs
COPY platform/api/pyproject.toml platform/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY platform/api ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/platform/api/.venv/bin:$PATH" PORT=8100
EXPOSE 8100
CMD ["sh", "-c", "exec uvicorn loandesk.app:app --host 0.0.0.0 --port $PORT"]
