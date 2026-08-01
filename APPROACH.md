# Approach — Transaction RAG Pipeline

How this system was designed, what we chose to make deterministic, what we left to
the model, and why. Complements the two existing documents:

| Document | Answers |
|---|---|
| [ARCHITECTURE_AND_PLAN.md](ARCHITECTURE_AND_PLAN.md) | What we *planned* to build, before writing code |
| [README.md](README.md) | How to run it, and what each module does |
| **APPROACH.md** (this file) | *Why* it is shaped this way, and where it goes next |

---

## 1. The problem, stated precisely

Given a `user_id` and a free-text question, return a grounded analytical answer about
that user's transactions, plus a chart, in one call:

```python
pipeline.run(user_id="usr_a1b2c3d4", prompt="What did I spend the most on last month?")
```

Three constraints shaped every decision:

1. **The data is small and tabular.** 347 rows, 3 users, 6 columns. There is no
   semantic-search problem here — the "retrieval" in *retrieval-augmented generation*
   is a Pandas filter, not a vector search.
2. **The answer contains money.** A wrong number is worse than no number. Financial
   figures are not a domain where "usually right" is acceptable output.
3. **The model tier is free.** The default chain is free OpenRouter models
   (`inclusionai/ling-3.0-flash:free` → `google/gemma-4-31b-it:free` →
   `openai/gpt-oss-20b:free`). These are rate-limited, occasionally unavailable, and
   noticeably worse at instruction-following than frontier models. The architecture
   has to be correct *despite* the model, not because of it.

Those three together produced the central design rule.

---

## 2. The central design rule: the model chooses, Pandas computes

> **No number in the user-facing response is ever produced by the language model.**

The LLM's entire job is to (a) decide which analysis the question calls for, and
(b) narrate figures that were handed to it. It never sees a transaction row, never
performs arithmetic, and never invents a total.

This split is enforced structurally, not by prompting:

| Responsibility | Owner | Failure mode if it drifts |
|---|---|---|
| Which tool, which period, which category | LLM (tool call) | Wrong chart — visible, recoverable |
| Filtering, grouping, summing | Pandas ([`UserDataStore`](src/data/user_data_store.py)) | Impossible to get wrong via prompt |
| Rendering the chart | Matplotlib ([`visualizations.py`](src/tools/visualizations.py)) | Deterministic |
| The prose around the numbers | LLM (narration round-trip) | Caught by output guardrails |
| Verifying every printed figure | [`OutputGuardrails`](src/guardrails/output_guardrails.py) | Ungrounded numbers stripped |

The consequence: a hallucinated figure is not a bug we hope the prompt prevents. It is
a value that fails a numeric membership test and gets removed before the user sees it.

### The narration round-trip

