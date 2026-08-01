#!/usr/bin/env bash
#
# Push this deployment's configuration into a linked Vercel project.
#
# Reads the provider keys from the local .env so they are never typed, pasted
# into a shell history, or committed. Everything else is a literal, because a
# serverless deployment needs settings a container does not -- see the comments
# against each one.
#
# Usage:
#   npx vercel login && npx vercel link      # once
#   ./deploy/vercel-env.sh                   # then this
#   npx vercel deploy --prod
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

TARGET="${VERCEL_TARGET:-production}"
VERCEL="${VERCEL_CMD:-npx vercel}"

[ -f .env ] || { echo ".env not found; run this from a checkout that has one" >&2; exit 1; }

from_env() { grep "^$1=" .env | cut -d= -f2- | head -1; }

# `vercel env add` reads the value from stdin, which keeps it off the command
# line (and therefore out of `ps` and shell history). `rm` first so re-running
# updates rather than erroring on a name that already exists.
put() {
  local name="$1" value="$2"
  [ -n "$value" ] || { echo "  skip  $name (empty)"; return; }
  $VERCEL env rm "$name" "$TARGET" --yes >/dev/null 2>&1 || true
  printf '%s' "$value" | $VERCEL env add "$name" "$TARGET" >/dev/null
  echo "  set   $name"
}

echo "Configuring Vercel ($TARGET)…"

# --- LLM ----------------------------------------------------------------------
put OPENROUTER_API_KEY "$(from_env OPENROUTER_API_KEY)"
put GROQ_API_KEY       "$(from_env GROQ_API_KEY)"
put LLM_PROVIDER       "auto"      # OpenRouter first, Groq second
put OFFLINE_FALLBACK   "true"      # then scripted, rather than an error page
# A cold invocation has already spent seconds importing pandas and matplotlib
# before the model is called, and the function is capped at 60s. Leaving the
# 25s default plus retries risks the platform killing the request mid-answer.
put LLM_TIMEOUT_S      "15"
put LLM_MAX_RETRIES    "2"

# --- storage ------------------------------------------------------------------
put STORAGE_BACKEND "sql"
put DATABASE_URL    "$(from_env VERCEL_DATABASE_URL || true)"
put AUTH_REQUIRED   "true"
put SHOW_LOGIN_HINTS "false"
# Each invocation is its own process with its own pool. Sized at 1 because a
# pool per invocation multiplies against Supabase's free connection budget --
# the pooler is doing the real pooling here, not SQLAlchemy.
put DB_POOL_SIZE    "1"
put DB_MAX_OVERFLOW "1"

# --- serverless-specific ------------------------------------------------------
# The POST that renders a chart and the GET that would fetch it are not
# guaranteed to be the same instance, so the PNG travels in the response.
put CHART_DELIVERY   "inline"
# The only writable path in the runtime.
put CHART_OUTPUT_DIR "/tmp/charts"
put AS_OF_MODE       "data_max"

# --- shared state -------------------------------------------------------------
# Without this the rate limiter is decorative: every invocation starts with a
# full bucket, so a public URL has no effective limit on spending your LLM
# quota. With it, the limit is real.
REDIS_URL_VALUE="$(from_env REDIS_URL || true)"
if [ -n "$REDIS_URL_VALUE" ]; then
  put CACHE_BACKEND "redis"
  put REDIS_URL     "$REDIS_URL_VALUE"
else
  echo "  WARN  no REDIS_URL in .env -- rate limiting will not bind across"
  echo "        invocations. Create a free Upstash Redis, put its rediss:// URL"
  echo "        in .env as REDIS_URL, and re-run."
  put CACHE_BACKEND "memory"
fi
put RATE_LIMIT_PER_MINUTE "10"
put RATE_LIMIT_BURST      "5"

# --- auth ---------------------------------------------------------------------
JWT="$(from_env JWT_SECRET || true)"
[ -n "$JWT" ] || JWT="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
put JWT_SECRET "$JWT"

echo
echo "Done. Deploy with:  npx vercel deploy --prod"
