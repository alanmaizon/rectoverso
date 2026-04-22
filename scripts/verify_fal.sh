#!/usr/bin/env bash
# Preflight check for fal.ai access (Kling 2.x). Costs nothing (read-only probes).
# Run from repo root:   bash scripts/verify_fal.sh
# Exit 0 = both keys reachable. Non-zero = one or more checks failed.

set -u

# Run from repo root regardless of where invoked.
cd "$(dirname "$0")/.."

FAIL=0
pass() { printf "\033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "\033[33m!\033[0m %s\n" "$1"; }
err()  { printf "\033[31m✗\033[0m %s\n" "$1"; FAIL=1; }
info() { printf "  %s\n" "$1"; }

echo "== rectoverso: fal.ai preflight =="

# --- load .env -------------------------------------------------------------
if [ ! -f ".env" ]; then
  err ".env missing — copy .env.example and fill keys"
  exit 1
fi

# shellcheck disable=SC1091
set -a
. ./.env
set +a

# --- curl installed --------------------------------------------------------
if ! command -v curl >/dev/null 2>&1; then
  err "curl not installed"
  exit 1
fi

# --- probe one key --------------------------------------------------------
# fal.ai auth uses `Authorization: Key <key>`. We hit the queue status endpoint
# with a bogus request_id: if the key is valid, fal returns 404 (not found);
# if invalid/missing, it returns 401. Either way, no job is submitted, no cost.
probe_key() {
  local label="$1"
  local key="$2"
  if [ -z "$key" ]; then
    err "$label is empty in .env"
    return
  fi

  # Mask: show first 6 + last 4, hide the middle.
  local masked
  if [ "${#key}" -gt 12 ]; then
    masked="${key:0:6}…${key: -4}"
  else
    masked="(short-key)"
  fi
  info "$label = $masked"

  local url="https://queue.fal.run/fal-ai/kling-video/requests/00000000-0000-0000-0000-000000000000/status"
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Key $key" \
    -H "Accept: application/json" \
    "$url")

  case "$code" in
    200|404)
      pass "$label authenticates (HTTP $code — reachable, key accepted)"
      ;;
    401|403)
      err "$label rejected (HTTP $code — invalid or revoked key)"
      ;;
    429)
      warn "$label rate-limited (HTTP 429 — key valid but throttled)"
      ;;
    000)
      err "$label network error (no response from queue.fal.run)"
      ;;
    *)
      warn "$label unexpected HTTP $code — treat as probable-ok, retry before run"
      ;;
  esac
}

echo
echo "-- key probes (queue.fal.run) --"
probe_key "FAL_KEY_PRIMARY"   "${FAL_KEY_PRIMARY:-}"
probe_key "FAL_KEY_SECONDARY" "${FAL_KEY_SECONDARY:-}"

# --- Kling model reachability (unauthenticated metadata) -------------------
echo
echo "-- Kling model reachability --"
# fal's public schema endpoint (no auth needed) confirms the app exists in the
# region the SDK will hit. A 200/301/302 is fine.
SCHEMA_URL="https://fal.ai/models/fal-ai/kling-video/v2.1/standard/text-to-video"
SCHEMA_CODE=$(curl -s -o /dev/null -w "%{http_code}" -L "$SCHEMA_URL")
case "$SCHEMA_CODE" in
  200) pass "kling-video v2.1 model page reachable" ;;
  404) warn "kling-video v2.1 model page 404 — fal may have renamed the slug; check https://fal.ai/models" ;;
  *)   warn "kling-video v2.1 model page HTTP $SCHEMA_CODE" ;;
esac

# --- summary ---------------------------------------------------------------
echo
if [ $FAIL -eq 0 ]; then
  pass "ready"
  exit 0
else
  err "one or more checks failed — fix and re-run"
  exit 1
fi
