# Plan: knowledge system setup state, sources in the database, and reset

Status: **Phases 2–5 done; pull request open, waiting for the user to
merge; then deploy (EC2 starts READY/existing, nothing reset there).** The local database was reset in phase 4 and stays empty
and `NEW` (seeding the demo needs Bedrock credentials locally: backlog
"Local model calls fail"), which is the starting point #10 needs. The user took every recommendation (2026-10-08): reset
EC2 only once setup works (#10–#14); keep people's sessions and flow
history on reset. Branch `kb/setup-state-and-reset`. Update this file
at the end of every step: tick what is done, note what was found, say what
comes next.

GitHub: issue #9, part of epic #19 (RAG Initialization Agent). Working
agreement: phases with a stop at each; a branch and a pull request; the
user merges (AGENTS.md).

## Goal

One installation has **one knowledge system**. A fresh install is set up
once by the Initialization Agent (#10 onwards); an admin can **reset** the
knowledge system to set it up again (another domain, or our own first real
run). This issue adds what the later steps hang off: a stored setup state,
the source list in the database instead of a file, and the reset.

## How the code is today

- **Knowledge**: `rag_chunks` (curated) and `research_chunks` (research)
  in `dags/common/storage/pg_store.py`, plus `ingestion_jobs` and
  `embedding_config`. Research also keeps `research_source_profiles` and
  `research_source_reports` (`app/source_profiles.py`).
- **The URL list** is a file: `URLS_CONFIG_PATH` (`/data/urls.json`, else
  `urls.example.json`; `dags/common/config.py`), mounted read-only into the
  web app; `trigger()` in `app/web_api.py` queues the first 10
  (`MAX_URLS_PER_JOB`). `deploy.sh` copies `data/urls.json` from the active
  release to the new one.
- **Other app tables** (same database, `config.PGDATABASE`): `app_users`,
  `app_user_permissions`, `app_sessions`, `app_session_turns`,
  `app_agent_memory`, `agent_sessions` (the local harness's conversations),
  `agent_flows`, `agent_flow_versions`, `agent_live_flow`,
  `agent_flow_runs`, `agent_eval_sets`, `agent_eval_runs`,
  `agent_eval_results`. Built-in eval sets are files
  (`app/flows/eval_sets/`). Each module creates its own tables
  (`ensure_schema()`, `CREATE TABLE IF NOT EXISTS`).
- **A fresh install**: `COMPOSE_PROFILES=demo` runs `seed_demo` (fictional
  documents through `adhoc_ingest`). Sign-in: the first user becomes an
  admin (dev sign-in "Create the first admin"; OIDC `ADMIN_EMAILS`).
  Nothing today knows whether the knowledge base is "set up".

## Design

### 1. The knowledge system record (`app/knowledge_system.py`, new)

```
app_knowledge_system   one row: state, origin, blueprint_version (#11),
                       set_up_at, set_up_by, urls_imported_at, updated_at
app_knowledge_system_events   audit: event, user_id, at, details jsonb
```

- `state`: the setup states (#10 adds the full machine): `NEW` and `READY`
  now; the rest arrive with #10. `origin`: how it became ready — `existing`
  (set up before setup existed), `demo`, or `setup`.
- **First run**: the row is created on first use. If the knowledge base
  already has chunks or URLs (every install today, including EC2), it
  starts `READY` with origin `existing`, so nothing changes for them.
  Otherwise `NEW`. `seed_demo` marks it `READY` / `demo`.

### 2. Sources in the database

```
kb_urls   url (primary key), ttl_days, origin (import | plan | manual),
          added_at, added_by
```

- On first use, an existing `urls.json` is **imported once** (recorded in
  `urls_imported_at`); from then on `trigger()` reads `kb_urls`, not the
  file. The 10-URL cap per job stays until the ingestion plan (#14).
- `urls.example.json` is not imported (it is a sample, not a choice).
- `deploy.sh` keeps copying `urls.json` for now (harmless once imported);
  removed in #14 when the plan owns the list.

### 3. Reset

`POST /api/knowledge-system/reset` with `{"confirm": "RESET"}`, permission
`manage_settings`; refused while an ingestion job is queued or running (as
`stack.sh stop` and `deploy.sh` do). In **one transaction**:

| Emptied | Kept |
|---|---|
| `rag_chunks`, `research_chunks` | `app_users`, `app_user_permissions` |
| `ingestion_jobs`, `kb_urls` | `app_sessions`, `app_session_turns` (people's history) |
| `research_source_profiles`, `research_source_reports` | `agent_flows`, `agent_flow_versions`, `agent_live_flow`, `agent_flow_runs` (flow history) |
| `agent_eval_sets` (saved sets), `agent_eval_runs`, `agent_eval_results` | `embedding_config` (the embedding model in use) |
| `app_agent_memory`, `agent_sessions` (agents' learnings and conversations) | built-in eval sets (files), Langfuse traces |

Then state `NEW`, origin cleared, and an audit event with the counts.
AgentCore Memory (cloud-side, per browser session) is not cleared: its
facts expire on their own; noted as a limitation.

### 4. While not set up

- Questions (`/api/agent/*`, `/api/ask*`) answer "This knowledge system is
  being set up" instead of searching an empty knowledge base.
- Admins (`manage_settings`) land on **Set up your knowledge system**: in
  this issue a placeholder saying setup arrives with #10, with the state and
  the reset; other users see a "being set up" notice.

### 5. Admin page: **Knowledge system** (`manage_settings`)

State, origin, when and by whom it was set up, counts (chunks, research
pages, URLs, reports, eval runs), recent audit events, and **Reset** with a
typed confirmation (type `RESET`) and the table above shown on screen.

### 6. EC2 backup before a reset

A runbook step (README and `scripts/aws/`): an EBS snapshot of the data
volume from the workstation (`aws ec2 create-snapshot`, profile
`personal`), taken and confirmed by the user before any reset on EC2. No
reset runs on EC2 as part of this issue.

## Files to touch

| File | Change |
|---|---|
| `app/knowledge_system.py` (new) | record, events, first run, `kb_urls` and import, reset |
| `app/web_api.py`, `app/auth.py` | `GET /api/knowledge-system`, reset, URL list from the DB in `trigger()`, "being set up" answers |
| `app/seed_demo.py` | mark `READY` / `demo` |
| `app/ingestion_info.py` | job description no longer names `data/urls.json` |
| `web/static/js/` | Knowledge system page (Admin), setup placeholder, notice, nav entry |
| `scripts/aws/` | snapshot helper for the backup step |
| tests | `test_pg_store.py` (reset, import, first run), `test_auth.py`, `test_demo.py` |
| README, PROGRESS.md, plan | phase 5 |

## Phases

- [x] **1. Plan** — this file.
- [x] **2. Record, first run, sources in the database**, with tests.
  - `app/knowledge_system.py`: `app_knowledge_system` (one row,
    `singleton` key), `app_knowledge_system_events`, `kb_urls` (with a
    `position` so the file's order is kept; `data/urls.json` has 2,458
    URLs and a job takes the first 10). The first run is one transaction
    under an advisory lock (web app and worker may race): import, then
    `READY`/`existing` if there are URLs or chunks, else `NEW`. A
    `urls.json` that appears later is still imported once; the example
    list never is. `status()`, `is_ready()`, `mark_ready()`, `urls()`,
    `events()`.
  - `web_api.py`: the first run at startup (lifespan; a database not up
    yet only defers it), `trigger()` reads `kb_urls` (400 "no URLs to
    ingest yet" when empty). `seed_demo` marks a new install
    `READY`/`demo`. The Ingestion job description no longer names the file.
  - Tests: `test_pg_store.py` (install with knowledge starts READY, empty
    install starts NEW and the demo marks it ready, import once and in
    order, example never, trigger reads the database, trigger with no
    URLs), `test_demo.py` (seeding marks a new install ready). Full suite
    286 OK.
- [x] **3. Reset and "being set up"**, with tests.
  - `knowledge_system.reset(confirmation, user_id)`: one transaction under
    the same advisory lock; refused without the typed `RESET` (ValueError)
    and while an ingestion job **or an evaluation run** is queued or running
    (`Busy`; eval runs added: a reset would cut a running one off); deletes
    every store in `EMPTIED` that exists (results before runs before sets),
    sets `NEW` and clears origin, blueprint and set-up fields, keeps
    `urls_imported_at` so `urls.json` is not imported again, and writes a
    `reset` event with the rows deleted per store. `counts()`, `EMPTIED`
    (with labels for the page) and `KEPT`.
  - `status()` sets itself up again when its table is missing (a replaced
    database), found through tests that switch databases.
  - API: `GET /api/knowledge-system` (everyone: state, origin, set-up
    time; `manage_settings` also gets counts, events, what a reset empties
    and keeps), `POST /api/knowledge-system/reset` (`manage_settings`; 400
    without the confirmation, 409 when busy; clears the overview caches).
  - "Being set up": `/api/ask`, `/api/ask/stream` and `/api/agent/stream`
    answer 409 `{"error": "This knowledge system is being set up…",
    "setup": true}` while not `READY`; the playground and evaluations are
    not gated (setup evaluates before go-live).
  - Tests: reset empties the listed stores and keeps users, sessions and
    flows, no re-import after a reset, refusals without the confirmation
    and while work runs, the API for a viewer and an admin, the gate
    (`test_pg_store.py`); permission rules (`test_auth.py`); session and
    sign-in tests patch the setup state (in CI there is no `urls.json`, so
    a new test database starts `NEW`). Full suite 289 OK, both with and
    without `data/` mounted.
- [x] **4. UI**, checked in the browser locally.
  - **Knowledge system** page (Admin, `manage_settings`;
    `web/static/js/knowledge_system.js`): state, origin, set-up time, URL
    count; Reset with what is deleted (with live counts) and kept, a typed
    `RESET` that enables the button, and the refusal note; History of
    events.
  - **Set up your knowledge system** page: where an admin lands while not
    set up (the six setup steps, and that guided setup is not in this
    version yet).
  - A notice on every page but setup's own while not set up: admins get
    "isn't set up yet. Set it up", others "being set up". The shell
    refreshes it after a reset (`rag:setup-changed`).
  - Retrieval: a question refused because setup is not done shows the
    message as the reply, not "Something went wrong", and the workflow
    chart stays idle (the error carries `setup`).
  - Local check: the page showed READY/existing with 75 chunks and 2,458
    URLs; the reset deleted 2,536 rows, the notice appeared, the history
    showed the reset and the first run, `/` landed on setup, a question
    got the notice; no console errors.
- [x] **5. Docs, snapshot helper, PR.**
  - `scripts/aws/snapshot.sh create [note] | list`: snapshots the volume
    tagged `rag-systems-data` (EC2's data volume, `/dev/sdf`) and waits for
    it; `list` checked against the account (no snapshots yet); `create`
    not run (it costs, and is the user's call before an EC2 reset).
  - README: "Adding URLs to ingest" (imported once into `kb_urls`), a new
    "Knowledge system: setup and reset", project layout entries;
    infra/README "Backup" names the helper.
  - Deploy after merge: EC2's web app imports its `urls.json` and starts
    `READY`/`existing`.

## Open questions for the user

1. **When to reset EC2.** Recommended: not until setup itself works
   (#10–#14), since after a reset the live assistant has nothing to answer
   from until setup has built a new knowledge base. Locally I will reset
   freely to test.
2. **Sessions on reset.** Recommended: keep people's past sessions (history)
   but clear the agents' own conversations and learnings. Or clear sessions
   too, for a completely clean start?
3. **Flows on reset.** Recommended: keep flow versions (history); the
   blueprint (#11) writes a new version for the new domain. The live flow
   stays as it is until setup's "Go live" (#17).
