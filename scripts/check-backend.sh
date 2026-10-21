#!/usr/bin/env bash
# Runs file-backed API tests; PostgreSQL is used only for differential validator tests.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
UV_IMAGE="ghcr.io/astral-sh/uv:0.8-python3.12-bookworm-slim"
NAME="sidewit-check-$$"

cleanup() {
  docker rm -fv "$NAME-db" >/dev/null 2>&1 || true
  docker network rm "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# PostgreSQL is a test oracle, not a product dependency.
docker network create "$NAME" >/dev/null
docker run -d --name "$NAME-db" --network "$NAME" \
  -e POSTGRES_USER=sidewit -e POSTGRES_PASSWORD=sidewit postgres:16 >/dev/null

# TCP check: during initdb the server only listens on the unix socket.
ready=false
for _ in {1..60}; do
  if docker exec "$NAME-db" pg_isready -h 127.0.0.1 -U sidewit >/dev/null 2>&1; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  echo "Test PostgreSQL did not become ready within 60 seconds." >&2
  exit 1
fi

docker run --rm --network "$NAME" \
  -v "$BACKEND_DIR":/app -v sidewit-backend-venv:/app/.venv -w /app \
  -v "$ROOT_DIR/evals":/evals:ro -e SIDEWIT_EVALS_DIR=/evals \
  -v "$ROOT_DIR/scripts":/checks:ro \
  -e UV_LINK_MODE=copy \
  -e SIDEWIT_DATABASE_URL="postgresql+asyncpg://sidewit:sidewit@$NAME-db:5432/sidewit_test" \
  "$UV_IMAGE" sh -c "uv sync --frozen -q && uv run ruff check . && uv run ruff format --check . && uv run pytest -q $* && uv build --wheel --out-dir /tmp/sidewit-wheel && uv run python -I /checks/check-wheel.py /tmp/sidewit-wheel/*.whl"
