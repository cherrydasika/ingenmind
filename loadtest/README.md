# Load tests and capacity (R61)

How many people one instance serves, and what breaks first.

## What is here

- `locustfile.py`: three kinds of simulated user against the public API
  (`/api/v1`) with API keys. **Askers** ask every 5–15 s, sometimes a
  follow-up in the same session. **Ingesters** add a short document every
  30–90 s. **Evaluators** start an evaluation of the live flow every 5–10
  minutes. The mix is 20 : 1 : 1.
- `make_keys.py`: load-test accounts and keys, on a throwaway install only.
- `app/llm_stub.py`: `LLM_PROVIDER=stub`, a stand-in model that waits
  `LLM_STUB_SECONDS` per call and answers the happy path. It measures the
  app (searches, PostgreSQL, sessions, guardrail plumbing, concurrency)
  without paying for a model or hitting its rate limits. With a real
  model, the same scripts measure the whole thing.

## Running it

Never against a real install: it creates accounts, sessions and documents.

```bash
# 1. a throwaway copy of the database, and an app on it with the stub model
docker exec <pgvector> psql -U rag -c "CREATE DATABASE rag_loadtest"
docker exec <pgvector> sh -c "pg_dump -U rag rag | psql -q -U rag -d rag_loadtest"
docker run -d --name webapp-loadtest --network <compose network> -p 18002:8000 \
  --env-file <the webapp's env with PGDATABASE=rag_loadtest, LLM_PROVIDER=stub,
               LLM_STUB_SECONDS=1.0, LIMIT_TOKENS_PER_DAY=0, Langfuse keys blank> \
  -v "$PWD/app:/app" -v "$PWD/dags:/app/dags:ro" -v "$PWD/web:/web:ro" \
  rag_systems-web uvicorn web_api:app --app-dir /app --host 0.0.0.0 --port 8000
# 2. keys (one per simulated person, so per-person limits don't cap the test)
docker cp loadtest/make_keys.py webapp-loadtest:/tmp/ && \
  docker exec webapp-loadtest python /tmp/make_keys.py 400 > keys.json
# 3. Locust, on the same network
pip install -r loadtest/requirements.txt
LOADTEST_KEYS=keys.json locust -f loadtest/locustfile.py --headless \
  -H http://webapp-loadtest:8000 -u 100 -r 10 -t 3m --only-summary
```

Langfuse is off in the load-test app, so its traces don't fill the real
project. If PostgreSQL is shared with a real install, keep
`workers × WEB_THREADS` under its `max_connections` (see below), or the real
install's queries fail too.

## Results (local, stub model at 1 s a call)

The machine was a Rancher Desktop VM with 6 vCPUs and 16 GB, on Apple
silicon (arm64, like Graviton). The database had 100 Trainline pages (550
chunks), local embeddings, and the stub model. A question makes about 6
model calls, so one takes about 6.2 s when nothing else is waiting.

| Setup | Users | Questions/s | p50 | p95 | Errors | What limited it |
|---|---|---|---|---|---|---|
| 1 worker, 40 threads (default) | 50 | 2.7 | 6.2 s | 6.3 s | 0 | nothing |
| 1 worker, 40 threads | 100 | 5.4 | 6.3 s | 7.0 s | 0 | nothing yet |
| 1 worker, 40 threads | 200 | ~6.0 | 19 s | 21 s | 0 | the thread pool: 40 ÷ 6.2 s ≈ 6.4 questions/s |
| 1 worker, 120 threads | 200 | ~9 | 6.7 s | 12 s | <0.1% | nothing yet |
| 1 worker, 120 threads | 400 | ~10.6 | 21 s | 26 s | 0.1% | one Python process (~1.9 cores: the GIL, local embeddings); PostgreSQL ~1 core |
| 3 workers, 120 threads | 400–600 | — | — | — | 44–76% | PostgreSQL `max_connections` (100): "too many clients" |
| 2 workers, 45 threads | 300 | ~12 | 12 s | 17 s | 0 | the threads again: 90 ÷ 6.2 s ≈ 14.5 questions/s |
| 2 workers, 45 threads | 400 | ~12 | 19 s | 28 s | 0 | as above: queueing, no errors |

