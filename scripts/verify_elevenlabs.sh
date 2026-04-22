#!/usr/bin/env bash
# Preflight check for ElevenLabs access + remaining credits.
# Run from repo root:   bash scripts/verify_elevenlabs.sh
# Exit 0 = key valid and credits visible. Non-zero = one or more checks failed.

set -u

cd "$(dirname "$0")/.."

FAIL=0
pass() { printf "\033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "\033[33m!\033[0m %s\n" "$1"; }
err()  { printf "\033[31m✗\033[0m %s\n" "$1"; FAIL=1; }
info() { printf "  %s\n" "$1"; }

echo "== rectoverso: ElevenLabs preflight =="

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

if [ -z "${ELEVENLABS_API_KEY:-}" ]; then
  err "ELEVENLABS_API_KEY not set in .env"
  exit 1
fi

KEY="$ELEVENLABS_API_KEY"
if [ "${#KEY}" -gt 12 ]; then
  info "ELEVENLABS_API_KEY = ${KEY:0:6}…${KEY: -4}"
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

# --- /v1/models (TTS-capable keys can always hit this; no extra scope) ----
MODELS_CODE=$(curl -s -o "$TMP" -w "%{http_code}" \
  -H "xi-api-key: $KEY" \
  -H "Accept: application/json" \
  "https://api.elevenlabs.io/v1/models")

case "$MODELS_CODE" in
  200)
    pass "/v1/models reachable; key authenticates for TTS"
    ;;
  401|403)
    err "HTTP $MODELS_CODE on /v1/models — key invalid or lacks TTS permissions"
    info "  payload: $(head -c 300 "$TMP")"
    rm -f "$TMP"; trap - EXIT
    exit 1
    ;;
  *)
    err "HTTP $MODELS_CODE on /v1/models — unexpected; payload: $(head -c 200 "$TMP")"
    rm -f "$TMP"; trap - EXIT
    exit 1
    ;;
esac

# --- /v1/user (credit introspection — requires user_read permission) ------
# Production-scoped keys often omit user_read. That's fine: we fall back to
# trusting budget.elevenlabs_credits_remaining in the manifest.
USER_CODE=$(curl -s -o "$TMP" -w "%{http_code}" \
  -H "xi-api-key: $KEY" \
  -H "Accept: application/json" \
  "https://api.elevenlabs.io/v1/user")

case "$USER_CODE" in
  200)
    if command -v python3 >/dev/null 2>&1; then
      python3 - "$TMP" <<'PY'
import json, sys, pathlib
data = json.loads(pathlib.Path(sys.argv[1]).read_text())
sub = data.get("subscription", {}) or {}
used  = sub.get("character_count", 0)
limit = sub.get("character_limit", 0)
tier  = sub.get("tier", "unknown")
remaining = max(0, limit - used)
print(f"  tier:       {tier}")
print(f"  used:       {used:,} characters")
print(f"  limit:      {limit:,} characters")
print(f"  remaining:  {remaining:,} characters")
if remaining < 10000:
    print("  \033[33m!\033[0m remaining credits < 10k — may run out mid-run")
PY
    else
      warn "python3 not found — skipping credit parse; raw:"
      head -c 400 "$TMP"; echo
    fi
    ;;
  401|403)
    # Check if this is the known "missing user_read permission" case.
    if grep -q "missing_permissions" "$TMP" 2>/dev/null; then
      warn "/v1/user blocked by scope (missing user_read permission) — key is TTS-only"
      info "cannot auto-introspect credits; track budget.elevenlabs_credits_remaining in manifest"
      info "(CLAUDE.md seed: 117,999 credits)"
    else
      err "HTTP $USER_CODE on /v1/user — unexpected; payload: $(head -c 200 "$TMP")"
    fi
    ;;
  *)
    warn "HTTP $USER_CODE on /v1/user — cannot read credit balance; manifest counter only"
    ;;
esac

# --- summary -------------------------------------------------------------
echo
if [ $FAIL -eq 0 ]; then
  pass "ready"
  exit 0
else
  err "one or more checks failed — fix and re-run"
  exit 1
fi
