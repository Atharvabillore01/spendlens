# Deploying SpendLens for free (Koyeb)

One container: FastAPI serves the API and the built Vite bundle from the same
origin, so there is no second service, no CORS, and nothing to keep in sync
between a frontend host and a backend host.

Target is a **Koyeb Free Instance** — 512MB RAM, 0.1 vCPU, 2GB SSD, one per
account, Washington D.C. or Frankfurt only. Measured footprint of this app,
idle and after serving a charted query: **~122MB**, so memory is not the
constraint. The 0.1 vCPU is: expect a few seconds for a matplotlib render.

---

## Once

```bash
brew install koyeb/tap/koyeb-cli
koyeb login
```

Then load the provider keys as secrets. Both are optional — see *What happens
without keys* below — but a live demo wants at least one:

```bash
koyeb secrets create openrouter-api-key --value 'sk-or-v1-...'
koyeb secrets create groq-api-key       --value 'gsk_...'
```

**Mint keys for this deployment specifically.** The URL is public and
unauthenticated, so anyone who finds it spends your free-tier quota. Nothing in
the deployment can prevent that — the per-user rate limit keys on a `user_id`
the caller supplies, which throttles honest traffic but not a determined one.
Treat these keys as burnable and rotate them when the demo is done.

## Every deploy

```bash
./deploy/koyeb-deploy.sh
```

That uploads the project as an archive, builds the Dockerfile on Koyeb's
builder, and deploys it. Redeploys are the same command.

```bash
KOYEB_REGION=fra ./deploy/koyeb-deploy.sh   # Frankfurt instead of Washington
KOYEB_APP=spendlens-staging ./deploy/koyeb-deploy.sh
```

Watch it:

```bash
koyeb service logs spendlens/web --type build   # the build
koyeb service logs spendlens/web                # the running app
koyeb app get spendlens                         # the URL
```

Then check `https://<your-url>/readyz`. It reports which providers are wired,
whether the circuit breaker is open, and whether answers are currently coming
from the offline path.

---

## What the script sets, and why

| Setting | Value | Reason |
|---|---|---|
| `STORAGE_BACKEND` | `dataframe` | Transactions come from the bundled workbook. Free Instances scale to zero after an hour idle and cannot mount a volume, so anything written to disk is gone by the next request. Stateless is the only correct shape here. |
| `AUTH_REQUIRED` | `false` | Public demo. The frontend treats this as "signed in with nothing special" and asks for a `user_id` per query instead of a login. |
| `SHOW_LOGIN_HINTS` | `false` | Never publish working credentials, even demo ones. |
| `CACHE_BACKEND` | `memory` | One instance, so a shared Redis buys nothing. |
| `RATE_LIMIT_PER_MINUTE` | `10` | Tightened from the default 20 because the URL is public. |
| `OFFLINE_FALLBACK` | `true` | The third link in the chain, below. |
| `LLM_PROVIDER` | `auto` | OpenRouter first, Groq second. |

`.env` is **never** uploaded. `koyeb deploy` archives the directory before
Docker's build context rules apply, so `.dockerignore` does not protect it at
that stage and `--archive-ignore-dir` only excludes directories. The script
therefore copies to a clean staging directory and deploys that.

---

## The answer chain: OpenRouter → Groq → offline

Every model on OpenRouter is tried in order, then every model on Groq, then the
scripted client answers rather than the request failing.

This is not theoretical. Building this, OpenRouter's free tier returned
`429 free-models-per-day` on all three models and the request was served by
Groq's `llama-3.3-70b-versatile` — the second link doing exactly its job. The
third link exists for when both are spent, which on free tiers is a matter of
when, not if.

What an offline answer is and is not:

- **Numbers are real.** The same tools run against the same data, so the totals
  and charts are exactly what a live model would have been handed.
- **Understanding is weaker.** Which tool to call is chosen by keyword routing,
  not by a model, so an unusually phrased question gets a blunter reading.
- **It never pretends.** `model_used` comes back as `scripted/offline`, and
  `/readyz` reports `"llm_degraded": true` while it is happening.

The circuit breaker still records live failures, so falling back does not hide
an outage from readiness. Set `OFFLINE_FALLBACK=false` to get a hard
`LLMUnavailableError` back instead.

### What happens without keys

The service starts, serves the UI, and answers every query offline —
`"llm_live": false` in `/readyz`, `scripted/offline` on every response. Useful
for showing the pipeline shape with zero quota; not a demo of the model
reasoning.

---

## Known limits of this deployment

- **Cold starts.** Free Instances scale to zero after an hour with no traffic.
  The first request afterwards waits for a microVM boot plus Python import —
  tens of seconds. Every request after that is warm.
- **Charts are per-instance.** They render to `/app/output` and are served from
  there with a signed URL. Fine on one instance; the moment there are two, a
  chart rendered by A is not on B. Object storage is the fix, not more replicas.
- **One free instance per account.** A staging copy alongside production means
  paying for one of them.
- **Uploads do not persist.** `/ingest` needs `STORAGE_BACKEND=sql`; in
  dataframe mode it refuses rather than accepting data it cannot keep.

## If you outgrow the free tier

`docker-compose.yml` is the production-shaped version: Postgres for
transactions, Redis so the cache and rate limiter stay correct across replicas,
`AUTH_REQUIRED=true`. Moving there is an env-var change and a database URL —
set `STORAGE_BACKEND=sql`, point `DATABASE_URL` at a managed Postgres, seed
accounts with `manage_accounts.py`, and drop `AUTH_REQUIRED=false`.