**Best local setup:** 2 workers × 45 threads (90 connections at most). It
gives about 12 questions a second with no errors. Latency stays near the
unloaded 6 s up to about 8 questions a second. That is roughly **130
people asking at once** at one question every 10–15 s each. Beyond that,
answers queue rather than fail.

## What to tune

1. **`WEB_THREADS`** (new, default 40). Each question holds a thread for its
   whole run, so threads ÷ seconds per question caps throughput. Raise it
   with care: each thread can hold a PostgreSQL connection.
2. **PostgreSQL connections.** The app opens a connection per database call
   and doesn't pool them. Keep `workers × WEB_THREADS` plus the worker and
   AgentCore KB well under `max_connections` (100 by default), or raise it
   (`postgres -c max_connections=300`). A shared connection pool (psycopg_pool
   or PgBouncer) is the proper fix and a follow-up.
3. **Workers** (`uvicorn --workers N`). One Python process tops out around
   2 cores. More workers use more cores, within the connection budget above.
   Background loops (alerts, nightly evaluations, retention) are safe with
   several workers: each takes a PostgreSQL advisory lock.
4. **Model latency dominates.** A real model takes 2–10 s a call, not 1 s, so
   a question holds its thread 2–10× longer. Concurrent questions =
   questions/s × seconds per question, which is what `WEB_THREADS` has to
   cover. The provider's own rate limits (tokens a minute) usually bind
   before the instance does.

## Capacity of a t4g.xlarge

**Measured on 2026-10-23** on the EC2 instance (4 Graviton2 vCPUs, 16 GB),
release 440185a, through SSM. The app under test was a second container
from the deployed image, on a copy of the production database
(`rag_loadtest`: the 19 Trainline pages, Bedrock embeddings), with the stub
model at 1 s a call and Langfuse and the AgentCore harness off. Locust ran
on the same instance, so it took some CPU from the app. Only Askers and
Ingesters ran: the EC2 install has no question set for the Evaluators. One
question takes **7.2 s unloaded**: the stub's calls plus Bedrock embedding
calls.

| Setup | Users | Questions/s | p50 | p95 | Errors | What limited it |
|---|---|---|---|---|---|---|
| 1 worker, 40 threads | 100 | 4.3 | 11 s | 14 s | 0 | the threads: 40 ÷ ~9 s |
| 2 workers, 45 threads | 200 | 5.3 | 22 s | 30 s | 0 | **CPU**: the app ~1.8 cores, PostgreSQL ~1.6 |
| 2 workers, 45 threads | 300 | 5.3 | 36 s | 46 s | 0 | as above: answers queue, none fail |

The one or two failures Locust counted in a run were requests it cut off
when the run stopped (`ClientDisconnect` in the app's log), not errors. At
the peak only about 30 database connections were open.

**One t4g.xlarge serves about 5 questions a second.** Latency stays near the
unloaded 7 s up to about 3–4 questions a second: **about 40–50 people asking
at once**, at one question every 10–15 s each. Beyond that, answers queue
rather than fail. That is less than the local machine's 12 a second because
the limit here is CPU, not threads. Much of PostgreSQL's share is a new
connection for every database call, so a connection pool (psycopg_pool or
PgBouncer) is the first thing to try for more. After that, a larger
instance (t4g.2xlarge, 8 vCPUs).

With a real model at about 3 s a call, a question holds its thread about
three times longer but costs the instance about the same CPU. The CPU limit
stays near 5 a second, and the 90 threads cap it at 90 ÷ ~20 s ≈ 4.5 a
second: **about 40 people at once**. The provider's rate limits come first on
most accounts.

## Service levels

Shown on Monitoring → Operations (`app/slo.py`), over the last 30 days of
real users' questions:

| Objective | Default target | Setting |
|---|---|---|
| Availability: answered without an error or a timeout | 99% | `SLO_AVAILABILITY` |
| Latency: answered within 30 s | 95% | `SLO_LATENCY_SECONDS`, `SLO_LATENCY_TARGET` |
| Answer pass rate: checked answers that passed | 80% | `SLO_PASS_RATE` |

The error budget is how many questions may miss in the window. The burn
rate compares the last day's misses with what the budget allows per day:
above 1× the budget runs out before the window ends.
