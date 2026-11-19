#!/usr/bin/env bash
# End-to-end checks of the bundled Compose deployment in an isolated project.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT="sidewit-deploy-check-$$"
WORK="$(mktemp -d)"
export SIDEWIT_NETWORK_PREFIX="${SIDEWIT_CHECK_NETWORK_PREFIX:-172.30.254}"
export SIDEWIT_DATA_PATH="$WORK/data"
export SIDEWIT_API_PORT=0 SIDEWIT_WEB_PORT=0
if [[ "$(id -u)" != 0 ]]; then
  export SIDEWIT_UID="$(id -u)" SIDEWIT_GID="$(id -g)"
else
  export SIDEWIT_UID=1000 SIDEWIT_GID=1000
fi
mkdir -p "$SIDEWIT_DATA_PATH"
if [[ "$(id -u)" == 0 ]]; then chown "$SIDEWIT_UID:$SIDEWIT_GID" "$SIDEWIT_DATA_PATH"; fi
# Never read the developer's .env (provider keys, ports).
: >"$WORK/env"
compose() { docker compose --project-directory "$ROOT_DIR" -p "$PROJECT" --env-file "$WORK/env" "$@"; }

cleanup() {
  compose down -v --rmi local --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
check() { echo "ok: $*"; }

compose config -q
compose up -d --build --wait --wait-timeout 180 >/dev/null
API="http://$(compose port api 8000)"
WEB="http://$(compose port web 80)"
IMAGE="$(docker inspect -f '{{.Config.Image}}' "$(compose ps -q api)")"
NETWORK="${PROJECT}_default"

# Health checks, restart policies and check intervals ---------------------------------------
for service in api web; do
  id="$(compose ps -q "$service")"
  [[ "$(docker inspect -f '{{.State.Health.Status}}' "$id")" == healthy ]] || fail "$service health"
  [[ "$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$id")" == unless-stopped ]] ||
    fail "$service restart policy"
  [[ "$(docker inspect -f '{{json .Config.Healthcheck.Interval}}' "$id")" -ge 30000000000 ]] ||
    fail "$service health check interval"
  [[ "$(docker inspect -f '{{json .Config.Healthcheck.StartInterval}}' "$id")" -le 1000000000 ]] ||
    fail "$service health check start interval"
done
check "api and web are healthy, restart unless stopped, probe every 30s after a 1s start"
compose run --rm -T --no-deps api python -c "print('ok')" >/dev/null || fail "compose run api"
check "one-off 'docker compose run --rm api' still works"

# Non-root API and data file permissions ------------------------------------------------------
# SIDEWIT_UID is never 0 here, so this also proves the API is not root.
[[ "$(compose exec -T api id -u)" == "$SIDEWIT_UID" ]] || fail "API user"
context="$(curl -fsS -X POST "$API/api/v1/contexts" -H 'Content-Type: application/json' \
  -d '{"name":"deploy-check","dialect":"postgresql"}')"
id="$(printf '%s' "$context" | sed -E 's/.*"id":"([^"]+)".*/\1/')"
owner_mode="$(compose exec -T api stat -c '%u %a' "/data/contexts/$id.json")"
[[ "$owner_mode" == "$SIDEWIT_UID 644" ]] || fail "data file owner/mode: $owner_mode"
check "API runs as UID $SIDEWIT_UID; new data files are $owner_mode"

# Forwarded client addresses ----------------------------------------------------------------
# Containers with fixed addresses play distinct remote clients. Each prints the status codes
# of failed sign-ins sent to URL with a spoofed X-Forwarded-For.
attempts() { # address url count spoofed-xff
  docker run --rm --network "$NETWORK" --ip "$1" "$IMAGE" python -c '
import sys, urllib.request, urllib.error
url, count, spoof = sys.argv[1], int(sys.argv[2]), sys.argv[3]
codes = []
for i in range(count):
    request = urllib.request.Request(url, method="POST", data=b"{\"username\":\"x\",\"password\":\"x\"}",
        headers={"Content-Type": "application/json", "X-Sidewit-Web": "1",
                 "X-Forwarded-For": spoof.replace("N", str(i))})
    try:
        codes.append(urllib.request.urlopen(request).status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
print(" ".join(map(str, codes)))
' "$2" "$3" "$4"
}
login_web="http://web/api/web/session"
login_api="http://api:8000/api/web/session"
first="$SIDEWIT_NETWORK_PREFIX.250"
codes="$(attempts "$first" "$login_web" 11 "198.51.100.N")"
[[ "$codes" == "$(printf '401 %.0s' {1..10})429" ]] || fail "spoofed XFF escaped the limit: $codes"
check "a client cannot reset its sign-in limit with a spoofed X-Forwarded-For"
codes="$(attempts "$SIDEWIT_NETWORK_PREFIX.251" "$login_web" 1 "$first")"
[[ "$codes" == 401 ]] || fail "second client behind the proxy shares the limit: $codes"
check "clients behind the bundled proxy have separate sign-in limits"
codes="$(attempts "$SIDEWIT_NETWORK_PREFIX.252" "$login_api" 1 "$first")"
[[ "$codes" == 401 ]] || fail "direct API connection trusted X-Forwarded-For: $codes"
check "direct API connections use the socket address, not X-Forwarded-For"

# Web caching, compression and security headers --------------------------------------------
headers() { curl -fsS -o /dev/null -D - -H 'Accept-Encoding: gzip' "$@" | tr -d '\r'; }
has() { grep -qi "^$2" <<<"$1" || fail "$3: missing '$2'"; }
lacks() { if grep -qi "^$2" <<<"$1"; then fail "$3: unexpected '$2'"; fi; }
secured() { has "$1" "Content-Security-Policy: default-src 'self'" "$2"; has "$1" "X-Content-Type-Options: nosniff" "$2"; has "$1" "X-Frame-Options: DENY" "$2"; }

index="$(headers "$WEB/")"
secured "$index" "index"; has "$index" "Cache-Control: no-cache" "index"
route="$(headers "$WEB/tools/some-client-route")"
secured "$route" "client route"; has "$route" "Cache-Control: no-cache" "client route"
asset="$(curl -fsS "$WEB/" | grep -oE '/assets/[^"]+\.js' | head -1)"
[[ -n "$asset" ]] || fail "no hashed script in index.html"
script="$(headers "$WEB$asset")"
secured "$script" "asset"
has "$script" "Cache-Control: public, max-age=31536000, immutable" "asset"
has "$script" "Content-Encoding: gzip" "asset"
has "$script" "Content-Type: application/javascript" "asset"
missing="$(curl -sS -o /dev/null -D - "$WEB/assets/index-missing.js" | tr -d '\r')"
has "$missing" "HTTP/1.1 404" "missing asset"; secured "$missing" "missing asset"
lacks "$missing" "Cache-Control" "missing asset"
api="$(headers "$WEB/api/v1/health")"
secured "$api" "proxied API"; lacks "$api" "Content-Encoding" "proxied API"
check "index revalidates, hashed assets are immutable and gzipped, missing assets 404"
check "security headers on every response"
echo "Deployment checks passed."
