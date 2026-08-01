# Two stages: the Node stage builds the frontend, the Python stage serves it.
# Keeping them separate means the runtime image carries no Node, no npm cache
# and no source — roughly 700MB of build tooling that would otherwise ship to
# production and widen the attack surface for nothing.

# ---- stage 1: build the UI --------------------------------------------------
FROM node:22-slim AS ui

WORKDIR /ui

# Manifests first. This layer is cached until a dependency actually changes, so
# editing a component does not re-run `npm ci`.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# ---- stage 2: runtime -------------------------------------------------------
FROM python:3.13-slim AS runtime

# PYTHONDONTWRITEBYTECODE: a read-only filesystem cannot write .pyc, and they
# are useless in a container that is replaced rather than restarted.
# PYTHONUNBUFFERED: without it, logs sit in a buffer and a crash loses them.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg

WORKDIR /app

# libgomp1 is required by numpy/scipy wheels; everything else matplotlib needs
# is already in the wheel. No build toolchain is installed, so a package that
# needs to compile will fail loudly here rather than silently bloating the image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY api.py demo.py cli.py manage_accounts.py verify_data.py stress.py ./
COPY assessment_transaction_data.xlsx ./
COPY --from=ui /ui/dist ./frontend/dist

# Non-root, and the two writable paths are created and owned before the switch.
# Charts and the SQLite fallback are the only things this process writes; every
# other path can be mounted read-only.
RUN useradd --create-home --uid 10001 ledger \
 && mkdir -p /app/output /app/data \
 && chown -R ledger:ledger /app/output /app/data
USER ledger

EXPOSE 8000

# Hits /healthz rather than /readyz on purpose: readiness reports degraded when
# an upstream model is unreachable, and Docker would then kill a container that
# is serving perfectly good degraded answers. Liveness asks "is this process
# broken", which is the question a restart can answer.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# One worker per container. Scale with replicas, not with in-container workers:
# the cache and rate limiter are per-process unless CACHE_BACKEND=redis, so
# several workers behind one container would each hold their own state.
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
