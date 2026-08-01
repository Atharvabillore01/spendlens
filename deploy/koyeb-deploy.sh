#!/usr/bin/env bash
#
# Deploy this app to a Koyeb Free Instance (512MB RAM / 0.1 vCPU / 2GB SSD).
#
# The app ships as one container: FastAPI serves the API and the built Vite
# bundle from the same origin, so there is no second service and no CORS.
#
# Why a staging copy instead of `koyeb deploy .`:
#   `koyeb deploy` uploads the directory to Koyeb's artifact store *before*
#   Docker's build context rules apply, so .dockerignore does not protect
#   anything at that point -- .env with live API keys would be uploaded as-is.
#   --archive-ignore-dir only excludes directories, never files, so it cannot
#   exclude .env either. Copying to a clean staging dir is the only way to
#   guarantee the secret never leaves this machine.
#
# Usage:
#   ./deploy/koyeb-deploy.sh              # deploy or redeploy
#   KOYEB_REGION=fra ./deploy/koyeb-deploy.sh
#
set -euo pipefail

APP="${KOYEB_APP:-spendlens}"
SERVICE="${KOYEB_SERVICE:-web}"
# Free Instances run in exactly one region: was (Washington, D.C.) or fra
# (Frankfurt). Anything else is rejected at deploy time, not at runtime.
REGION="${KOYEB_REGION:-was}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="$(mktemp -d)/spendlens"
trap 'rm -rf "$(dirname "$STAGE")"' EXIT

command -v koyeb >/dev/null 2>&1 || {
  echo "koyeb CLI not found. Install it with:" >&2
  echo "  brew install koyeb/tap/koyeb" >&2
  exit 1
}

# --- secrets ------------------------------------------------------------------
# Checked before the build rather than discovered five minutes into it. Missing
# keys are a warning, not an error: the OpenRouter -> Groq -> offline chain still
# answers, just from the scripted client every time.
have_secret() { koyeb secrets get "$1" >/dev/null 2>&1; }

ENV_ARGS=()
if have_secret openrouter-api-key; then
  ENV_ARGS+=(--env "OPENROUTER_API_KEY={{ secret.openrouter-api-key }}")
fi
if have_secret groq-api-key; then
  ENV_ARGS+=(--env "GROQ_API_KEY={{ secret.groq-api-key }}")
