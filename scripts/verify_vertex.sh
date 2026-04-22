#!/usr/bin/env bash
# Preflight check for Vertex AI + Veo access. Costs nothing (read-only probes).
# Run from repo root:   bash scripts/verify_vertex.sh
# Exit 0 = ready to render. Non-zero = one or more checks failed; see output.

set -u

# Run from repo root regardless of where invoked.
cd "$(dirname "$0")/.."

FAIL=0
pass() { printf "\033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "\033[33m!\033[0m %s\n" "$1"; }
err()  { printf "\033[31m✗\033[0m %s\n" "$1"; FAIL=1; }
info() { printf "  %s\n" "$1"; }

echo "== rectoverso: Vertex + Veo preflight =="

# --- gcloud installed ------------------------------------------------------
if ! command -v gcloud >/dev/null 2>&1; then
  err "gcloud not installed — https://cloud.google.com/sdk/docs/install"
  exit 1
fi
pass "gcloud installed"

# --- active user account ---------------------------------------------------
ACTIVE_ACCT=$(gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>/dev/null | head -1)
if [ -z "$ACTIVE_ACCT" ]; then
  err "no active gcloud account — run: gcloud auth login"
  exit 1
fi
pass "authenticated as $ACTIVE_ACCT"

# --- ADC present -----------------------------------------------------------
ADC_PATH="$HOME/.config/gcloud/application_default_credentials.json"
if [ ! -f "$ADC_PATH" ]; then
  err "ADC missing — run: gcloud auth application-default login"
else
  pass "ADC credentials present"
fi

# --- ADC quota project set (critical — unset causes 403 SERVICE_DISABLED) --
if [ -f "$ADC_PATH" ] && grep -q '"quota_project_id"' "$ADC_PATH"; then
  QUOTA_PROJ=$(grep -o '"quota_project_id"[[:space:]]*:[[:space:]]*"[^"]*"' "$ADC_PATH" | sed 's/.*"\([^"]*\)"$/\1/')
  pass "ADC quota project: $QUOTA_PROJ"
else
  err "ADC quota project not set — run: gcloud auth application-default set-quota-project <PROJECT_ID>"
fi

# --- project id (from .env, fall back to gcloud config) --------------------
PROJECT_ID=""
if [ -f ".env" ] && grep -q '^GCP_PROJECT_ID=' .env; then
  PROJECT_ID=$(grep '^GCP_PROJECT_ID=' .env | head -1 | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs)
fi
if [ -z "$PROJECT_ID" ]; then
  PROJECT_ID=$(gcloud config get-value project 2>/dev/null)
fi
if [ -z "$PROJECT_ID" ]; then
  err "no project — set GCP_PROJECT_ID in .env or run: gcloud config set project <id>"
  exit 1
fi
pass "project: $PROJECT_ID"

LOCATION="${GCP_LOCATION:-us-central1}"
if [ -f ".env" ] && grep -q '^GCP_LOCATION=' .env; then
  ENV_LOC=$(grep '^GCP_LOCATION=' .env | head -1 | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs)
  [ -n "$ENV_LOC" ] && LOCATION="$ENV_LOC"
fi
info "location: $LOCATION"

# --- mint access token -----------------------------------------------------
TOKEN=$(gcloud auth application-default print-access-token 2>/dev/null)
if [ -z "$TOKEN" ]; then
  err "cannot mint access token from ADC"
  exit 1
fi
pass "access token obtainable"

# --- Vertex AI API enabled -------------------------------------------------
API_ENABLED=$(gcloud services list --enabled \
  --filter="config.name:aiplatform.googleapis.com" \
  --format="value(config.name)" \
  --project="$PROJECT_ID" 2>/dev/null)
if [ "$API_ENABLED" = "aiplatform.googleapis.com" ]; then
  pass "aiplatform.googleapis.com enabled"
else
  err "Vertex AI API not enabled — run: gcloud services enable aiplatform.googleapis.com --project=$PROJECT_ID"
fi

# --- IAM: aiplatform.user on this user -------------------------------------
IAM_CHECK=$(gcloud projects get-iam-policy "$PROJECT_ID" \
  --flatten="bindings[].members" \
  --filter="bindings.role:roles/aiplatform.user AND bindings.members:user:$ACTIVE_ACCT" \
  --format="value(bindings.role)" 2>/dev/null | head -1)
if [ -n "$IAM_CHECK" ]; then
  pass "user has roles/aiplatform.user"
else
  warn "user lacks roles/aiplatform.user (may still work if inherited via Owner/Editor)"
  info "grant explicitly: gcloud projects add-iam-policy-binding $PROJECT_ID \\"
  info "  --member=\"user:$ACTIVE_ACCT\" --role=\"roles/aiplatform.user\""
fi

# --- Gemini control probe (baseline: if this 403s, it's an API/IAM issue) --
echo
echo "-- baseline probe: Gemini (widely available) --"
GEMINI_URL="https://${LOCATION}-aiplatform.googleapis.com/v1/publishers/google/models/gemini-2.5-flash"
GEMINI_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $TOKEN" \
  -H "x-goog-user-project: $PROJECT_ID" "$GEMINI_URL")
case "$GEMINI_CODE" in
  200) pass "gemini-2.5-flash reachable (baseline OK)" ;;
  *)   err "gemini-2.5-flash returned HTTP $GEMINI_CODE — IAM/API problem, not a Veo-specific one" ;;
esac

# --- Veo model probes ------------------------------------------------------
echo
echo "-- Veo publisher model probes --"
MODELS=(
  "veo-3.1-fast-generate-001"
  "veo-3.1-generate-001"
  "veo-3.1-fast-generate-preview"
  "veo-3.1-generate-preview"
  "veo-3.0-fast-generate-001"
  "veo-3.0-generate-001"
  "veo-2.0-generate-001"
)
FOUND_ANY=0
for M in "${MODELS[@]}"; do
  URL="https://${LOCATION}-aiplatform.googleapis.com/v1/publishers/google/models/${M}"
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer $TOKEN" \
    -H "x-goog-user-project: $PROJECT_ID" "$URL")
  case "$HTTP_CODE" in
    200) pass "$M — reachable"; FOUND_ANY=1 ;;
    403) info "$M — 403 (not allowlisted for this project)" ;;
    404) info "$M — 404 (not in $LOCATION or retired)" ;;
    *)   info "$M — HTTP $HTTP_CODE" ;;
  esac
done

if [ $FOUND_ANY -eq 0 ]; then
  err "no Veo model reachable — request access at https://cloud.google.com/vertex-ai/generative-ai/docs/video/overview or try a different region"
else
  echo
  info "pick the highest 3.1 variant reachable above as VEO_MODEL_ID in .env"
fi

# --- summary ---------------------------------------------------------------
echo
if [ $FAIL -eq 0 ]; then
  pass "ready"
  exit 0
else
  err "one or more checks failed — fix and re-run"
  exit 1
fi