The most important mechanical detail. After tools execute, we do **not** let the model's
first response stand — it was written before it knew any numbers. Instead
([`pipeline.py:329-347`](src/pipeline.py#L329-L347)) we send a second call containing the
real computed results as `tool` messages, with `tool_choice="none"`:

```
turn 1  →  model: "I'll break down last month"  + tool_call(plot_category_breakdown, period=last_month)
           ↓ Pandas executes, chart rendered, summary computed
turn 2  →  model receives {top_category: HOUSING, amount: 2122.0, share_pct: 68.5, ...}
        ←  model: "Last month you spent the most on Housing — $2,122.00, 68.5% of your total…"
```

The prose is therefore written *from* computed values rather than from the model's
recollection of the conversation. Everything it can cite is already grounded.

---

## 3. Reading the data honestly

Two properties of the delivered spreadsheet contradict the brief, and both would have
silently broken the headline query if taken at face value.

### 3.1 The taxonomy is flat, not hierarchical

The brief describes `Food > Restaurants > Fast Food`. The file contains 27 flat values in
`SUBCATEGORY_PARENT` form: `RENT_HOUSING`, `FASTFOOD_FOOD`, `SALARY_INCOME`. No value
contains more than one underscore, so `rsplit("_", 1)` is unambiguous.

We derive the hierarchy rather than hardcode it.
[`CategoryTaxonomy`](src/data/category_taxonomy.py) computes the parent vocabulary from
the data at load time — 11 parents (`EDUCATION … TRAVEL`) — and the tool schemas'
category enum is *generated from that vocabulary*. A new category in a future data
refresh is picked up with zero code change, and the model is told about it automatically.

### 3.2 The dataset is frozen in the past

Data ends **2025-12-31**; the process runs at today's wall-clock date. Anchored to
`datetime.now()`, "last month" returns an empty DataFrame and every 6-month lookback
returns nothing — the assessment's own headline query would answer "you have no data".

Every relative date resolves against an explicit `as_of` anchor, defaulting to
`max(transaction_date)` and overridable via `AS_OF_DATE`. One helper,
[`resolve_period`](src/data/periods.py), owns every phrase and normalizes whatever the
model emits (`"last month"`, `"Last 3 Months"`, `"last-6-months"`, `"year to date"`),
falling back to a default rather than raising on anything unrecognised.

### 3.3 Sign convention

`transaction_amount` is a signed integer: **negative = income, positive = expense**. This
was verified against the file, not assumed — all 54 negative rows are `*_INCOME`, and no
`*_INCOME` row is positive. The convention is applied in exactly one place
([`UserDataStore`](src/data/user_data_store.py)) so no downstream module can invert it.

---

## 4. The pipeline, stage by stage

```
run(user_id, prompt)
 │
 ├─ 1a  validate_user ──────────────► unknown? structured error dict, never an exception
 ├─ 0   InputGuardrails.check ─────► blocked? return refusal. No data touched, no LLM call, no cost.
 ├─ 1b  UserCache.get_or_build_profile ─► sets cache_hit; builds from Pandas on miss
 ├─ 2   PromptBuilder.build ───────► profile + few-shot history + viz_state, token-budgeted
 ├─ 3   OpenRouterClient.complete ─► retry → model fallback → circuit breaker
 │      ToolDispatcher.dispatch ───► validate args, force user_id, render PNGs
 │      narration round-trip ──────► real numbers handed back to be narrated
 ├─ 4   OutputGuardrails.check ────► hallucination / toxicity / confidence
 ├─ 5   cache write ───────────────► query_history ring buffer + viz_state
 └─ 6   AuditLogger.record ────────► PII-redacted, then return
```

**Nothing in `run()` raises to the caller.** Every failure path has a defined degraded
response. That is a deliberate contract: a service surface that throws forces every
consumer to reimplement error handling.

### Ordering choices worth noting

- **User validation precedes guardrails**, because the cross-user guardrail needs the
  current user's *name* to detect "show me Sarah's spending" when the caller is Sarah.
- **Guardrails precede everything expensive.** A blocked prompt costs zero tokens and
  loads zero data.
- **The empty-data check precedes prompt assembly.** No point spending a model call to
  discover there are no rows.

---

## 5. Retrieval and context assembly

The "RAG" here is a per-turn budgeted assembly of four sources
([`prompt_builder.py`](src/llm/prompt_builder.py)):

| Block | Source | Purpose |
|---|---|---|
| Role + schema | Static | What the assistant is, what columns exist |
| User profile | `user:{id}:profile` (cached) | Totals, top categories, date range, busiest month |
| Few-shot history | `user:{id}:query_history` (last 5) | Prior prompt → Pandas operation → result, so the model imitates the *shape* of a good answer |
| Viz state | `user:{id}:viz_state` (1h TTL) | What was last charted, so "show me that as a trend" resolves |
| As-of block | Computed | Pins "today" to the data anchor |

Budget: 6000 input tokens, estimated at 4 chars/token. When it overflows, the builder
degrades in a fixed order — history first, then profile detail — and raises the
`context_trimmed` flag rather than silently truncating mid-block.

The few-shot history block is the piece that does the most work per token. Showing the
model three prior turns of *this user's* prompts alongside the Pandas operation each one
resolved to is a far stronger signal for tool selection than any amount of instruction text.

---

## 6. Tool calling, and surviving bad tool calls

Three tools, each a chart plus its computed summary:

| Tool | Answers |
|---|---|
| `plot_category_breakdown` | "What did I spend the most on?" |
| `plot_monthly_spending_trend` | "How has my spending changed?" |
| `plot_income_vs_expense` | "Am I saving money?" |

Free-tier models emit malformed tool calls regularly. Rather than treat that as an error
path, [`ToolDispatcher`](src/llm/tool_dispatcher.py) treats it as the expected case and
repairs in layers:

1. **Parse tolerance** — arguments arriving as a JSON string, a double-encoded string, or
   fenced in ```` ```json ```` are all unwrapped.
2. **Type coercion** — `"6"` → `6`, `"true"` → `True`, `"Last 3 Months"` → `last_3_months`,
   category strings matched case-insensitively against the derived taxonomy. Each repair
   raises `tool_args_repaired` rather than failing.
3. **`user_id` is always overwritten**, never read from the model's arguments. A model
   that tries to chart another user's data gets the caller's data instead. This is the
   hard boundary; the input guardrail is the polite one.
4. **One corrective retry** — if *every* call was rejected, we re-ask once with an explicit
   correction message ([`pipeline.py:297-316`](src/pipeline.py#L297-L316)), then fall back
   to a text answer. We never loop.

Unknown tool names and execution failures are flagged and dropped, not raised.

---

## 7. Guardrails

### Input — before the LLM, in priority order

| Check | Behaviour |
|---|---|
| Length | Truncate at 2000 chars, user-visible notice, `prompt_truncated` |
| Prompt injection | Pattern-matched ("ignore previous instructions", "you are now…", role-hijack, exfiltration) → refuse, `injection_detected` |
| Cross-user access | Another user's id or name in the prompt → refuse, `cross_user_access_attempt` |
| Scope | No finance vocabulary and matches off-topic patterns → redirect, `scope_violation` |
| Empty | Redirect, `empty_prompt` |

The finance vocabulary is *extended with the derived taxonomy*, so asking about a category
that exists in the data is never mistaken for off-topic.

### Output — before the user

**Hallucination check.** Every number in the response is extracted and tested for
membership in the turn's grounding set (2% relative / $1 absolute tolerance). Sentences
containing an ungrounded figure are removed; if that empties the response, a
deterministic Pandas-composed summary is substituted.

The grounding set is deliberately **narrow** — only figures the executed tools computed,
figures in the composed `data_summary`, and figures from the cached profile that were in
the prompt. The tempting alternative, grounding against every possible aggregate, was
rejected: an exhaustive set of every window × category × merchant figure is so dense that
almost any plausible number lands within tolerance of *something*, which makes the check
theatre. See the note at [`pipeline.py:379-394`](src/pipeline.py#L379-L394).

**Toxicity** filtering and **confidence** detection (hedging markers → `low_confidence`)
round it out.

### Operational

- **Retry with exponential backoff** on 408/409/425/429/5xx — 3 attempts, 0.5s base, ×2.
- **Model fallback chain** — on exhaustion, the next model in the chain is tried.
- **Circuit breaker** — 3 consecutive failures opens it for 60s (CLOSED / OPEN / HALF_OPEN),
  so a provider outage fails fast instead of burning 25s timeouts per request.

---

## 8. The degradation ladder

Every layer below is reachable and tested; the system never returns a stack trace.

| Failure | Response |
|---|---|
| Unknown user | Structured `{"error": "user_not_found", …}` dict |
| Prompt blocked | Refusal text, no data or model access |
| User has no transactions | Plain explanation, `no_data_for_query` |
| LLM unreachable on turn 1 | **Heuristic chart selection** from prompt keywords + deterministic Pandas prose, `llm_unavailable`, `degraded: true` |
| LLM unreachable on narration turn | Deterministic prose substituted for the model's |
| All tool calls malformed | One corrective retry, then plain-text answer |
| Tool returns empty | Honest "no data in that window" with the reason |
| Response fully stripped by guardrails | Deterministic summary substituted |

The degraded mode is the one worth highlighting: with the network off entirely,
`python demo.py --offline` still answers every query with correct figures and a correct
chart. The model is an enhancement to the phrasing, not a dependency for the analysis.

---

## 9. Observability, and an adversarial self-check

Standard surface: PII-redacted structured audit log per turn (latency, flags, cache hit,
model, tool calls), `/healthz` and `/readyz`, circuit-breaker state exposed in `health()`.

The non-standard piece is [`src/observability/verify.py`](src/observability/verify.py).
It recomputes each headline figure **straight from the raw spreadsheet columns using its
own independent logic** — re-deriving the sign convention, the parent-category split and
the period window itself rather than calling `UserDataStore` or `CategoryTaxonomy`.

That independence is the entire value. A checker built on the same data layer would
reproduce any bug in that layer identically and agree with itself, proving nothing. Two
independent computations agreeing means the number the user saw is the number in the file.

---

## 10. Testing strategy

**350 tests, no network, ~3 seconds.** The suite is fast enough to run on every save,
which is the property that actually gets tests run.

The enabling decision is `FakeOpenRouterClient` in [`tests/conftest.py`](tests/conftest.py):
a scripted stand-in that returns a list of `LLMResponse`s (or raises exceptions) in order
and records every call. This makes otherwise-awkward scenarios into ordinary unit tests —
malformed tool arguments, mid-turn outages, models that ignore the schema, models that
hallucinate a figure — all deterministic and instant.

| File | Covers |
|---|---|
| `test_data_layer.py` | Sign convention, taxonomy derivation, period resolution |
| `test_cache.py` | TTL expiry, ring buffer, copy isolation, invalidation |
| `test_llm.py` | Retry, backoff, fallback chain, breaker transitions |
| `test_tools.py` | Chart outputs, summaries, empty windows |
| `test_guardrails.py` | Injection, cross-user, scope, hallucination stripping |
| `test_pipeline_e2e.py` | Full turns across ≥2 users, every degradation path |
| `test_api.py` | HTTP contract |
| `test_verify.py` | The independent cross-check itself |
| `test_live_openrouter.py` | Real API, skipped unless `RUN_LIVE_TESTS=1` |

**Current status: 350 passed, 7 skipped, 0 failed** (~3.2s). The seven skips are the live
OpenRouter tests, which run only under `RUN_LIVE_TESTS=1`.

`test_verify.py` is worth a note, because it depends on a fixture subtlety. The shared
`pipeline` fixture's `FakeOpenRouterClient` has no script, so it returns plain text with
**no tool calls** — which means no tools execute, no figures are produced, and the
cross-check has nothing to check. That module therefore builds its own `routed` fixture on
`demo.offline_client()`, a `FakeOpenRouterClient` subclass that routes each prompt to a
real tool call and then narrates from the payload the pipeline hands back. Any new test
that asserts on computed figures needs the same treatment.

---

## 11. Interfaces

Four surfaces over one pipeline, all sharing a single `TransactionRAGPipeline` instance:

- **`api.py`** — FastAPI. `POST /query`, `GET /users`, `GET /users/{id}/cache`,
  `DELETE /users/{id}/cache`, `/charts/{file}`, `/healthz`, `/readyz`. Serves the built
  frontend at `/`.
- **`frontend/`** — React 19 + Vite + TypeScript, with a react-three-fiber hero field.
  Passes `theme` through to `run()` so dark mode gets charts rendered on a dark surface
  rather than a white rectangle.
- **`cli.py`** — terminal chat loop.
- **`demo.py`** — the assessment's §7 query matrix, live or `--offline`.

`chart_theme` is the only addition to the brief's `run(user_id, prompt)` signature, and
it is keyword-optional, so the specified contract is unchanged.

---

## 12. Configuration philosophy

Every tunable lives in [`src/config.py`](src/config.py) as a typed pydantic
`BaseSettings` field with an env override. There are no magic numbers anywhere else:
timeouts, retry counts, TTLs, ring-buffer size, token budgets, hallucination tolerances,
breaker thresholds, chart DPI, the `as_of` anchor and the model chain are all one env var
away from being changed without touching code.

The model chain in particular is shipped as a *default*, not a constant — free-tier
availability on OpenRouter shifts often, and `MODEL_FALLBACK_CHAIN` exists so that
shifting doesn't require a release.

---

## 13. Would a multi-agent approach help here?

Short answer: **not for the current workload — the single-agent, deterministic-tool design
is the right call.** But there are three specific extensions where it starts to pay, and
the codebase is already shaped to accept them. Details below.

### 13.1 What we have today

One model in the loop, called at most three times per turn (initial → optional correction
→ narration), around a deterministic tool layer. This is "agentic" in the meaningful
sense — the model autonomously chooses tools and arguments — but it is a **single agent
with a bounded, non-iterative loop**.

### 13.2 Why more agents would not help *this* problem

| Multi-agent normally pays off when… | Here |
|---|---|
| Retrieval is expensive, ambiguous, or parallelizable across sources | Retrieval is one Pandas `groupby` over 347 rows — sub-millisecond, exact |
| Sub-tasks need genuinely different context that would blow one window | Total context is ~6k tokens including few-shot history |
| Sub-tasks need different tools or permissions | Three tools, one permission scope (one user's rows) |
| Verification needs judgement | Verification is **numeric membership testing** — deterministic, free, and strictly better than an LLM judge |

That last row is the strongest argument. The obvious place to bolt on a "critic agent" is
hallucination checking — and that is exactly where we already have something better. A
critic agent asked "is $2,122 supported by this data?" is a probabilistic check that costs
a round-trip, can be talked out of its answer, and is itself a hallucination surface.
`is_grounded()` is a set-membership test with a tolerance: it costs nothing, cannot be
argued with, and never produces a false confident "yes".

The costs are also real. Each additional agent adds a full round-trip (~1.5–4s on free-tier
models), another parse surface for malformed output, another failure mode needing a
degradation path, and another place where the numbers could drift from the spreadsheet.
Latency today is ~4.7s end-to-end; a planner + analyst + narrator + critic topology would
plausibly be 12–20s for an answer that is *already exact*.

### 13.3 Where it genuinely would pay — three concrete cases

**(a) Multi-hop comparative questions.** *"Why did my spending jump in March versus
February, and is subscription creep the cause?"* This needs several tool calls whose
arguments depend on earlier results — March breakdown, February breakdown, diff, then a
merchant-level drill into the largest mover. The current design fires **one round** of
tool calls and narrates. This is the real gap.

The right fix is not multi-agent, though — it is an **agentic loop**: let the same agent
call tools repeatedly until it declares itself done, with a hard step cap (say 5) and the
existing budget/guardrail machinery unchanged. That captures most of the value at a
fraction of the complexity. `_reason_and_dispatch()` is already the natural place; it
becomes a bounded `while` rather than a straight line.

**(b) Heterogeneous data sources.** If this grew to span bank transactions + email receipts
+ budget goals + market data, separate retrieval agents with their own contexts and tools
would earn their keep, with a synthesizer merging findings. That is a genuine
context-isolation problem, which is what multi-agent is *for*. With one spreadsheet, it is
not.

**(c) Advice quality, not number accuracy.** A reviewer agent judging tone, actionability
and whether the answer actually addressed the question — things with no deterministic
oracle — is a defensible addition. It should never be given authority over figures.

### 13.4 If we did build it — the shape

The existing seams make this a small change rather than a rewrite. Deterministic tools stay
exactly as they are and become the **shared tool layer** every agent calls; guardrails stay
at the boundary; the audit logger already records per-tool-call.

```
                    ┌──────────────────┐
   prompt ─────────►│  Planner agent   │  decomposes into ≤5 analysis steps
                    │  (small, fast)   │  cheapest model in the chain
                    └────────┬─────────┘
                             │ step list
                ┌────────────┴────────────┐
                ▼                         ▼
        ┌───────────────┐         ┌───────────────┐
        │ Analyst agent │  ...    │ Analyst agent │   parallel, one per step
        │  tools only   │         │  tools only   │   no prose, tool calls only
        └───────┬───────┘         └───────┬───────┘
                └────────────┬────────────┘
                             ▼
                    ┌──────────────────┐
                    │ Synthesizer      │  narrates from computed values only
                    │ tool_choice=none │  (today's narration round-trip)
                    └────────┬─────────┘
                             ▼
                    OutputGuardrails  ← unchanged, still the hard numeric gate
```

Three things must hold for this to be an improvement rather than a liability:

1. **Grounding stays union-of-executed-tools, and stays narrow.** More agents must not mean
   a looser grounding set, or the hallucination check degrades into theatre exactly as
   described in §7.
2. **Only the synthesizer writes prose.** Analyst agents return tool calls and nothing else.
   Every intermediate natural-language hop is a place for a number to mutate.
3. **The whole topology degrades to today's path.** If the planner fails, fall through to a
   single-agent turn; if that fails, fall through to heuristic charts. The ladder in §8
   gets one more rung, not a new failure mode.

### 13.5 Recommendation

| Priority | Change | Why |
|---|---|---|
| **1** | Bounded agentic loop (multi-round tool calling, cap 5) in `_reason_and_dispatch` | Closes the one real capability gap (multi-hop questions) with ~40 lines and no new failure classes |
| **2** | Model routing — cheapest model for tool selection, strongest for narration | Cost/latency win, no architectural change; the fallback chain already exists |
| **3** | Planner/analyst/synthesizer split | Only once (a) questions routinely need >3 dependent steps, or (b) a second data source lands |
| **Never** | Critic agent over numbers | Strictly worse than the deterministic check already shipped |

---

## 14. Running this for real: the multi-tenant path

Everything above describes the assessment deliverable — one spreadsheet, one process.
Serving thousands of clients' data needed a different data and serving layer. The
*reasoning* layer below the API is unchanged: same guardrails, same grounding check, same
degradation ladder, same figures.

Two backends now exist behind one interface, selected by `STORAGE_BACKEND`:

| | `dataframe` (default) | `sql` |
|---|---|---|
| Data | one file, loaded at import | Postgres, queried per turn |
| Tenancy | none | `tenant_id` on every key, index and query |
| Identity | caller-supplied | verified JWT |
| Process memory | grows with the corpus | grows with *one user's window* |

### 14.1 Identity is no longer a request field

The single hardest defect in the original: `POST /query` trusted the `user_id` in the
request body, so anyone could read anyone's financial history by changing one string.
No downstream guardrail could help — the cross-user check reads the *prompt text* and has
nothing to say about a forged identity field.

`user_id` now comes from a signed token's `sub` claim ([`src/auth/principal.py`](src/auth/principal.py)).
Scopes are deliberately few, because each one is a chance to grant more than intended:

| Scope | Grants |
|---|---|
| `query` | read your own data |
| `read:any` | read any user *in your tenant* — support tooling, logged as impersonation |
| `ingest:write` | upload files for your tenant |
| `admin` | tenant administration; implies the rest |

Reading somebody else requires `read:any` and returns an `impersonated` flag, because an
unlogged impersonation is indistinguishable from a breach after the fact. `AUTH_REQUIRED=true`
with a missing or under-length key **fails at construction**, not per request — a
configuration mistake should not become a live incident.

### 14.2 Memory stops tracking the corpus

Measured on a 300,000-row / 2,000-user synthetic corpus, same query, isolated processes:

| Backend | Peak RSS | Rows resident to answer a 41-row question |
|---|---|---|
| `dataframe` | 331.8 MB (+252 MB) | 300,000 |
| `sql` | 95.8 MB (+16 MB) | 41 |

That is ~840 bytes of resident memory per row on the DataFrame path — about **84 GB per
worker at 100M rows**, versus flat for SQL. The filter moves into the query; only the
window is materialised.

The derived columns (`parent_category`, `is_income`, `expense_amount`, …) are *stored*,
computed once at ingest by the same `CategoryTaxonomy` the DataFrame path uses. Deriving
them per read would re-split strings across every row on every query and put the category
filter out of reach of an index.

### 14.3 Tenant isolation, in three places

`tenant_id` leads every primary key and every index, so a query that forgets it cannot
efficiently reach another tenant's partition. But storage is only one of the three places
isolation has to hold:

1. **Storage** — `tenant_id` is bound at store construction and appended to every
   `WHERE`; it is never a caller-supplied argument.
2. **Cache** — the original `user:{id}:profile` keys are *not* unique across tenants. Two
   clients each with a `usr_001` would share an entry, serving one client's profile to
   another. The read never reaches storage, so the SQL filter cannot save it; the key
   itself carries the tenant now.
3. **Charts** — rendered PNGs were served to anyone who knew the filename, which embedded
   the user id and a timestamp. Filenames now carry a random nonce, *and* each render
   records a grant naming its owner, which `/charts/{name}` checks.

Tested by loading two tenants with a deliberately colliding `user_id` — the case a bare
`WHERE user_id = ?` gets wrong ([`tests/test_sql_backend.py`](tests/test_sql_backend.py)).

### 14.4 Ingestion

`POST /ingest` takes CSV/XLSX/Parquet ([`src/ingest/loader.py`](src/ingest/loader.py)). Four
properties, each earned from a way this normally goes wrong:

- **Idempotent.** Every row carries a hash of its natural key under a unique constraint,
  so a resent file inserts nothing. Without it, "why did my spending double?" becomes the
  most common support ticket.
- **Atomic.** One transaction for the batch. A partial load produces figures that are
  wrong without looking wrong.
- **Tolerant.** Malformed rows are dropped and counted, not fatal. A 200,000-row upload
  with nine bad dates loads 199,991 and reports the nine.
- **Reversible.** Every row carries its `batch_id`, so a bad import can be undone.

### 14.5 The cross-user roster had to stop being a list

The guardrail asked "does this prompt name someone else?" by holding every name in a set.
That fails twice at scale: it loads the roster into every worker, and matching a prompt
against 50,000 first names flags "my phone bill" because a customer is called Bill.

It is now an interface ([`src/data/roster.py`](src/data/roster.py)). The in-memory
implementation is unchanged for small datasets. The SQL one inverts the question: the
prompt proposes a few *conservative* candidate tokens — capitalised mid-sentence, or
possessive, and never finance vocabulary — and one indexed query decides whether any
belongs to a different user.

One deliberate non-check: the roster does **not** verify that a `usr_`-shaped token is a
real user before refusing. Refusing only on a hit would turn the guardrail into an oracle
for enumerating valid ids.

### 14.6 The `as_of` anchor is now per tenant

`data_max` is right for a frozen historical upload and wrong for live data; `now` is the
reverse. It was a global constant. It is now per tenant (`tenants.as_of_mode`), because
one client uploading a stale extract must not shift what "last month" means for everyone.

---

## 15. Known gaps

**Reasoning layer**
- Single-round tool calling limits multi-hop questions (§13.3a).
- Chart selection in degraded mode is keyword-heuristic, so an unusual phrasing gets a
  reasonable-but-not-ideal chart.
- Injection detection is pattern-based; it catches the documented classes, not novel
  phrasings.
- **`can_read_all` is unreachable for the prompts it exists to permit.** In
  [`input_guardrails.check`](src/guardrails/input_guardrails.py), `detect_cross_user` runs
  before the population branch, and "who spends the most of **all users**?" matches a
  cross-user pattern first — so a `read:any` holder is refused identically to an ordinary
  caller. Verified: `can_read_all=True` and `False` both return
  `blocked=True, flags=['cross_user_access_attempt']`. The fix is an ordering decision
  about intended semantics, so it is flagged rather than assumed.

**Serving layer (the remaining Stage 2/3 work from the readiness review)**
- Endpoints are still synchronous `def`, so concurrency is capped around FastAPI's
  threadpool (~40) with each request holding a thread for the full LLM round-trip.
- `plt.subplots()` uses pyplot's global figure manager, which is not thread-safe under
  that threadpool. Needs the object-oriented `Figure` + `FigureCanvasAgg` API.
- Charts are still local files with no lifecycle policy, so they neither expire nor
  survive multi-pod deployment. Needs object storage behind the existing grant check.
- Redis is wired behind the `KVCache` ABC but remains the untested path; with N workers
  the in-memory cache is N divergent caches.
- Free-tier models will rate-limit at this scale; no per-tenant quotas or cost controls.
- The audit log writes to a local file — financial data likely carries retention
  requirements that need a managed sink.