fi
if [ ${#ENV_ARGS[@]} -eq 0 ]; then
  cat >&2 <<'EOF'
WARNING: no LLM provider secret found on Koyeb, so every answer will come from
the offline path -- grounded numbers and real charts, but keyword-routed
question understanding. To get live models, create at least one secret and
re-run:

  koyeb secrets create openrouter-api-key --value 'sk-or-v1-...'
  koyeb secrets create groq-api-key       --value 'gsk_...'

(Mint a key for this demo specifically. The URL is public, so the key behind it
is effectively public too -- see DEPLOY.md.)

EOF
  read -r -p "Deploy offline-only anyway? [y/N] " reply
  [ "$reply" = "y" ] || [ "$reply" = "Y" ] || exit 1
fi

# --- storage mode -------------------------------------------------------------
# sql   -> transactions and logins live in Postgres (Supabase), so uploads and
#          accounts survive the instance being destroyed. Requires auth: the app
#          reports itself un-ready if it is serving multi-tenant data with
#          AUTH_REQUIRED=false, and it is right to.
# demo  -> stateless, reads the bundled workbook, no login. Nothing to persist,
#          so nothing is lost when the instance scales to zero.
MODE="${SPENDLENS_MODE:-sql}"

if [ "$MODE" = "sql" ]; then
  if ! have_secret database-url; then
    cat >&2 <<'EOF'
SPENDLENS_MODE=sql needs a database-url secret. From Supabase, take the
*connection pooler* string -- not the db.<ref>.supabase.co one, which resolves
to IPv6 only and is therefore unreachable from Koyeb -- and give it the
SQLAlchemy driver prefix:

  koyeb secrets create database-url --value \
    'postgresql+psycopg://postgres.<ref>:<password>@aws-1-<region>.pooler.supabase.com:6543/postgres'

An @ or : in the password must be percent-encoded (@ -> %40).
Or deploy the stateless demo instead:  SPENDLENS_MODE=demo ./deploy/koyeb-deploy.sh
EOF
    exit 1
  fi

  # A signing secret nobody chose is better than one somebody reused. Generated
  # once and kept in Koyeb; regenerating it invalidates every issued token.
  if ! have_secret jwt-secret; then
    echo "Creating jwt-secret..."
    koyeb secrets create jwt-secret --value "$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
  fi

  ENV_ARGS+=(
    --env STORAGE_BACKEND=sql
    --env "DATABASE_URL={{ secret.database-url }}"
    --env AUTH_REQUIRED=true
    --env "JWT_SECRET={{ secret.jwt-secret }}"
    # One instance, one worker. Supabase's free tier shares a modest pooled
    # connection budget, and a pool sized for a server that does not exist is
    # just a way to hit that ceiling.
    --env DB_POOL_SIZE=3
    --env DB_MAX_OVERFLOW=2
  )
else
  ENV_ARGS+=(
    --env STORAGE_BACKEND=dataframe
    --env AUTH_REQUIRED=false
  )
fi

# --- staging copy -------------------------------------------------------------
# Only what the Dockerfile actually COPYs, plus the frontend sources it builds.
#
# The leading slashes matter. An unanchored rsync pattern matches at every
# depth, so a bare `data/` silently takes `src/data/` with it -- the category
# taxonomy -- and the image then dies at import time with
# `ModuleNotFoundError: No module named 'src.data'`. Anchored patterns hit the
# project root only; unanchored ones (node_modules, __pycache__) are the ones
# that genuinely should match anywhere.
mkdir -p "$STAGE"
rsync -a \
  --exclude '/.env' \
  --exclude '/.envkey' \
  --exclude '/.venv/' \
  --exclude '/.git/' \
  --exclude '/output/' \
  --exclude '/data/' \
  --exclude '/.uploads/' \
  --exclude '/frontend/dist/' \
  --exclude 'node_modules/' \
  --exclude '__pycache__/' \
  --exclude '.pytest_cache/' \
  --exclude '.DS_Store' \
  --exclude '*.log' \
  --exclude '*.pdf' \
  "$ROOT"/ "$STAGE"/

# The image is useless without these, and the failure would otherwise surface as
# a crash loop five minutes into a remote build.
for required in src/data api.py Dockerfile assessment_transaction_data.xlsx frontend/package.json; do
  [ -e "$STAGE/$required" ] || { echo "staging dropped $required -- aborting" >&2; exit 1; }
done

echo "Staged $(du -sh "$STAGE" | cut -f1) for upload."

# --- deploy -------------------------------------------------------------------
# Storage comes from the mode block above. What is set here holds either way.
#
# Charts are the exception to persistence in both modes: they render to
# /app/output, which does not survive the instance. A chart URL is therefore
# good until the next cold start, not forever.
#
# The per-user rate limit is tightened from the default 20/min because the URL
# is public. In demo mode the limiter keys on a caller-supplied user_id, so it
# throttles honest traffic without stopping someone set on draining the quota;
# in sql mode it keys on the authenticated subject, which is a real bound.
koyeb deploy "$STAGE" "$APP/$SERVICE" \
  --archive-builder docker \
  --archive-docker-dockerfile Dockerfile \
  --instance-type free \
  --regions "$REGION" \
  --ports 8000:http \
  --routes /:8000 \
  --checks 8000:http:/healthz \
  --env SHOW_LOGIN_HINTS=false \
  --env CACHE_BACKEND=memory \
  --env LLM_PROVIDER=auto \
  --env OFFLINE_FALLBACK=true \
  --env AS_OF_MODE=data_max \
  --env RATE_LIMIT_PER_MINUTE=10 \
  --env RATE_LIMIT_BURST=5 \
  --env INGEST_RATE_LIMIT_PER_MINUTE=2 \
  --env MPLBACKEND=Agg \
  "${ENV_ARGS[@]}"

echo
echo "Watch the build:   koyeb service logs $APP/$SERVICE --type build"
echo "Watch the app:     koyeb service logs $APP/$SERVICE"
echo "Get the URL:       koyeb app get $APP"
