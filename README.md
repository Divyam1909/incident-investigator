# Incident Investigator

A LangGraph agent that investigates production incidents the way an SRE would.
You give it an alert, for example *"p95 latency on POST /checkout jumped from
180ms to 2.4s at 14:05"*. It pulls evidence from traces, logs and deploys, tests
competing hypotheses, and writes a root-cause report. Every claim in the report
cites the tool output it came from, and code (not another LLM) checks that it does.

It follows on from my LLM Observatory project: Observatory
collects OpenTelemetry traces, and this agent reasons over them. The `spans`
table uses the same conventions (W3C trace and span ids, `kind`, `status`
UNSET/OK/ERROR, OTel attribute names such as `http.route`, `peer.service`,
`db.system` and `gen_ai.system`).

## Contents

- [The problem](#the-problem)
- [Architecture](#architecture)
- [Run it in under 5 minutes](#run-it-in-under-5-minutes)
- [Example investigation](#example-investigation)
- [Evaluation](#evaluation)
- [Design decisions](#design-decisions)
- [Limitations and next steps](#limitations-and-next-steps)
- [Commands](#commands)

## The problem

When an alert fires, the on-call engineer has to answer one question fast:
*what changed, and is it the cause?* Most of the first 15 minutes goes on the
same routine: check latency against the baseline, look at errors, check
recent deploys, check the dependencies, grep the logs. The hard part is not
collecting the data. It is avoiding the obvious wrong answer, such as blaming
the deploy that happened to go out four minutes before a cache died.

An LLM can do that routine, but on its own it is a liability. It will state
confident numbers it never looked up. This project is built around that risk:

1. The agent can only see the world through five narrow, read-only tools.
2. Every claim must cite an evidence id returned by a tool.
3. A deterministic verify node checks every number, version and time in each
   claim against the evidence it cites. Unsupported claims are sent back for
   one fix, then removed, and confidence is lowered.

## Architecture

The graph below is generated from the compiled LangGraph (`python -m investigator graph`):

```mermaid
graph TD;
	__start__([<p>__start__</p>]):::first
	triage(triage)
	hypothesize(hypothesize)
	investigate(investigate)
	tools(tools)
	verify(verify)
	report(report)
	__end__([<p>__end__</p>]):::last
	__start__ --> triage;
	hypothesize --> investigate;
	investigate -.-> tools;
	investigate -.-> verify;
	tools --> investigate;
	triage --> hypothesize;
	verify -.-> investigate;
	verify -.-> report;
	report --> __end__;
	investigate -.-> investigate;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```

| Node | Uses the LLM? | What it does |
|---|---|---|
| `triage` | yes (structured output) | Extracts service, endpoint, symptom and severity from the alert. A service the model invents is replaced by the alert's own. |
| `hypothesize` | yes (structured output) | 2-4 competing, testable hypotheses, at least one that is not a deploy. |
| `investigate` | yes (tool calling) | The ReAct loop with `tools`. Ends when the model calls `submit_findings`, or when the max-steps guard forces a structured conclusion. |
| `tools` | no | Runs tool calls, gives each result an evidence id (E1, E2...), and never runs more calls than the budget left. |
| `verify` | **no** | Checks claims against cited evidence ([verify.py](investigator/agent/verify.py)), calibrates confidence, and can send the agent back once. |
| `report` | **no** | Builds the report from verified claims only. |

```
   alert ─▶ FastAPI / CLI ─▶ runner ─▶ LangGraph graph ─▶ Toolbox ─▶ SQLAlchemy ─▶ SQLite / Postgres
                               │                                                     ▲
                               └── writes every node event to investigation_steps ───┘
                                   (the API's polling and SSE endpoints read this)
```

### Code map

| Path | What is in it |
|---|---|
| [investigator/agent/graph.py](investigator/agent/graph.py) | Nodes, routing, the max-steps guard, retry policy |
| [investigator/agent/state.py](investigator/agent/state.py) | The typed graph state (`TypedDict` with reducers) |
| [investigator/agent/schemas.py](investigator/agent/schemas.py) | Pydantic models for every LLM output (Triage, Hypotheses, Findings) |
| [investigator/agent/verify.py](investigator/agent/verify.py) | Deterministic claim verification and confidence calibration |
| [investigator/agent/llm.py](investigator/agent/llm.py) | Gemini factory and the offline mock model |
| [investigator/tools.py](investigator/tools.py) | The five tools |
| [investigator/db/models.py](investigator/db/models.py), [docs/schema.md](docs/schema.md) | Schema, and why each index exists |
| [investigator/seed.py](investigator/seed.py), [investigator/scenarios.py](investigator/scenarios.py) | Synthetic traffic and the four labelled incidents |
| [investigator/runner.py](investigator/runner.py) | Runs the graph and records each step |
| [investigator/api.py](investigator/api.py) | FastAPI service |
| [investigator/evaluate.py](investigator/evaluate.py) | Accuracy eval against ground truth |

### Tools

All are plain Python functions over the database. Windows are durations
ending at the moment the alert fired, so replaying an old incident sees
exactly the data that existed when it was paged.

| Tool | Returns |
|---|---|
| `query_error_spans(service, window)` | Failed requests, error rate **by `service.version`**, top errors with first-seen time and dependency, failed status codes |
| `latency_percentiles(service, endpoint, window, baseline_window)` | p50/p95/p99 now vs baseline, the p95 ratio, the **onset minute**, and stats since onset |
| `recent_deploys(service, window)` | Deploys with version, previous version, author, change summary, minutes before the alert (`service='all'` for every service) |
| `search_logs(service, pattern, window)` | Match count by level, first and last seen, top messages |
| `dependency_health(service)` | Per downstream (database, cache, HTTP, LLM): error rate and p95 vs baseline, top error, **connection-pool wait**, and **other services calling the same dependency** (blast radius) |

Tools return aggregates, never raw rows, so prompt size stays flat however much
traffic there is. Bad input (unknown service, bad endpoint, bad window)
returns an error that lists the valid values, so the model can correct itself.

## Run it in under 5 minutes

Needs Python 3.11+. No API key is needed for the default mock mode.

```bash
cd incident-investigator
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"

python -m investigator run --scenario db_pool     # migrates and seeds on first run (~10s)
```

That runs the full graph offline with the deterministic mock model. To use Gemini:

```bash
copy .env.example .env             # macOS/Linux: cp .env.example .env
# set GOOGLE_API_KEY=... in .env (get one at https://aistudio.google.com/apikey)
python -m investigator run --scenario cache_red_herring --provider gemini
```

The API:

```bash
python -m investigator serve
curl -X POST localhost:8000/incidents -H "content-type: application/json" -d "{\"scenario\": \"db_pool\"}"
curl -N localhost:8000/investigations/1/events        # live steps over SSE
curl localhost:8000/investigations/1                  # status, steps, tokens, cost, report
```

`POST /incidents` also takes a free-form alert:
`{"alert_text": "...", "service": "checkout-api", "endpoint": "POST /checkout", "detected_at": "2026-11-02T14:12:00Z", "provider": "gemini"}`.
Interactive docs are at `http://localhost:8000/docs`.

Postgres works by setting `DATABASE_URL=postgresql+psycopg://user:pass@host/db`
after `pip install -e ".[postgres]"`. Migrations are Alembic (`alembic upgrade head`,
or automatically on first run). The test suite runs on SQLite. The Postgres path
was checked by hand against `postgres:16` in Docker: migrations match the models,
`attributes` is `jsonb`, an investigation completes, and `EXPLAIN` shows the error
query using the partial index. Seeding Postgres is slow (several minutes over
Docker on Windows), because it uses ORM bulk inserts rather than `COPY`.

## Example investigation

A real Gemini (`gemini-3.1-flash-lite`) run of the red-herring scenario. A
deploy of catalog-service went out at 14:01, four minutes before latency spiked.
The real cause is the shared redis cache refusing connections. Output is
trimmed for width.

```
$ python -m investigator run --scenario cache_red_herring --provider gemini
Investigation #17 of incident #4 with gemini/gemini-3.1-flash-lite (max 12 tool calls)

[01] triage                 service=catalog-service endpoint=GET /products symptom=latency
[02] hypothesize            H1 [db_pool_exhaustion] ... | H2 [bad_deploy] A recent deployment
                            introduced an N+1 query ... | H3 [cache_outage] The Redis cache ...
[04] E1 recent_deploys      catalog-service 3.2.0 at 14:01 (2ms)
[06] E2 latency_percentiles p95 1205.2ms vs baseline 69.6ms (x17.32); onset 2026-11-05T14:05:00Z
[08] E3 dependency_health   postgres err 0.0% (base 0.0%) p95 1286.7ms (base 54.4ms);
                            redis err 46.38% (base 0.0%) p95 2.6ms (base 2.0ms) (85ms)
[10] E4 search_logs         70 matches for 'redis' {'ERROR': 1, 'WARN': 69} (3ms)
[11] investigate            findings submitted: cache_outage in redis (model confidence high)
[12] verify                 passed; confidence high
[13] report                 cache_outage / redis (confidence high)
```

The report it produced (excerpt):

> **Root cause (cache_outage, confidence: high):** The Redis cache cluster is
> down, causing the catalog-service to fall back to the primary database for
> all requests, which is significantly slower.
>
> - The Redis cache is experiencing a 46.38% error rate with 'ConnectionError:
>   Error 111 connecting to redis-cache:6379. Connection refused.' starting at
>   14:05:02Z. [E3]
> - The database (postgres) p95 latency increased from 54.4ms to 1286.7ms, but
>   pool wait time remained low (1.3ms vs 1.1ms baseline). [E3]
> - **H2 ruled out** (bad deploy): The deploy occurred at 14:01, but the cache
>   outage started at 14:05. [E1, E3]
> - **H1 ruled out** (pool exhaustion): Database pool wait time remained low. [E3]

Two things worth noticing. The agent did not stop at "postgres is slow": it
used the pool-wait numbers to rule out pool exhaustion. It also ruled out the
deploy on timing alone; it never used the blast-radius field showing that
checkout-api, which had no deploy, saw the same redis errors. That was the
stronger argument, and it was available.

**Verify catching a bad claim.** In an earlier `db_pool` run the model's first
`submit_findings` had a claim that failed verification. Verify replied with the
exact problem, the model resubmitted, and the second version passed:

```
[15] investigate   findings submitted: db_pool_exhaustion in checkout-api (model confidence high)
[16] verify        1 unsupported claim(s), root cause supported; confidence medium; sent back to investigate
[17] investigate   findings submitted: db_pool_exhaustion in checkout-api (model confidence high)
[18] verify        passed; confidence high
```

## Evaluation

`python -m investigator eval --provider gemini --pause 45` runs all four
scenarios as fresh investigations and scores them. A run counts as correct
only if the **category matches exactly and the component matches**: naming
`dependency_timeout` but blaming stripe instead of gemini is wrong.

| Scenario | What happened (ground truth) | Trap |
|---|---|---|
| `bad_deploy` | checkout-api v2.14.0 throws `TypeError` on carts without a promo code; ~30% of checkouts fail | Latency is unchanged, so a latency-only check sees nothing |
| `db_pool` | An order-export job holds Postgres connections; requests queue for the pool | Postgres *looks* slow, but query time is normal and the wait is in the pool. A payments-service deploy 22 minutes earlier is noise. |
| `llm_timeout` | The LLM provider (Gemini) degrades; 40% of chat calls hit the 30s deadline | An unrelated catalog-service deploy in the window |
| `cache_red_herring` | redis-cache refuses connections from 14:05; reads fall through to Postgres | A catalog-service deploy at 14:01, and Postgres latency up 24x as a side effect |

### Results: Gemini (`gemini-3.1-flash-lite`, temperature 0)

**Accuracy: 4/4.**

| Scenario | Agent's root cause | Correct | Confidence | Tool calls | Tokens in / out | Latency | Cost (USD) | Claims removed |
|---|---|---|---|---|---|---|---|---|
| bad_deploy | bad_deploy / checkout-api@2.14.0 | yes | high | 3 | 9,301 / 984 | 8.8s | 0.0053 | 0 |
| db_pool | db_pool_exhaustion / postgres | yes | high | 5 | 15,456 / 1,048 | 18.5s | 0.0073 | 0 |
| llm_timeout | dependency_timeout / gemini | yes | high | 4 | 11,738 / 980 | 34.9s | 0.0060 | 0 |
| cache_red_herring | cache_outage / redis | yes | high | 4 | 12,676 / 1,178 | 36.9s | 0.0067 | 0 |

Cost is an estimate at $0.30 / $2.50 per million input / output tokens
(configurable in `.env`); on the free tier the real cost is zero. Latency
includes free-tier rate-limit backoff, which is why it varies between runs
(8.8s to 55s for the same scenario).

### Results: mock (offline, rule-based)

**Accuracy: 4/4**, 5 tool calls each, about 0.2s per run. The mock is a
hand-written SRE runbook, not a language model, so this score shows the tools
and data carry the signal. It says nothing about LLM reasoning.

### Read these results honestly

- **4 scenarios is a smoke test, not a benchmark.** The same person wrote the
  generator, the scenarios and the prompts, so there is a real risk of fitting
  the prompts to the data.
- **The first Gemini runs were not 4/4.** In the very first full run, two
  scenarios failed on free-tier rate limits (429), which is why the LLM nodes
  now have a LangGraph `RetryPolicy` and the eval has `--pause`. And in an
  earlier `db_pool` run, Gemini got the category right but named the
  component `checkout-api` (the pool's owner) instead of `postgres`, which the
  scorer marks wrong. I then made the component instruction explicit in the
  prompt and schema ("name the dependency, not the caller"). That is a fair
  prompt fix, but it was made after seeing the result.
- **Three repeat runs were identical** at temperature 0 (same tool sequence,
  same token counts), so repeats add no statistical weight.
- The `gemini-flash-latest` alias returned 503 and then 404 for this API key, and
  `gemini-3.5-flash` / `gemini-3.7-flash` / `gemini-3.8-flash` returned 503
  or 504 (overloaded) on the day of testing. The results above use
  `gemini-3.1-flash-lite`. Set `GEMINI_MODEL` to compare a stronger model.

## Design decisions

**Why LangGraph rather than a plain agent loop.** The investigation has a fixed
shape (triage, hypotheses, evidence, verification, report) with one loop and
one conditional back-edge. LangGraph makes that shape explicit and testable:
each node is a plain function over typed state, routing is two small
functions, the Mermaid diagram above comes straight from the compiled graph,
and streaming node updates gives step-by-step progress for free. A single
"agent with tools" prompt would hide all of that inside one LLM call. The
graph is also where the guarantees live: the step budget, the retry policy and
verification are code paths, not instructions the model may ignore.

**Why a verify node, and why it is not an LLM.** An LLM judge checking an LLM
is a second opinion with the same failure mode. The verify node checks facts
mechanically:
every cited evidence id must exist, and every number, version and HH:MM time
in a claim must appear in the evidence that claim cites. It tolerates units
and rounding ("2.4s" matches 2412.3ms, "28%" matches 28.43, "x14" matches
2412.3/171.2), and the tolerance scales with the claim's precision, so a small
invented number cannot pass by sitting near a real one (a test caught exactly
that bug). The root-cause component must be named in its cited evidence.
Failing claims go back to the agent once with the exact problems; claims that
still fail are dropped from the report and listed under "Verification". The
model's own confidence can only be lowered: by unsupported claims, by too few
verified claims, by running out of budget, or to `low` if the root cause
itself is not backed.

What it does not catch: a claim whose numbers are all real but whose
*reasoning* is wrong ("the deploy caused it", citing true deploy times).
Scoring that needs ground truth, which is what the eval is for.

**The LLM decides; code writes the report.** The `report` node assembles the
report from verified claims. Nothing the verifier rejected can appear in it.

**Structured output everywhere.** Triage, hypotheses and findings are Pydantic
models. `submit_findings` is a tool whose arguments are the `Findings` schema,
so ending the loop and producing structured output are the same act.
Malformed findings are rejected with the validation errors and cost a step,
so a model that keeps failing still hits the guard instead of looping forever.

**The max-steps guard is enforced twice.** The `tools` node never runs more
calls than the budget left (one model turn can request several in parallel;
the rest get "Skipped: step budget exhausted"). Then `investigate` forces a
structured conclusion without tools and confidence is capped at medium. Both
paths are tested with a scripted model that never stops calling tools.

**A mock that is a real `BaseChatModel`.** The offline mode implements
`bind_tools` and `with_structured_output` and returns real tool calls, so tests
and demos run the exact same graph code as Gemini. Behind it is a rule-based
policy that reads the actual tool results. It does not know the answers.

**Tools see the world as of the alert.** Every window ends at the incident's
`detected_at`. Investigations are reproducible, the eval can replay old
incidents, and a test checks that tools cannot see data from after "now".

**Progress lives in the database.** The runner writes each node event to
`investigation_steps` as it happens; the SSE and polling endpoints read it back.
Any API process, or the CLI, can watch any run, and the audit trail (tool,
arguments, result summary, latency, tokens, cost) survives restarts.

**Schema and indexes.** Every tool query is "one service, one time window", so
the indexes lead with `service_id` and end with the timestamp, giving one range
scan per query. A partial index covers error spans only (under 1% of rows).
Ground truth sits in its own `incident_labels` table that no tool reads. Full
reasoning per index is in [docs/schema.md](docs/schema.md).

**Provider-agnostic.** The graph depends only on LangChain's `BaseChatModel`.
Adding OpenAI or Anthropic is one branch in `build_llm`.

## Limitations and next steps

- **Synthetic data, four scenarios.** Next: replay real OTLP traces exported
  from LLM Observatory, and add harder cases: two simultaneous faults, a slow
  burn with no clear onset, and an incident whose cause is outside the data
  (the right answer is `unknown`).
- **The verifier checks facts, not causality.** A next step is requiring each
  ruled-out hypothesis to cite a contradicting measurement, and flagging
  verdicts that rest on timing alone.
- **The agent under-used blast radius** in the red-herring run (see above). A
  prompt or hypothesis-template change is cheap; measuring it needs more
  scenarios first.
- **Percentiles are computed in Python** (SQLite has no `percentile_cont`).
  Fine at this volume; on Postgres they should be pushed into SQL, and `spans`
  and `logs` partitioned by day.
- **Seeding Postgres is slow.** Switching the seed to `COPY` would make it seconds.
- **`search_logs` is substring search.** On Postgres, add a `pg_trgm` index.
- **Background tasks run in the API process.** For production, move runs to a
  worker queue (the step table already decouples progress from the process).
- **No auth** on the API. It is a local demo.

## Commands

| Task | Command (`make` target in brackets) |
|---|---|
| Install | `pip install -e ".[dev]"` (`make install`) |
| Reset and seed the database | `python -m investigator seed --reset` (`make seed`) |
| Investigate a scenario | `python -m investigator run --scenario db_pool [--provider gemini] [--max-steps 6]` (`make demo`) |
| Evaluate | `python -m investigator eval --provider mock` / `--provider gemini --pause 45` (`make eval`, `make eval-gemini`) |
| API | `python -m investigator serve` (`make serve`) |
| Regenerate the graph diagram | `python -m investigator graph` (`make graph`) |
| Tests | `pytest` (`make test`) |
| Lint | `ruff check . && ruff format --check .` (`make lint`) |

Scenarios: `bad_deploy`, `db_pool`, `llm_timeout`, `cache_red_herring`.
Eval output is written to [eval/](eval/).
