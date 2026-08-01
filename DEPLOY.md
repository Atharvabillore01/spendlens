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
brew install koyeb/tap/koyeb
koyeb login
```

Then load the provider keys as secrets. Both are optional — see *What happens
without keys* below — but a live demo wants at least one:

```bash
koyeb secrets create openrouter-api-key --value 'sk-or-v1-...'
koyeb secrets create groq-api-key       --value 'gsk_...'
```

And, for the default `sql` mode, the database (see *Persistence* below):

```bash
koyeb secrets create database-url --value \
  'postgresql+psycopg://postgres.<ref>:<password>@aws-1-<region>.pooler.supabase.com:6543/postgres'
```

`jwt-secret` is generated for you on first deploy if it does not exist.

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

## Persistence: Supabase Postgres

Two modes, chosen with `SPENDLENS_MODE`:

```bash
./deploy/koyeb-deploy.sh                       # sql — the default
SPENDLENS_MODE=demo ./deploy/koyeb-deploy.sh   # stateless, no login
```

**`sql`** puts transactions and logins in Supabase, so uploaded data and
accounts survive the instance being destroyed — which happens on every scale to
zero, since a Free Instance cannot mount a volume.

**This forces authentication on.** `/readyz` reports the service un-ready if it
is serving multi-tenant data with `AUTH_REQUIRED=false`, and that check is
correct: without a token the `user_id` comes from the request body, so anyone
could read anyone. So `sql` mode means visitors need a login, and the deployment
is no longer a click-and-try demo. If you want the click-and-try version, use
`SPENDLENS_MODE=demo` — with the bundled workbook there is nothing to persist in
the first place.

### Getting the connection string right

Take the **connection pooler** string from Supabase, not the one on the
project's main connection page:

- `db.<ref>.supabase.co` resolves to **IPv6 only**. Koyeb egress is IPv4, so this
  host is simply unreachable from the deployed container — it will connect fine
  from a laptop and fail in production.
- `aws-1-<region>.pooler.supabase.com:6543` is IPv4 and is what to use. Note
  `aws-1`, not `aws-0`, for recent projects; the region here is the project's,
  not necessarily the nearest one.
- The username becomes `postgres.<project-ref>`, not `postgres`.
- Percent-encode the password (`@` → `%40`).
- Prefix the scheme with the driver: `postgresql+psycopg://`.

Port 6543 is the *transaction* pooler, which hands the server connection back
after every transaction. psycopg3 prepares a statement server-side after seeing
it five times, and that prepare does not survive the handback — so around the
sixth identical query you get `prepared statement "_pg3_0" does not exist`,
under load, having passed every test. [`src/db/engine.py`](src/db/engine.py)
detects a transaction pooler and sets `prepare_threshold=None` to prevent it.

### Seeding

Once, against the Supabase URL:

```bash
STORAGE_BACKEND=sql DATABASE_URL='postgresql+psycopg://...' \
  python manage_accounts.py seed --password '<pick-one>'
```

Creates the schema, loads the workbook, and makes five logins: three account
holders, one manager, one admin.

### Supabase free-tier limits worth knowing

- Projects **pause after 7 days** with no activity, and must be resumed from the
  dashboard. A paused database means `/readyz` returns 503 and every query
  fails — this deployment can sleep for a week very easily.
- 500MB storage, 5GB egress. This dataset is 347 rows; not a concern.

## What the script sets, and why

| Setting | Value | Reason |
|---|---|---|
| `STORAGE_BACKEND` | `sql` / `dataframe` | Per mode, above. |
| `AUTH_REQUIRED` | `true` in sql mode | Not optional — the app refuses readiness without it when serving multi-tenant data. |
| `DB_POOL_SIZE` | `3` (+2 overflow) | One instance, one worker. Supabase's free pooled-connection budget is modest, and a pool sized for servers that do not exist is just a way to hit the ceiling. |
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
- **Two free tiers, two idle timers.** Koyeb destroys the instance after 1 hour
  idle; Supabase pauses the project after 7 days idle. The first costs a slow
  first request, the second costs a manual resume in the dashboard.

## If you outgrow the free tier

`docker-compose.yml` is the production-shaped version: Postgres for
transactions, Redis so the cache and rate limiter stay correct across replicas,
`AUTH_REQUIRED=true`. The application-level move has already been made by
running in `sql` mode — what is left is a paid instance that does not sleep, a
Redis for shared cache and rate-limit state, and object storage for charts.
