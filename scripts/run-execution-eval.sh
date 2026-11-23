#!/usr/bin/env bash
# Run the new execution-result suite in isolated, disposable containers.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="sidewit-execution-eval-$$"
REPORT_NAME="execution-workshops-$(date -u +%Y%m%dT%H%M%SZ)-$$.json"

cleanup() {
  docker compose -f "$ROOT_DIR/docker-compose.yml" -p "$PROJECT" --profile eval down -v \
    --rmi local --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

arguments=(run /evals/execution-workshops --api-url http://eval-api:8000 \
  --execution-url postgresql+asyncpg://sidewit_eval:sidewit_eval@eval-db:5432/sidewit_eval \
  --out "/results/$REPORT_NAME")
for case_id in "$@"; do
  arguments+=(--case "$case_id")
done

docker compose -f "$ROOT_DIR/docker-compose.yml" -p "$PROJECT" --profile eval \
  run --rm --build eval "${arguments[@]}"
