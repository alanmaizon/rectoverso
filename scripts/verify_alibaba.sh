#!/usr/bin/env bash
# Preflight check for Alibaba DashScope access (Wan 2.7 Plus/Turbo).
# Run from repo root:   bash scripts/verify_alibaba.sh
# Exit 0 = key accepted by DashScope. Non-zero = one or more checks failed.
#
# Notes:
# - DashScope has two endpoints: domestic (dashscope.aliyuncs.com) and
#   international (dashscope-intl.aliyuncs.com). We probe both; either one
#   working is sufficient for the renderer.
# - The probe submits NO job. We call a tasks GET with a bogus task_id:
#     * 401/403  → key invalid
#     * 404      → key accepted, task just doesn't exist (what we want)
#     * 200      → shouldn't happen with a bogus id, but also fine
# - Free quota is not introspectable via API; we rely on a counter in the
#   manifest (budget.alibaba_quota_remaining).

set -u

cd "$(dirname "$0")/.."

FAIL=0
pass() { printf "\033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "\033[33m!\033[0m %s\n" "$1"; }
err()  { printf "\033[31m✗\033[0m %s\n" "$1"; FAIL=1; }
info() { printf "  %s\n" "$1"; }

echo "== rectoverso: Alibaba DashScope (Wan) preflight =="

if [ ! -f ".env" ]; then
  err ".env missing — copy .env.example and fill keys"
  exit 1
fi

# shellcheck disable=SC1091
set -a
. ./.env
set +a

if ! command -v curl >/dev/null 2>&1; then
  err "curl not installed"
  exit 1
fi

if [ -z "${DASHSCOPE_API_KEY:-}" ]; then
  err "DASHSCOPE_API_KEY not set in .env"
  exit 1
fi

KEY="$DASHSCOPE_API_KEY"
if [ "${#KEY}" -gt 12 ]; then
  info "DASHSCOPE_API_KEY = ${KEY:0:6}…${KEY: -4}"
fi

BOGUS_TASK="00000000-0000-0000-0000-000000000000"

# Track per-endpoint results without failing the overall run when only one
# accepts the key — intl-scoped keys are rejected by the domestic endpoint
# (and vice versa). Passing on *either* host is sufficient.
probe_host() {
  local label="$1"
  local host="$2"
  local url="https://${host}/api/v1/tasks/${BOGUS_TASK}"
  local tmp
  tmp=$(mktemp)
  local code
  code=$(curl -s -o "$tmp" -w "%{http_code}" \
    -H "Authorization: Bearer $KEY" \
    -H "Accept: application/json" \
    "$url")

  case "$code" in
    404)
      pass "$label (HTTP 404 — key accepted, bogus task not found as expected)"
      REACHED=1
      ACCEPTED_HOST="$host"
      ;;
    200)
      pass "$label (HTTP 200 — key accepted)"
      REACHED=1
      ACCEPTED_HOST="$host"
      ;;
    400)
      pass "$label (HTTP 400 — key accepted, malformed-id rejected)"
      REACHED=1
      ACCEPTED_HOST="$host"
      ;;
    401)
      info "$label (HTTP 401 — key not scoped to this endpoint)"
      ;;
    403)
      info "$label (HTTP 403 — key lacks permission on this endpoint)"
      ;;
    000)
      warn "$label network error (no response from $host)"
      ;;
    *)
      warn "$label HTTP $code — unexpected; payload: $(head -c 200 "$tmp")"
      ;;
  esac
  rm -f "$tmp"
}

REACHED=0
ACCEPTED_HOST=""

echo
echo "-- endpoint probes (pass if EITHER endpoint accepts the key) --"
probe_host "dashscope-intl.aliyuncs.com" "dashscope-intl.aliyuncs.com"
probe_host "dashscope.aliyuncs.com     " "dashscope.aliyuncs.com"

if [ $REACHED -eq 0 ]; then
  err "neither DashScope endpoint accepted the key"
else
  info "use host: $ACCEPTED_HOST  (set DASHSCOPE_HOST in renderer config)"
fi

# --- remind about quota (not API-introspectable) ---------------------------
echo
info "free quota is not exposed by the API — seed budget.alibaba_quota_remaining"
info "in state/manifest.json (CLAUDE.md: 50–100 calls, expires 2026-07-19)"
info "model ids to render with:  wan-2.7-plus  (final)   wan-2.7-turbo  (iteration)"

# --- summary ---------------------------------------------------------------
echo
if [ $FAIL -eq 0 ]; then
  pass "ready"
  exit 0
else
  err "one or more checks failed — fix and re-run"
  exit 1
fi
