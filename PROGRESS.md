# Progress

Running log of what is done and what is open. Update it after every step;
read it first when picking work back up. Decisions and the longer-term
plan live in [AWS_MIGRATION.md](AWS_MIGRATION.md).

Last updated: 2026-10-22

**Repository:** moved on 2026-10-09 to the public, open-source repository
`ingenmind` (Apache-2.0) as one fresh commit; the private
`rag_systems_cloud` is archived with the full history. Identifiers of the
maintainer's deployment are placeholders here (`<instance-id>`,
`<account-id>`…), real values in the git-ignored `DEPLOYMENT.local.md`.
Issues were recreated here (#1–#19, renumbered; the finished ones closed)
and the backlog is the open issues (#20 on); earlier pull requests are
cited as "PR N (earlier repository)". `main` is protected (#24): pull
requests only, CI must pass, also for admins.

**Product:** the app and README present it as **IngenMind — "one tool for
information retrieval, with agents at every step"** (PRs #50–#52): the
sidebar, sign-in and tab titles say IngenMind ("Retrieval with agents");
the README opens with the product pitch, the agents, how it works, and
fresh screenshots (#37). Internal names (Compose project, AWS resources,
Langfuse project) stay `rag-systems`. The website (ingenmind.tech) did not
answer yet on 2026-10-09, so it is not linked.

**Deploying (#25, since 2026-10-09):** merging a pull request into `main`
is the whole process. CI ("Terraform and app checks") runs, then **Publish
reviewed source archive** uploads the commit, then **Deploy to EC2** deploys
it through Systems Manager, all by themselves. There is no second approval:
the reviewed, merged pull request is the user's approval (their choice).
The deployer role (`infra/terraform/deploy.tf`) trusts only the
`production` environment (only `main` may use it) and may only run the
`rag-systems-deploy` command document (`deploy.sh` for a full commit SHA)
on the app instance. A deploy to a stopped EC2 fails and says so. If CI
fails on a merge for a reason outside the code (Docker Hub was slow), re-run
the failed job and publish and deploy follow. CI pulls its pgvector image
from Docker Hub signed in (`DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`
secrets). Tunnels close after 60 minutes idle (was 20).

**Epic #19 (RAG Initialization Agent) is done (closed 2026-10-10):** the
MVP #9–#17 is merged and deployed, the last being **#15** knowledge
metadata (PR #56, merged and deployed as `70d15d4` on 2026-10-10;
[plan](docs/plans/knowledge-metadata.md)): every page is labelled from the
blueprint (topic, organisation, authority, dates, content type, the
blueprint's own fields); the knowledge-base agent may filter by
organisation and content type and prefer a topic (**#57**: a topic only
ranks pages higher, never hides them; compared on 6 questions, twice per
side: 10/12 met after vs 8/12 before, none worse); the Sources, research and
citation views show the labels; **Relabel** on the setup Build card labels
the plan's pages and research's (local build: 22 of 22). Still open from
the epic, as their own issues: #39 (guided setup on EC2 for real) and #18
(learning from live use, after the MVP). **Done 2026-10-11: #60, install with one command**, released as
**v0.1.0** (PR #61; `curl -fsSL https://raw.githubusercontent.com/cherrydasika/ingenmind/main/install.sh | sh`;
the image on GHCR, public, amd64 and arm64; demo first, your own domain
from Profile → Knowledge system; the demo has its own flow, `lakeshore_demo`;
plan: [docs/plans/install.md](docs/plans/install.md)). **#62, new domains
start with one agent** (PR from `flows/starter`; plan:
[docs/plans/starter-flow.md](docs/plans/starter-flow.md)): the Agent
component and the starter flow; setup's candidate starts from it and keeps
what the admin grew; the builder shows unapplied settings as fixed, blocks
publishing with "soon" components, and asks for an evaluation before a
version goes live. On the local domain the starter met 14/16 vs the full
flow's 11–12/16, ~2.3× faster, ~4× fewer tokens.

**Done 2026-10-12: a simpler Agent Builder layout** (PR #72, deployed):
one top bar (flow, Live pill, Test, Versions & live, ⋯ menu, Publish,
Save draft), components down the left, the canvas with its own tools
(undo, Architectures, Tidy up, Snap), and Test as a panel on the right.
Fixed on the way: a new flow was framed before its nodes were measured
(off-centre), and an agent's Ports listed every resource against every
port. A narrated walkthrough video was recorded with Playwright (local
only); it left three "Demo assistant" flows (not live) in the
local database. Starting locally, other projects' containers
(`rag_systems-*` Langfuse, MWAA runner) restarted with Rancher Desktop and
held port 5432; stopping them let local Langfuse start.

**Roadmap to v1.0 (since 2026-10-21):** every open issue is numbered
R01–R63 in the order to pick it up (tracker
[#120](https://github.com/cherrydasika/ingenmind/issues/120), pinned): 39
new issues (#81–#119) and the 24 older ones renamed into the same
sequence, in nine phases (foundations, guardrails, reliability and models,
retrieval quality, memory, agents and orchestration, tools and
integrations, dashboards and alerts, ship). The agent works through them
in order on its own, merging each when CI passes, and asks only for what
needs a person (signing in, AWS changes that cost money or touch security,
third-party accounts); a blocked issue gets a comment saying what is
needed, and work moves on. **Where to pick up:** the lowest open R-number
on #120 that isn't waiting on the user.

- **R01 (#39) waits on the user:** guided setup on EC2 needs them signed
  in (Cognito; "Forgot your password?" if theirs is lost).
- **R02 (#81) done:** [docs/security.md](docs/security.md) (assets,
  attackers, trust boundaries, the OWASP Top 10 for LLM applications and
  the web app and deployment, each gap mapped to its R-issue) and
  [SECURITY.md](SECURITY.md). Fixed on the way: the scraper (setup,
  ingestion, research) could fetch internal addresses (server-side request
  forgery: the database, Langfuse, the metadata service); it now fetches
  only public addresses and checks each redirect
  (`FETCH_ALLOW_PRIVATE=true` for an intranet).

- **R03 (#21) waits on the user:** rotating the EC2 Langfuse secrets
  changes the SSM parameter and needs EC2 running (AWS approval).
- **R04 (#82) done:** `.github/workflows/security.yml` (every PR, main,
  weekly): pip-audit, npm audit, Trivy on the web image, gitleaks over the
  history, CodeQL; every Action pinned by SHA; Dependabot. Fixed: Vite
  (npm audit), Debian security updates in the image, pip removed from it
  (its vendored urllib3, msgpack, setuptools). Triaged with reasons in
  `.github/security/`: Pillow < 13, held back by fastembed. Also fixed a
  flaky auth test (a tampered cookie was added beside the valid one).
- **R05 (#83) done:** `app/web_security.py`: a strict CSP and the
  standard headers on every response (HSTS over HTTPS), cross-site
  state-changing requests refused (Origin / Sec-Fetch-Site;
  `ALLOWED_ORIGINS`), admins signed out after `ADMIN_IDLE_MINUTES` (60)
  idle, background polls excluded. Every page and a Plotly chart checked
  in a browser: no CSP violations. Model and page text reach the page only
  through `markdown()`/`richText()`, which escape first.

- **R06 (#84) done:** `app/usage_limits.py`: questions per minute and
  tokens per day per user (role defaults, admins more; per-user overrides
  on the Users page, which also shows today's questions and tokens), an
  optional global daily cap ("busy"), 429 with Retry-After on the Ask,
  playground and classic routes; a run's tokens are charged when it
  finishes, even if it fails. Research runs aren't limited separately
  (that belongs with per-run budgets, R15).

- **R07 (#85) done:** `app/audit.py`: middleware records every
  state-changing request whose route needs more than `query_rag` (a test
  lists the few exceptions, so new routes are audited by default), plus
  sign-ins, sign-outs, refused attempts and confirmations; field names and
  short non-secret values only; `X-Request-ID`; append-only by trigger.
  Admin → Audit log (filters, CSV). Also: Dependabot raises pip versions
  only when the range doesn't allow them (four minimum-bump PRs closed),
  and CI's tests start PostgreSQL in a step, signing in to Docker Hub only
  when the secret exists, so Dependabot's PRs (which get no secrets) run.

- **R08 (#22) code done, waiting on the user to apply:** daily snapshots
  by Data Lifecycle Manager (`infra/terraform/backup.tf`: 03:00 UTC, 14
  kept) and `scripts/aws/restore-test.sh` (a new volume from a snapshot,
  a throwaway database on it, row counts compared with production, all
  removed after). Needs approval for `terraform apply` and the test run.
- **R09 (#23) waits on the user:** a domain, HTTPS and inbound access.
- **R10 (#20) done:** a signed-in question's Langfuse trace carries the
  app's session and user IDs, and a session's questions group together
  (checked locally on a demo preview; the identity plan is done). Fixed on
  the way: local Langfuse had stored no traces since 2026-10-13 because
  its `minio` container was on no network (recreated).

- **R11 (#86) done:** `app/pii.py`: emails, phone numbers, card numbers
  (Luhn), NI numbers, IBANs and IP addresses in a question are masked
  before any model, tool, session or trace sees it; the flow's input
  guardrail can refuse or allow instead (`personal_data`); web searches
  and everything sent to Langfuse are masked always (Langfuse's `mask`
  hook). The Ask page says what was removed. Checked end to end with real
  calls: the session and the Langfuse trace hold `[email]`, the address
  appears nowhere in Langfuse.

- **R12 (#87) done:** `app/injection.py`: every passage, search result,
  fetched page and API reply given to an agent is fenced as quoted data
  (the agents' rules say never to follow it); instruction-like passages
  are withheld from every model, the evidence and answer evaluators too;
  researched pages like that are never added (`blocked_injection`);
  Knowledge base → Ingestion lists the withheld passages. Checked end to
  end on a demo preview with an ad-hoc page carrying "ignore all previous
  instructions… CANARY-7731": the passage was retrieved and withheld, and
  the canary appears in no answer and no trace.

- **R13 (#88) done:** tool guardrails: an agent can only call the tools
  it was offered (`run["offered"]`); `api_tools.check()` validates an API
  call's arguments (schema, types, length, no personal data, links or
  instruction-like text), a per-tool budget per question (`max_calls`),
  and refuses `side_effects` tools until approval exists (R32). Refusals
  go back to the agent and into the trace with the reason. Checked with
  real calls: weather lookups pass.

- **R14 (#89) done:** a built-in red-team set (24 questions that must be
  refused; `{domain}` becomes the flow's domain) runs on every published
  version; the Agent Builder's Versions panel shows each version's share
  refused, and going live below `RED_TEAM_MIN` (90%) asks once more. It
  doesn't count as the evaluation going live needs. Real run on the demo
  flow: 24 of 24 refused in about 45 s. Not done: writing red-team
  questions from each domain's blueprint (the generic set names the domain).

- **R15 (#90) done:** a flow's Start node sets the run's budget (tokens,
  seconds, model calls, tool calls; 0 none), checked before every agent
  turn (looking one turn ahead for tokens) and tool call; when it runs out
  the agents answer with what they have and the Ask page / Test panel say
  so. A circuit breaker skips an API for 2 minutes after 3 service failures
  in a row (not request errors). Real runs on the demo: 3,000 tokens
  stopped early and said so (one turn alone used ~7,500: budgets are
  checked between turns); 12,000 finished inside it. Provider fallback is
  R16.

- **R16 (#91) done:** `llm.py` keeps one backend per provider and model;
  `LLM_FALLBACK` is tried in order on provider failures (timeouts, 429,
  5xx, unreachable; never on request errors), with a breaker per provider
  (3 failures → skipped 60 s); `LLM_FOR_GUARDRAILS/EVALUATORS/REWRITING`
  give those jobs their own model; an LLM node's `chat_provider` /
  `chat_model` pick an agent's model on the local runtime (traced with
  that model; a warning on AgentCore, an error if the provider isn't set
  up). Monitoring → Overview → Models lists them with list prices
  (`app/model_prices.json`).

- **R17 (#27) code done, check on EC2 left:** a harness turn whose stream
  carries no usage (AgentCore reports usage only on an agent's final
  turn) is estimated from the text sent and received (~4 characters a
  token) and counted in `usage.estimated_turns`, so totals, budgets (R15)
  and costs aren't low. `AGENTCORE_STREAM_LOG=1` logs each harness event's
  type and any usage it carries (never text), to find where AgentCore puts
  the real numbers: needs EC2 running.

- **R18 (#92) done:** progress already streamed; added heartbeats (a
  comment line every 15 s, for the SSM tunnel and proxies) and **Cancel**
  on the Ask page: closing the stream sets a stop the agents and tools
  check like a budget (`agent.CANCELLED`), so nothing more is spent. First
  progress arrives in ~80 ms on a demo preview. The answer still appears
  only after the answer check (not token by token: nothing unreviewed is
  shown).

- **R19 (#93) done:** `app/answer_cache.py` reuses checked answers on
  the Ask page for questions that mean the same (same key words in order
  *and* embeddings ≥ 0.97, same live version and knowledge base); never
  live-tool, research, personal-data or follow-up answers. Cleared on going
  live, start over, restore and setup go-live; hit rate and Clear on the
  Overview. Real check on a demo preview: 15 s → 71 ms for a reworded
  question. Found on the way: embedding similarity alone let "Off-Peak
  return" reuse the "Anytime return" answer (a wrong fare); the key-word
  signature now stops that, and a test covers it.

- **R20 (#1) done:** Evaluations → **Search quality**
  (`app/flows/search_eval.py`): a test set written from passages spread
  over the knowledge base (each question knows its passage; real questions
  added by hand with their page), and runs that search each question once
  and score every setting (meaning / keywords / both with RRF_K 1–60 × top
  3–10) by hit rate and MRR, with no model calls. Heatmap with the current
  setting outlined, comparison with an earlier run, misses per cell; runs
  saved. Checked on a demo preview (6 passages, too small to choose
  settings): meaning-only and fused found every passage, **keywords-only
  only 33–40%**: `plainto_tsquery` needs every word of the question in the
  passage, so everyday questions rarely match; worth looking at with R21/R22
  on a real knowledge base.

- **R21 (#3) done:** structure-aware chunking (`CHUNKING=structure`,
  default still `fixed`): cut along headings, paragraphs and list items,
  each chunk starting with and storing its heading path; Sources shows a
  page's chunks with their headings; `python -m rechunk` re-cuts the stored
  pages through the worker, keeping labels. Search quality now matches a
  re-cut passage by its text, so one set compares chunkings. Compared on 40
  Trainline pages with a set written from each chunking: each favoured its
  own (hybrid top 3: 0.96 vs 0.90 on the fixed-written set, 0.98 vs 1.00 on
  the structure-written one), so fixed stays the default; worth repeating
  on a real domain's knowledge base with longer pages.

- **R22 (#2) done, off by default:** a local cross-encoder reranker
  (`app/rerank.py`) after fusion, switched by a flow's Retriever (`rerank`,
  `rerank_candidates`) or `RERANK`; shown in the workflow chart and traced;
  a failure keeps the fused order. The search test can add reranked rows
  and times it. On 150 Trainline pages / 98 questions: MiniLM MRR 0.88 →
  0.92 but hit rate top 5 0.99 → 0.97 (~0.2 s a question); bge-reranker-base
  no better at ~1.3 s. No clear gain, so it stays off.

- **R23 (#5) done:** citation previews on the Ask and Sessions pages: each
  `[n]` shows its passage with the supporting sentences highlighted (hover,
  keyboard focus, phone tap; Escape closes), and "Open the page" scrolls to
  them (text fragment). The browser now receives cited chunk text, only for
  answers that passed both checks, never instruction-like chunks, ≤1,200
  characters (README Guardrail Scope updated). Checked on a demo preview
  with a real question: the right two sentences were marked.

- **R24 (#36) done:** page addresses in Sources lists (Ask, Sessions,
  evaluations, golden and search sets) show the site as well as the path
  (`thetrainline.com/via/trips/…`, no scheme or "www."); the full address
  on hover.

- **R25 (#35) waits on the user:** a data change on the deployed UK-rail
  knowledge base (EC2, or guided setup there: R01); noted on the issue.
- **R26 (#94) done:** follow-up context from the session's stored turns
  (`app/conversation.py`) instead of process memory, a standalone-question
  rewrite the agents get (both runtimes), a rolling summary for long
  sessions (bounded prompt), the local runtime's history capped by size,
  and New conversation on the Ask page. Checked on a demo preview across a
  web app restart.

- **R27 (#95) done:** the agents read agent memory (`AGENT_MEMORY_USE`,
  default on): search results ranked by and labelled with each site's
  validation record, tools failing most recent calls skipped, the rewriter
  told which kinds of rewrite worked (strategies only, no queries). Counts
  decay (30-day half-life); admins pin or forget entries; evaluations run
  with memory on, off or both. Demo comparison (8 questions): 7/8 met both
  ways, no gain shown yet (nothing for memory to steer on the demo).

- **R28 (#96) done:** opt-in long-term user memory (`app/user_memory.py`):
  Profile → Memory to turn it on, see each fact with its source question,
  edit or delete any or all (off deletes all; consent audited). Facts come
  from the user's own questions only, reach only their Ask-page runs
  (supervisor, and the output and answer checks), never web searches.
  Found on the way: the checks blocked personalised answers until they saw
  the facts too. Export/delete with the rest of a user's data: R59.

- **R29 (#97) done:** a typed task/result contract between agents and
  sub-agents (`app/contracts.py`): `ask_<id>` takes a validated Task (goal,
  inputs, constraints, expected output, turn budget, deadline, the last two
  enforced); the run comes back as a validated Result (status, findings,
  citations, missing, question, confidence, cost) built without an extra
  model turn, traced, and phrased for the parent with what to do per status.
  Shown in the Agent Builder. Checked with real calls on the demo: a filled
  task and a done result; a nothing-found result is partial, not failed.

- **R30 (#98) done:** durable runs (`app/durable.py`): LangGraph
  checkpoints in PostgreSQL plus a run record with a snapshot of the run's
  own state after each step; at startup interrupted Ask-page runs resume and
  their answer joins the session (the open Ask page picks it up); transient
  step failures retry with backoff; Monitoring → Runs (resume / cancel);
  retention 7 days. Killing the web app mid-run and restarting resumed the
  run (twice). Found on the way: holding a transaction while the
  checkpointer's setup ran `CREATE INDEX CONCURRENTLY` hung a fresh
  database's first start; now a session lock. Guardrail classifier errors
  are now logged. **Deploy note:** the image gains
  `langgraph-checkpoint-postgres` (CI builds it).

- **Fix (#155, merged):** the checkpointer's setup ran at import and its
  `CREATE INDEX CONCURRENTLY` deadlocked the demo seed on a fresh database
  (new installs with the demo); now set up before the first run.

- **R31 (#99) done (#156):** Planner component (`app/planner.py`,
  `_Run.run_plan`): a task graph for its sub-agents, ready tasks in parallel
  through the R29 contract, results passed to dependants, one re-plan after
  a failure, a composed answer; plan panel under the Ask page's workflow
  chart and in the playground; gallery architecture "Planner–executor".
  Found in the real check (local rail domain, 4 multi-part questions mixing
  the knowledge base and live weather): a task that found nothing still fed
  its dependants (now it counts as failed: they are skipped and re-planned);
  the composer cited "[live]" labels (now stripped in code), answered parts
  the reports said were missing, and dropped live parts (prompt fixed: 6/6
  honest and complete on a fixed report set, was 0/4; end to end it still
  sometimes says "yes" on the borderline question, and the answer check
  catches it); the re-plan gave a
  knowledge question to Live data (prompt fixed). End to end, 2 of 4
  questions pass every time; the other 2 fail the answer check on the live
  flow too (the knowledge base only half-covers them), so not a planner gap.
  Local Compose note: the webapp container's bind mounts had gone stale
  (empty `/app/dags`); `up -d --force-recreate webapp worker` fixed it.

- **R32 (#100) done:** Ask a human component (`app/human.py`, flow node
  `human`): **clarify** (before the agent: an ambiguous question gets one
  question to the user; their next Ask-page message is the reply) and
  **approve** (after the answer check: an approver with manage_agents
  approves the answer and any proposed side-effecting tool calls in
  **Monitoring → Approvals**; they run only once approved). LangGraph
  `interrupt()` + the R30 PostgreSQL checkpoints: the run is `waiting`, a
  restart leaves it waiting, the answer resumes it (`Command(resume=…)`).
  Timeout (`timeout_minutes`, swept every minute): clarify goes on as asked,
  approve rejects. Playground and evaluations never wait; a flow with the
  step skips the answer cache. Gallery: Human-in-the-loop is ready (clarify
  + approve). **Done-when checked on the local stack:** a real run paused for
  approval, the web app restarted, the approval (HTTP, and again by clicking
  Approve in the browser) resumed it and it finished; a second decision is
  refused. Clarify → reply → approve ran through `web_api` with a real
  session (turns settle and update in place). Not in this issue: approving
  researched pages before ingestion (follow-up issue), and notifying
  approvers (R55 alerts, #113).

- **R33 (#101) done:** Blackboard component (`app/blackboard.py`,
  `_Run.run_board`): a shared board of typed entries (question, fact,
  hypothesis; author, confidence, status); a controller picks one agent and
  one open question per step (may post sub-questions and hypotheses) or
  finishes; findings → facts, what's missing → open questions; tool-less
  agents post hypotheses only; the user's question stays open until the
  controller finishes; a question never goes back to the agent that posted
  it. Board live in the playground; gallery card ready (Controller +
  Knowledge base, Live data, Analyst). **Done-when checked in the browser:**
  applied from the gallery to a new draft flow ("Board check", left in the
  local database), saved, run in the playground with the board updating.
  Answer check on a two-part question: 2 of 4 board runs passed vs 0 of 2
  for the live flow. **Fixed on the way (also broke R31's Planner card):**
  the gallery diagram and the builder's auto-layout only treated `agent`
  nodes as having sub-agents, so applying a Planner or Blackboard card
  threw "reading 'x'" and left the canvas blank.

- **R34 (#38) done:** the investigation and clarification agents
  (`app/evidence_agents.py`) replace the knowledge-base subflow's two
  placeholders: conflicting evidence is investigated from the passages'
  sources, organisations and dates (conditions / newer / more authoritative
  / unresolved → both versions, cited); insufficient evidence passes on what
  is supported (cited, so the answer check can verify it), what is missing,
  and a question for the user only when the answer depends on a missing
  detail. Old saved flows with the placeholder nodes run the agents too
  (same route ids). Supervisor prompt: never fill a reported gap from its
  own knowledge, never generalise past the findings. Real checks (local
  rail domain, live built-in flow): "Are children under 5 free on all UK
  trains?" failed before (the supervisor added "standard across National
  Rail"), passes now from the clarification agent's cited findings; "How
  much does a 16-25 Railcard cost?" now says what the knowledge base has.
  The travel_basics set (no conflicting/insufficient cases) was 8/15 after
  vs 10/15 before, within run-to-run noise (the two new failures passed 3
  of 4 reruns).

- **R35 (#102) done:** collections (`app/knowledge_collections.py`): named
  knowledge bases with description and roles; sources assigned per source
  (`knowledge_collection_sources`, survives re-ingestion; unassigned = the
  main collection); searches limited to the user's visible collections
  (`storage.meta_filter` "collection"); Retriever `collection` setting →
  knowledge agents; gallery "Knowledge agents (collections + router)" built
  from the deployment's collections (brief with `{collections}`, expanded per
  user; guardrails follow it); cited sources carry their collection; hidden
  collections' agents aren't offered and aren't named. Data → Knowledge base
  → Collections tab. Generic agents now keep each fact with what it applies
  to (no "all"/"standard" merges). **Done-when checked locally:** HR handbook
  (an ad-hoc policy) + UK rail travel: HR question cites the HR handbook,
  rail question cites rail, "Will Northwind reimburse my 16-25 Railcard, and
  how much does a Railcard save?" cites both; with HR limited to admins, a
  user's run neither searched nor named it. Local data added: the
  "hr_handbook" collection, its ad-hoc page, a user "Robin"
  (robin-r35@example.com), and the main collection renamed "UK rail travel".

- **R46 (#106) done (before R36, which needs it):** API keys
  (`app/api_keys.py`: per user, scoped ask/ingest/evaluate/admin within the
  owner's permissions, expiring, revocable, SHA-256 at rest, shown once) and
  the versioned API (`/api/v1/ask|sessions|documents|flows|evaluations`,
  OpenAPI at `/api/v1/openapi.json`, docs at `/api/v1/docs`). Bearer keys
  are checked in `auth.AuthMiddleware` (only /api/v1/, scopes, owner still
  active); rate limits apply; key changes and key-made changes are audited
  with the key prefix. Profile → API keys; Users → all keys. **Done-when
  checked over real HTTP** (a temporary server in the local container with
  only the setup-ready check patched, since local guided setup is mid-way):
  a key got a cited answer that passed the answer check; after revoking, the
  same call got 401 at once; a missing scope got 403, a non-v1 route 401.

- **R36 (#103) done:** A2A both ways (`app/a2a.py`): agent card at
  `/.well-known/agent-card.json`, JSON-RPC `POST /api/v1/a2a`
  (message/send → completed task with the cited answer + sources/answer check
  as data; tasks/get; input-required for Ask a human), API key with `ask`;
  the **Remote agent (A2A)** component as a sub-agent under the R29 contract
  (masked out, fenced in, its [n] stripped and sources named, recorded as a
  call for the answer check, tool-call budget + per-run limit + timeout,
  token from an env var, https only); traces linked via
  `ingenmind_trace_id`. **Done-when checked locally** (temporary server in
  the container, setup-ready check patched): the official A2A SDK client
  (python:3.11-slim container, a2a-sdk 0.3.10) got a cited answer that passed
  the answer check; a flow delegated to the remote agent and its answer
  passed 2 of 2 after telling the parent to name the remote source (the
  first run failed on citations). Not done: exposing knowledge agents
  separately ("optionally" in the issue).

- **R37 (#28) done:** a run's evidence decisions are saved with its result
  (`evidence`: decision, attempt, confidence, in order; labels and numbers
  only) and kept in the session, so a reloaded answer's workflow chart
  replays them and lights the branch taken (checked in the browser with a
  saved retry → good run: the retry and good paths lit, research and "not
  handled" skipped).

- **R38 (#104) done, except trying it in Claude Desktop (yours):** MCP both
  ways (`app/mcp_integration.py`, hand-written: no MCP SDK in the image).
  Admin → Integrations (add by URL, list and enable tools, token encrypted
  with INTEGRATIONS_KEY); the MCP tool component is runnable (server, tools,
  max_calls) and offers `mcp__<server>__<tool>` to generic agents, through
  `api_tools.check_arguments` (factored out of the API tool guardrails),
  fenced, recorded as calls. IngenMind's MCP server at `/api/v1/mcp`
  (search_knowledge_base within the owner's collections, ask). Generic
  agents now name the source of tool facts without [n] (fixed the answer
  check's citation failures for MCP and A2A facts). **Checked:** a flow
  called DeepWiki's public MCP server (3/3 passed the answer check); the
  official MCP Python SDK 2.3.0 client used our server (initialize, list,
  search, ask). Claude Desktop config is in the README (via mcp-remote).
  Local data: the DeepWiki server is registered with ask_wiki_question
  enabled. Not done: stdio servers (would run commands from the admin page).

- **R39 (#105) done:** OpenAPI import (`app/openapi_tools.py`): load a spec
  by URL (JSON/YAML, local $refs), pick operations, prefix, base URL, auth
  (none / API key / OAuth client credentials; secrets in `secrets_box.py`,
  now shared with MCP); each operation is an ApiTool via
  `api_tools.all_tools()`/`get()` (the API tools resource validates and lists
  them; GET read-only, others side-effecting → approval). Admin →
  Integrations: import, Try it, remove. API tool results now ask the agent
  to name the API as the source; generic agents add no conversions or
  figures the tools didn't give (fixed "about 50°F"). **Done-when checked:**
  Open-Meteo's forecast spec imported from the page and tried; a flow's
  agent used it for Sheffield's temperature, 3/3 passed the answer check.
  Local data: the openmeteo_get_v1_forecast tool is imported.

- **R40 (#30) done:** "Draft the tool" on a data-source report that
  recommends an API tool (`app/tool_stub.py`, GET
  `/api/source-reports/{id}/tool-stub`, manage_knowledge): a draft
  `app/api_tools/<name>.py` in weather.py's shape from the integration
  sketch and the profile (typed parameters from the sketch's inputs, the
  key from an environment variable when the source needs one, TODOs where
  the endpoint or auth is unknown; checked to compile), shown and
  downloadable; nothing written into the app or registered. When the
  profile has an OpenAPI spec, it points to the no-code import (R39).
  Checked in the browser on the local Darwin report: drafted and downloaded
  darwin_rail_api.py.

- **R41 (#33) done:** setup's Sources step researches the blueprint's live
  areas too (`app/initialization/live_sources.py`, after site discovery, in
  its background work): per live area (at most 4) two web searches, the best
  few distinct sites fetched, profiled and judged as in research, saved as a
  data-source report, and the area's needed tool recorded (name, whether it
  exists here, method, endpoint) plus tools here that may help. The Sources
  step's Coverage card shows each live area's recommendation and tool.
  Checked on a copy of the local blueprint with two live areas: live
  departures → UK Rail Schedule API (api_tool), weather → a weather API with
  get_weather and the imported Open-Meteo tool listed as already here.
  (The recommendations' quality is R42's business.)

- **R42 (#31) done:** for an on-demand lookup the judge prefers a
  request/response API: the prompt says so, and a deterministic rule after
  the judge (`research.prefer_lookup_api`) puts the best-ranked eligible API
  (the same provider's first) before a recommended push feed, the feed as
  fallback, unless the task needs history, analytics or every update. The
  live test's bar is now `api_tool`. **Live runs (RUN_LIVE=1, local):** 3
  of 3 judged runs recommended an API (Darwin SOAP APIs twice, RTT
  Next-Generation API once); 1 passed every assertion; 2 failed the older
  website assertions (the National Rail website wasn't profiled, or wasn't
  listed as not recommended), which vary with what the search finds. An
  earlier run recommended nothing (cause not found).

- **R43 (#34) done:** a live-data gap's answer names the recommended source's
  provider and a link users can follow (`agent.official_source`: the same
  provider's profiled public page, else its site root, never the API docs),
  and that fact is recorded as evidence (a `source_recommendation` call on
  the knowledge-base task) so the answer check accepts it. **Checked** with
  the live default flow: "When is the next train from Leeds to York right
  now?" and "Is my 17:05 train from Sheffield to Manchester delayed today?"
  both answered "not available here … use National Rail Enquiries at
  https://www.nationalrail.co.uk/" and passed the answer check (1.0).

- **R44 (#32) done:** source profiles follow a website's terms page:
  `research.terms_link` picks a terms / conditions / legal / copyright link
  from the fetched pages' links (the same site first), and
  `profile_sources` fetches it and profiles the candidate again when a
  web-page or scrape-only profile's terms are still unknown. **Checked** on
  nationalrail.co.uk/live-trains/: it followed /terms-and-conditions/ and
  the profile's terms went from unknown to "non-commercial display only …
  no scraping or extraction, no redistribution".

- **R45 (#29) done:** Open-Meteo calls (`api_tools/weather._get`) retry once
  on a 429 or 503, after its Retry-After (1 s if none; not waited for when
  it asks for more than 3 s). Tests cover the retry, giving up after one,
  no retry for other errors, and a long Retry-After.

- **R47 (#107) done:** outbound webhooks (`app/webhooks.py`): hooks (URL,
  events, signing secret sealed with INTEGRATIONS_KEY, trusted flag) and a
  delivery log; HMAC-SHA256 signatures over "<timestamp>.<body>"; retries
  at 1 m, 5 m, 30 m, 2 h (a sweeper thread in the web app), then failed.
  Events wired where they happen: answer.failed_review (`_run_to_end`, Ask
  page runs), knowledge.gap (`_research_report`, Ask page runs),
  ingestion.finished (`ingestion_worker`), evaluation.regressed
  (`flows.evals._finish`, ≥5 points below the previous run), version.live
  (`flows.store.set_live`), ping. Admin → Integrations → Webhooks (add,
  secret shown once, Send test, delivery log, remove). **Done-when checked:**
  a receiver container verified the signature of all six event types; an
  untrusted hook got no question or task text; a first round rejected with
  401 stayed pending for retry.

- **R48 (#108) in part (open):** the connector interface and the first
  connectors that need no outside account (`app/connectors`): file upload
  (PDF, DOCX, PPTX, HTML, Markdown, text; DOCX/PPTX read from their XML, no
  new dependency) and S3/MinIO (list, fetch, changed-since; keys sealed),
  ingested through the ad-hoc path with a source id and provenance. UI on
  Data → Ad-hoc documents. **Checked:** three files from a local MinIO
  bucket imported; an answer cited the .docx's s3:// source and passed the
  answer check; a .pptx uploaded in the browser. **Left, needs you:** Google
  Drive (the done-when), SharePoint, Confluence and Notion need OAuth apps
  on your accounts; also setup's Sources step offering them, and permission
  sync. Local data: the `ingenmind-docs` MinIO bucket and the `handbook`
  connector (its keys sealed with a throwaway INTEGRATIONS_KEY, so it can't
  be browsed from the app).

- **R49 (#109) done:** scheduled re-crawl (`app/recrawl.py`): a schedule
  per web page (weekly default; daily/off on Ingestion → Freshness), the
  worker's round every 10 minutes (`RECRAWL=false` off), a conditional GET
  (`scraping.check_changed`; 304 renews the lifetime), else a re-ingest
  with labels kept (`recheck` keeps the hash check) that re-embeds only
  chunks whose text changed (`ingest` reuses stored vectors from
  `pg_store.page_chunks`) and removes the old ones; a chunk-text diff per
  change; stale when gone, failing 3 times or overdue twice over.
  **Done-when checked:** a page served from a local container was edited;
  on its next due check the round said changed, the old text was gone and
  the new stored, the diff recorded; the next check was a 304, unchanged.

- **R59 (#115) done:** data lifecycle (`app/data_lifecycle.py`, Profile →
  Privacy and your data, Users → Download data / Delete account and data):
  retention per kind applied daily; a person's data exported as JSON or
  deleted from PostgreSQL, Langfuse (by trace id; v4 has no trace listing,
  so ids come from the turns and observations by user id) and agent memory
  (local `agent_sessions`, AgentCore Memory events and records). The audit
  log's append-only trigger now allows only this code's anonymising and
  retention (both audited). **Done-when checked:** a test seeds a person's
  data through every module, deletes them and finds no row in any table
  mentioning their id, email, session or memory actor, while another
  person's data stays. On the local install a throwaway account's export
  and deletion left no row mentioning it. The Langfuse half couldn't be
  proven locally: Langfuse's storage is full (the Docker disk is 99% used),
  so the throwaway trace was never stored.

- **R58 (#18) done:** learning from live use (`app/use_learnings.py`, on
  Analytics): every real question's outcome, sources by answer quality, and
  four kinds of recommendation (refresh, drop or add a source; re-run an
  evaluation) that run only when an admin approves. A reset clears the
  learnings and recommendations. **Done-when checked:** tests show a reset
  clears them and that working out, refreshing and dismissing
  recommendations leaves pages, the URL list, ingestion jobs and re-crawl
  checks untouched until one is approved (then only that page goes). On the
  local install, the 32 earlier real questions replayed into 18 worked,
  13 wrong answers and 1 missing knowledge, and the Trainline bikes page
  (marked out of date by a thumbs-down) came out as "Refresh source". New
  questions couldn't be asked: the Anthropic API key has run out of credit
  (they were recorded as blocked).

- **R57 (#7) done:** analytics export for DuckDB (`app/analytics_export.py`,
  `export_analytics.py`, Analytics → Download export,
  `scripts/aws/export-analytics.sh` over SSM): ten Parquet files typed from
  PostgreSQL, `explore.sql` with views and example queries. People are
  salted-hash references; no emails or names; text only with
  `--include-text`. **Done-when checked:** exported the local install
  (32 questions, 386 runs, 34 evaluation answers…), copied the files out and
  ran every example query in DuckDB; a scan of every file found no email
  address or user id. The SSM path on EC2 was not run (the instance is
  stopped; starting it costs money).

- **R56 (#114) done:** nightly evaluations and a promotion gate
  (`app/flows/eval_gate.py`): chosen sets run each night on the live flow
  (claimed once a day in the database; a drop raises the R55 alert), a
  pass-rate trend per set on Evaluations, and going live needs a version to
  match or beat the live one on the gate sets (within a tolerance), or a
  reason kept in the audit log (`gate_overridden`; the Agent Builder asks).
  **Done-when checked:** a deliberately worse copy of the live flow
  (retriever top_k 1, no full-text search) and the live flow ran a 5-question
  set for real: live 75%, the copy 0% (no answer got as far as the check);
  making the copy live was refused with 409 "Gate check: 0% vs live 75%".
  Found on the way: a run with no checked answers had no pass rate and
  slipped past the gate; it now counts as 0%.

- **R55 (#113) built; the real Slack and email check and CloudWatch need
  you (open):** Monitoring → Alerts (`app/alerts.py`): eight rules (pass
  rate, cost today, error rate, p95 time, ingestion failures, guardrail
  spike, stale sources, evaluation regression) with threshold, window,
  on/off, mute and Send test; a 5-minute checker that alerts on breach and
  on clearing; history; delivery to a Slack incoming webhook (sealed), email
  over SMTP (`SMTP_*`, `ALERT_EMAIL_FROM`) and `alert.fired` webhooks.
  **Checked locally:** every rule's test alert reached a stand-in Slack
  endpoint (8/8) and a local SMTP server (8/8), and unit tests make every
  measured rule fire from real run records, resolve, and stay quiet when
  muted. **Left, needs you:** a real Slack incoming webhook URL and an SMTP
  account (e.g. SES) for the done-when; CloudWatch alarms for the EC2
  instance, disk, memory and health check (an AWS change; EC2 is stopped).

- **R54 (#4) done:** knowledge-gap report on Analytics
  (`app/knowledge_gaps.py`): each KNOWLEDGE_GAP's missing information kept
  with its session turn (server-side only: removed before any reply, and
  deleted with the session), embedded on first report and grouped into
  topics (cosine ≥ 0.78, named by the most typical item; identical wording
  when no embedding model is reachable), with questions, still unanswered,
  example questions and last asked, for the page's date range. Off-topic
  questions are blocked before a run, so they are never gaps. **Done-when
  checked:** three real questions through the Ask path (run in the web app
  container, as the local install is mid-setup): "toilets at London
  Victoria" became a topic (3 missing items in one group, still
  unanswered); the step-free question was answered and isn't listed; the
  reply never carried the gaps. Light/dark and phone width checked.

- **R53 (#112) done:** Monitoring → Operations (`app/operations.py`):
  yesterday's cost and slowest flow, agent and model at the top; for the
  period, p50/p95 time, error, timeout, guardrail-block and budget-breach
  counts, tokens and approximate cost per day, flow, agent, model, channel,
  source and user (filters for each), the cache hit rate (R19) and the
  ingestion queue and its failures. Runs now record user, channel, model,
  `timed_out` and `budget_hit` (channel: app, slack, teams, widget, or the source for the playground and evaluations); `agent_flow_run_agents` keeps each agent's
  model, seconds and tokens. **Done-when checked:** against the local
  database (194 runs in 7 days, 133 yesterday) the page showed yesterday's
  $3.64 and the slowest flow on one screen; two new playground runs were
  recorded with their user, model and per-agent rows; light and dark
  themes and phone width checked (no sideways scroll). The local install is
  mid-setup, so the Ask page refused questions: new runs came from the
  playground.

- **R52 (#6) done:** Monitoring → Analytics (`app/analytics.py`): views
  `analytics_questions` and `analytics_runs`; a question-journey Sankey, the
  search funnel (each turn now keeps a `funnel` summary, built in
  `agent._drive` where the real searches are: counts, chunk ids, pages), most
  and least cited sources and pages never found, a 3-D knowledge map
  coloured by use, daily questions / pass rate / p50 and p95 time / tokens
  and approximate cost, and content freshness; a date filter; admins
  (manage_settings). **Checked:** with real local questions, all seven
  charts render in the light and dark themes and at phone width (no
  sideways scroll); queries tested against the test database.

- **R50 (#110) built; the real Slack check needs you (open):** channels
  (`app/channels`): an embeddable widget (script tag, allowed origins via
  the frame's `frame-ancestors`, public or signed-in), a Slack app (signed
  Events API and interactivity, answers in the thread with sources and
  👍 / 👎 then a reason, follow-ups keep the session, people matched by
  email) and a Teams bot (Bot Framework token checked against Microsoft's
  keys, an Adaptive Card with sources and feedback). All go through the Ask
  page's path (`_ask_live`: limits, guardrails, answer cache, sessions);
  each turn records its channel (Sessions, Langfuse metadata, Monitoring's
  "By channel"). **Checked locally:** a signed `app_mention` against a
  stand-in Slack API was answered in its thread with the same reviewed
  answer the Ask page then gave (answer check passed, 0.91), and a signed
  👎 → "out of date" click was stored on that answer; the widget, embedded
  on a page served from another origin, answered and took a 👎, and a page
  from an origin not allowed was refused by the frame's CSP. Found and
  fixed on the way: a cached answer carried the channel of the question that
  filled the cache. **Left, needs you:** a Slack app in your workspace
  (manifest in the README) for the done-when, and an Azure Bot for Teams;
  both need the public HTTPS address (EC2 is stopped).

- **R51 (#111) done:** answer feedback (`app/answer_feedback.py`): thumbs
  up/down under each Ask-page answer, a reason (wrong, incomplete, out of
  date, not cited, other) and comment when down; kept per session turn
  (`answer_feedback`, shown on Sessions) and sent as the `user_feedback`
  BOOLEAN score on the answer's Langfuse trace (score id per turn, so a
  change replaces it). Evaluations → From answer feedback: an admin writes
  the corrected answer and adds the question to the `user_feedback` golden
  set. Monitoring → Overview: last 7 days' counts. No gap report exists yet
  to feed (R55's alerts can use `answer_feedback.summary`). **Done-when
  checked:** a thumbs-down given in the browser showed under the answer after
  a reload and on the Sessions page, and the score `feedback-23` (value 0,
  "Incomplete; …") is on the answer's trace in Langfuse (read from its
  ClickHouse: the local Langfuse v4 is events-only, so its read API is off);
  it was then added to the golden set from Evaluations.

**Done 2026-10-12 to 2026-10-20** (all merged and deployed; EC2 release
`6160793`):
- **#75, #78 Start over with a new domain:** in Setup, confirmed by
  signing in again (`/auth/confirm`, the password asked again, valid once
  for 5 minutes). The current domain is **set aside** (schema `ks_aside`),
  not deleted: until the new domain goes live you can go back to it; going
  live deletes it after a pop-up with Cancel / Back to it / Go live. The
  Profile master reset is gone.
- **#77 A light interactive scene** (dots that follow the pointer) while
  setup waits, instead of spinners.
- **#79 Section pages:** select or deselect all of a section's pages, and
  filter them.
- **#80 Golden set** on Evaluations: 5–100 questions with expected answers
  written from passages spread over the whole knowledge base (k-means over
  the embeddings, even share per page, no topic over about twice its
  share), with a map and per-topic bars that show clusters; remove any,
  then save as an evaluation set. Evaluation sets may hold 100 questions.
- The trains domain's 702 rows on the local build were deleted by the first
  start-over (before set-aside existed) and can't be recovered; the local
  install is now a Cheshunt planning setup at content selection.

**Done 2026-10-11: the sidebar reorganised** (Ask: Ask, Sessions · Build:
Agent Builder · Evaluate: Evaluations · Data: Knowledge base (Sources and
Ingestion as tabs), Ad-hoc documents · Monitoring: Overview (replaces
Home), Agent memory, Langfuse · Admin: Setup, Users · Account: Profile).
Pipeline docs and the Knowledge system page are gone (its controls are on
Profile, with an admin-only **master reset**: flows and sessions too, users
kept). The Ask page's workflow chart draws the live flow. Fixed on the
way: a test (`test_demo`) had made `lakeshore_demo` live in the local
database; restored to the built-in flow. Then the **Agent Builder's
component list**: Agent, Evaluator and Guardrail are one item each; a drop
asks for the kind and the name (kinds that cannot run where dropped are
greyed out with the reason). Then **#66, the generic Agent**: tools,
sub-agents (another Agent plugged into `agents`) and evaluators (`check`,
plugged into `checks`, any number at any level, retry with feedback) plug
into one Agent; unique names enforced; classic components kept under
"Classic"; existing flows unchanged. Local setup evaluation: generic
multi-agent 12/16 at 47k tokens vs classic 11–12/16 at 320–410k; the
one-agent starter (14/16) stays the default. Local test flows
`starter_check` and `generic_check` are left in the local database (not
live). Then **#69**: an architecture gallery in the Agent Builder (13
runnable architectures from the generic components, 3 coming soon; each
validates and compiles in the tests; Apply keeps the flow's name, scope,
brief and domain), sub-agent trees laid out beside their parent, and a
collapsible sidebar. Sessions moved to Monitoring; Langfuse opens after a
notice.

**Fixes on 2026-10-09** (deployed):
- PR #48: an honest "not available" answer passes the answer check (it is
  not judged on citations), and the supervisor no longer adds referrals
  the evidence does not name ("contact National Rail…"). Those blocked
  "toilets at Birmingham station?" and "train strikes tomorrow?" with the
  standard failure message.
- PR #49: research could not use long pages. The source validator saw only
  a page's first 2,500 characters (Network Rail's Birmingham New Street
  toilets were at 5,944), and the research guardrail refused anything over
  24,000 characters unread. The validator now sees the page's start plus
  the passages about what is missing, and the guardrail checks long pages
  in parts.

**The user's rules (2026-10-09):** nothing goes into the knowledge base
without the user's approval or rejection, in the front end (evaluations
already never ingest). **For now research during users' questions still
ingests automatically**, as the user asked: approving researched pages in
the app is planned (on the README roadmap), not yet an issue.

**Locally:** the checkout is `~/Documents/code/personal/ingenmind`; its
database is still the Compose project `rag_systems_cloud` (from the
archived checkout), so run Compose with `-p rag_systems_cloud`. Model calls
and web search work (`.env`: `LLM_PROVIDER=anthropic`,
`EMBEDDING_PROVIDER=local`, `TAVILY_API_KEY`). The install is at
`EVALUATING` (plan 4: National Rail's Railcards and Help and assistance,
49 chunks; readiness 81%, 4 gaps; candidate flow
`setup_uk_train_information` v4, not live). The web app and worker were
left running for the user; pgvector always stays running. Agents of any
kind: start from [AGENTS.md](AGENTS.md); the backlog is the open issues
([docs/BACKLOG.md](docs/BACKLOG.md)).

## Current state

- **Everything is shut down (2026-10-20):** EC2 stopped with
  `session.sh stop` (stack, AgentCore endpoints, instance); locally every
  container stopped and Rancher Desktop quit. Data kept everywhere. Start
  again: locally `scripts/start.sh` (stop the other projects'
  `rag_systems-*` and MWAA containers first if Langfuse won't start); EC2
  `session.sh start`, then the tunnels (app 28000, Langfuse 23000).
- EC2's active release is `6160793` (PR #80, deployed by GitHub Actions on
  2026-10-13; everything below is on it). Its agents run on the **AgentCore
  harness** with Claude Haiku 4.5 through **Bedrock** and Bedrock
  embeddings (checked in the running app 2026-10-11); locally they run in
  the app with the **Anthropic API** and local embeddings. Setup there is
  READY/existing with its 5 URLs and no confirmed blueprint, so no page is
  labelled and the setup steps don't show until #39; its live flow is
  unchanged (the classic built-in). Generic agents (#66) and the
  architectures (#69) have not run on EC2 yet.
  Sign-in is Amazon Cognito (AUTH_MODE=oidc); the first account,
  `admin@example.com`, works, and sign-out ends the Cognito session too.
- The live flow on EC2 is `travel_assistant` **v3** (UK trains and weather
  scope, UK rail supervisor); v0 (built-in), v1 and v2 stay in its history.
  Flow versions live in the EC2 database, not in git.
- Deploying a commit: merge it into `main`; CI, publish and **Deploy to
  EC2** run by themselves (see "Deploying" above). A commit CI skipped (docs
  only) is not published or deployed; the user publishes one by hand if
  needed (`gh workflow run publish-source.yml --ref main -f
  confirmation=PUBLISH`), which then deploys by itself.
- Access is private through SSM port forwarding: app on `localhost:28000`,
  Langfuse on `localhost:23000`. Sessions close after 60 minutes idle
  (Session Manager preferences, #25). Open the app at `localhost`, not
  `127.0.0.1`: Cognito only accepts returning to `localhost`.
- AWS CLI profile for this account: `personal`.

## RAG app on EC2

### Done

- [x] AWS foundation, private source delivery, secrets from SSM, Compose
  stack on EBS (see AWS_MIGRATION.md, Status)
- [x] End-to-end test on an empty database: 5 URLs ingested (41 chunks),
  retrieval, Bedrock answers, Langfuse traces, failure and retry behavior
- [x] Pinned Buildx and Docker log rotation; `stack.sh` start/stop runbook;
  graceful worker stop
- [x] `deploy.sh`: one-command deploys with fallback to the previous release
- [x] Retrieval page: live workflow chart (embedding → retrieval →
  generation) with planned guardrail rails; deployed in `b5d766b`

#### 2026-10-05 / 06

- [x] Flow builder layout (`35224ee`): components in a strip above a
  full-width canvas, details in a panel that slides over the canvas's right
  edge (node or edge selected, or the new **Flow details** button; Esc or ×
  closes it), larger node text, and flows open at 80% zoom or more (a big
  flow opens from its top). Checked in the browser locally and deployed
- [x] Guardrail scope on EC2: UK trains, read as broadly as possible, plus
  weather anywhere; visas, other transport and rail journeys that neither
  start nor end in the UK are blocked. Published through the flow API as
  `travel_assistant` v1 (scope) and v2 (supervisor domain "UK rail and
  weather": trains → knowledge base, weather → APIs, no Swiss or entry-rule
  tools), both made live. Tested on the classifier on EC2 (25 of 26) and end
  to end (a visa question blocked; Delay Repay and a Manchester forecast
  answered)
- [x] UK trains and weather is the built-in default in code (`6eda584`):
  `guardrails.UK_RAIL_SCOPE` and messages, `flows.registry` domain and
  `UK_RAIL_SUPERVISOR_INSTRUCTIONS`; the built-in eval set now expects its
  visa and Swiss timetable questions to be blocked
- [x] In-scope questions missing a detail are allowed (`a2385b6`): the
  classifier asked users to clarify "are there toilets at station in
  london?". It now also records `topic_in_scope`, and a CLARIFY on an
  in-scope topic is allowed; only greetings and questions it cannot place
  ("do I need to book?") get the clarify message. Weather with no place is
  in scope. Live flow published as v3 with the updated scope text
- [x] Follow-ups read in context (`da73a7b`): "what about London Victoria?"
  after a toilets question was judged alone and asked to clarify. The input
  check now sees the session's last three questions that passed it, kept in
  the web app's memory only (never stored, gone after an hour or a restart);
  the new question is still judged on its own, and a blocked question never
  becomes context. Replayed on EC2: both turns allowed, the agents answered
  about Victoria
- [x] Guardrail probe: 54 checks (UK rail, weather anywhere, newly blocked
  topics, vague in-scope questions, six follow-ups), all passing on EC2
  with Claude Haiku on Bedrock (run with the new files copied into the
  running container, then removed)
- [x] Simpler Retrieval page (`c3aa9c8`): question box, chat, live workflow
  graph. Search internals, the embedding plot, evaluation, research and API
  call detail, run metrics, agent turns, max turns and the session, microVM
  and memory panels are gone (the memory panel's endpoint already returned
  403). Testing and step-by-step traces belong to the Agent flow page; full
  traces to Langfuse. The quantisation demo moved to Pipeline docs; agent
  answers no longer build an embedding plot
- [x] Sources and answer check on the Retrieval page (`d0a55fa`): public
  results never carried searches or the answer evaluation, so both were
  always empty, on the old page too. A public result now has `sources` (page
  URL and citation numbers, never chunk text) and `answer_check` (verdict
  and scores only). Verified on EC2: Delay Repay → "answer check passed ·
  1.00", Sources (1)
- [x] Workflow graph (`27aa5c6`, `336bf9f`): one left-to-right graph, each
  step a box coloured by state with its time, the evidence check's four
  branches drawn out, retry and research looping back to Search, Research →
  Summarizer when a gap remains, a Now line and a Steps list. It reads the
  public activity events; the old chart listened for raw events that never
  reach the browser, so the evaluator's branch never lit. Verified on EC2:
  York step-free access + weather → retry, gap, research (1 of 3 sources
  passed), answer check 1.00 in 91 s, each branch lit as it ran
- [x] Deployed `336bf9f`, then stopped everything (2026-10-06): EC2 stack
  and instance, SSM tunnels; locally only pgvector is left running
- [x] Observability: one Langfuse trace per question (`question`, tagged
  with session, user, flow, version and source). The input guardrail now
  sits inside it (it ran before any trace and so was separate), every agent
  turn is a generation (system prompt, messages, reply, tool calls, tokens,
  cost) and every tool call a tool observation (input, result, ERROR on
  failure); parallel tool calls keep the trace context. Verdicts are trace
  scores: guardrail decisions, answer check pass/fail and its four scores,
  answer type, and each evidence decision and confidence. Verified on a
  local run (Anthropic key, local Langfuse v4): 14 observations in one
  trace, 9 scores, tokens and cost per agent turn
- [x] Deployed `8e1068c` (tracing + weather fix, 2026-10-07). On EC2 "Will
  it rain in Leeds, UK tomorrow?" → `get_weather("Leeds, UK")` found Leeds,
  England first time; one trace with 12 observations and 9 scores; answer
  check 1.00
- [x] **Assistant brief** (2026-10-07, deployed as `cddc6f2`): "Is there a pantry car on trains?" researched and tried to
  ingest Indian Railways pages. The supervisor node now has `brief` (the
  main prompt every agent reads first) and `search_country` (Tavily
  `country`, worldwide if Tavily rejects it); the research agent puts the
  brief's country in its queries, the source validator and research
  guardrail reject other countries' pages, and an empty guardrail scope or
  message follows the brief / names the domain. Tests pass; a pantry car
  case is in `travel_basics`. Live v3 saved no brief, so it gets the UK
  defaults (no new flow version needed). Next: the user checks the pantry
  car question and `travel_basics` on EC2 (agents cannot sign in);
  phase 3 — list the Indian pages in `research_chunks` on EC2 and delete
  them once the user agrees
- [x] **Remove ingested pages** (2026-10-07, deployed as `c7deca4`): the Ingestion tab lists the pages research added
  (`research_chunks`: why, publisher, score, expiry) and the Sources page
  lists curated pages; with `manage_knowledge`, pages can be filtered,
  ticked and removed after an inline confirmation
  (`POST /api/sources/remove`, `web/static/js/source_picker.js`). Lets the
  user remove the Indian Railways pages on EC2 themselves (replaces phase
  3 of the assistant brief)
- [x] **Data-source research** (issue #8, 2026-10-08, PRs 12–15 (earlier repository),
  deployed as `b0b7b3e`): a gap about live data profiles data sources
  (APIs, feeds, downloads), a judge recommends one with an integration
  sketch, reports and profiles are saved and reused, and the Ingestion tab
  lists the reports. Live test on EC2 passes (Darwin recommended, the
  National Rail website not). Details in the plan
- [x] Guardrail and evaluator generations sent token usage as
  `input_tokens` / `output_tokens`, which Langfuse neither counts nor prices;
  `tracing` now renames them to `input` / `output`

### Open

- [ ] Rotate the Langfuse secrets (they match the local development `.env`) (#21)
- [ ] EBS snapshot, restore test, backup frequency (#22)
- [x] Guardrails behind the rails: enforced server-side on every question,
  answer and research source (`app/guardrails.py`), whatever a flow contains
- [ ] Knowledge base gaps for UK stations: answers about station facilities
  (for example London Victoria's toilets) say the knowledge base has
  nothing; St Pancras is covered. Ingest station facility pages (National
  Rail) for the main stations (#35). Since PR #49 research can add a
  station's page when someone asks (Birmingham New Street: Network Rail's
  page and station guide pass validation)
- [ ] Sources list on the Retrieval page shows only the page path; add the
  site's domain (#36)
- [x] Retake `docs/images/multi-agent-answer.png` and
  `docs/images/flow-builder.png` (#37, PRs #51 and #52; plus
  `setup-readiness.png` and a new `flow-evaluation.png`)
- [x] Local model calls fail (`classifier_unavailable` on the local stack):
  `.env` sets no `LLM_PROVIDER`, so it defaults to Bedrock with no AWS
  credentials; the Anthropic key is set. Add `LLM_PROVIDER=anthropic` (and
  an embedding provider that matches the local vectors) to `.env`. Done:
  `.env` sets `LLM_PROVIDER=anthropic` and `EMBEDDING_PROVIDER=local`
- [x] Weather tool: Open-Meteo's geocoder found nothing for "Leeds, UK"
  (found in the first full agent trace; the agent retried with "Leeds").
  The tool now searches the name before the first comma, within the country
  when the rest names one (UK, England, USA…, or a two-letter code), and
  picks among the matches by region or country, then population. Live:
  "Leeds, UK", "Newark, UK" (Newark-on-Trent, not New Jersey), "Salzburg,
  Austria", "Inverness, Scotland". A region alone with no country
  ("Newark, Nottinghamshire") still gets the most populous match worldwide
- [ ] Langfuse v4 (events-only mode) has no public traces or scores API;
  read scores in the UI or ClickHouse. On EC2 a trace's steps are in
  ClickHouse table `events_full` (`trace_id`, `name`, `input`, `output`),
  as used on 2026-10-09 to find why answers failed
- [ ] AgentCore harness reports token usage only on an agent's final
  (end_turn) invocation, not on turns that stop for a tool call (seen in the
  first EC2 trace: 0/0 tokens on tool turns). Flow metrics' token totals on
  EC2 undercount for the same reason. Inspect the raw stream for where the
  usage of tool-call turns goes (#27)
- [ ] After a reload the workflow graph cannot show which evidence branch
  was taken (the saved public result has no decision) (#28)
- [ ] Public access prerequisites: authentication, domain and HTTPS, inbound
  path, public IP decision, alarms and logs, Terraform plan and security
  review (#23)
- [x] GitHub Actions deploy and a longer SSM idle timeout (#25, PRs #53 and
  #54): merging deploys by itself; tunnels close after 60 minutes idle

## agentcore-kb (Bedrock AgentCore agent over the EC2 knowledge base)

A Bedrock AgentCore Harness (Claude Haiku 4.5, eu-west-2) whose one tool,
`search_knowledge_base`, is an MCP server over `rag_chunks` on EC2. Code in
[agentcore-kb/](agentcore-kb/); it can move to its own repo later with
`git filter-repo --subdirectory-filter agentcore-kb`.

### Decisions

- Region eu-west-2, next to the database (the original spec said us-east-1)
- Model `eu.anthropic.claude-haiku-4-5-20251001-v1:0`; Claude 3 Sonnet,
  named in the spec, has reached end of life in us-east-1
- Database: existing `rag_chunks` on EC2, so queries are embedded with Titan
  Text Embeddings v2 (256-dim, normalized) to match the stored vectors, not
  all-MiniLM-L6-v2 (384-dim)
- MCP server runs on AgentCore Runtime (streamable HTTP) behind an
  AgentCore Gateway; stdio mode for local tests. A Harness can only reach
  remote tools, so a local stdio server cannot be its tool.
- `mcp` SDK 2.x (`MCPServer`, formerly `FastMCP`)

### Done

- [x] Verified: Harness API (`CreateHarness`, `InvokeHarness`) exists in
  boto3 and in eu-west-2; Gateway targets support MCP server endpoints and
  can call the Runtime with the gateway's IAM role (no OAuth needed)
- [x] `mcp_server/server.py`: `search_knowledge_base(query)` over
  `rag_chunks`, Titan v2 256-dim query embedding, top 5 unexpired chunks as a
  numbered list; fails fast if the database or table is unreachable
- [x] Tested locally against the local `rag_systems_cloud` database: stdio,
  streamable HTTP from another container, and the built arm64 image.
  Similarity scores match the RAG app's dense search (0.732 Austria, 0.881
  Belgium)
- [x] `agent/deploy.py` (create or update the Harness), `agent/invoke.py`
  (streamed answer); request bodies validated offline against the API
  schema, stream parsing tested with a fake stream
- [x] `Dockerfile` (arm64, non-root), pinned requirements, `.env.example`,
  README listing every deviation from the original spec

- [x] Terraform (`infra/terraform/agentcore.tf`): runtime and endpoint
  security groups, pgvector access from the runtime only, interface
  endpoints behind `agentcore_endpoints_enabled` (default off), free S3
  gateway endpoint, ECR repository, IAM roles for runtime, gateway and
  harness. Plan: 22 to add, 0 to change, 0 to destroy.
- [x] `aws_instance.app` ignores AMI changes; before this, every plan
  wanted to replace the instance because the AMI parameter tracks the
  newest Amazon Linux image
- [x] `aws_instance.app` also ignores `associate_public_ip_address`: a
  stopped instance has no public IP, so every plan while EC2 was stopped
  wanted to replace it. With EC2 stopped the plan now has no resource
  actions.
- [x] `setup_tool.py` checks the live endpoint count: `session.sh` switches
  endpoints with a targeted apply, which leaves the `endpoints_enabled`
  Terraform output stale

### Open

Apply Terraform from `main` only: the state is shared, and a plan from a
branch without these resources would propose deleting them.

- [x] `terraform apply -var instance_type=t4g.xlarge` from `main`
  (endpoints off): 17 added, 0 changed, 0 destroyed. Runtime SG
  `sg-0eea9be828da21be4` (no inbound; out 443 VPC/S3, 5432 to EC2); EC2 SG's
  only inbound rule is 5432 from it; ECR
  `<account-id>.dkr.ecr.eu-west-2.amazonaws.com/agentcore-kb-mcp`; roles
  `agentcore-kb-{runtime,gateway,harness}`. Inputs:
  `terraform output agentcore_kb`
- [x] Read-only database user `kb_reader`: SELECT on `rag_chunks` only,
  read-only transactions, 10 s statement timeout, 10 connections; verified
  denied on writes, other tables and CREATE. Password only in SSM
  `/rag-systems/prod/agentcore-kb-pg-password` (EC2 got the SCRAM hash)
- [x] MCP server reads the password from the SSM SecureString named by
  `PGPASSWORD_PARAMETER` (or `PGPASSWORD` locally); tested with the
  environment, a stubbed SSM response, a missing parameter (fails fast) and
  the real parameter (decrypts)
- [x] pgvector published on the host's private address only
  (`10.77.1.240:5432`, from IMDSv2 in `stack.sh`); deployed as `3a1acac`.
  `127.0.0.1:5432` is Langfuse's own Postgres (loopback, unchanged)
- [x] MCP image pushed to ECR (`agentcore-kb-mcp`, tag = commit); the
  first image's scan found HIGH CVEs in Debian base packages, so the
  Dockerfile now applies Debian security updates at build time
- [x] Patched image `agentcore-kb-mcp:ff20141d9429`: scan 2 HIGH / 1 MEDIUM
  / 1 LOW (was 7/6/5); the remaining HIGH (zlib, gcc runtime) have no Debian
  fix yet
- [x] Interface endpoints on (Terraform: 5 added); private DNS verified from
  EC2 (all five resolve to 10.77.1.x); the RAG app's Titan calls still work
- [x] `scripts/aws/session.sh start [--agent] | stop | status`: one command
  turns the endpoints, the stack and EC2 off together; targeted plans
  checked (endpoints on: no changes; off: destroys exactly the 5 endpoints)
- [x] `agent/setup_tool.py`: Runtime `agentcore_kb_mcp-EkwYuhB6iB` (MCP,
  VPC mode, image `ff20141d9429`), Gateway
  `agentcore-kb-gateway-suzkm6t3y2` (AWS_IAM) and target `knowledge-base`;
  a SigV4 call to the gateway listed `knowledge-base___search_knowledge_base`
  and returned EC2 results (0.732 for the Austria question)
- [x] Harness `<harness-id>` (Claude Haiku 4.5) via `deploy.py`.
  It also provisions AgentCore Memory `agentcore_kb-AmadHaFyfr` for session
  history; the harness role needed memory, workload identity, ECR Public and
  log permissions (first call failed with AccessDenied on ListEvents)
- [x] End-to-end: `invoke.py "How do I travel by train in Austria?"` searched
  the knowledge base and answered with [1]–[5] citations (2,683 tokens in,
  411 out)
- [x] First real `scripts/aws/session.sh stop` (2026-10-01): endpoints 0,
  stack stopped cleanly, EC2 stopped
- [x] Web UI: Retrieval page has a RAG pipeline / AgentCore agent switch
  (`web/static/js/agent.js`, `app/agent.py`, `/api/agent`,
  `/api/agent/stream`). It shows a live workflow chart (agent ⇄ tools) and
  per-question tool options (allowed tools, max turns). Tested locally against
  the real harness: tool off, and tool on with endpoints off (fails after 40 s
  with a hint)
- [x] `terraform apply` from `main` (2026-10-01): `aws_iam_role_policy.app_agent`
  on the EC2 role (Get/InvokeHarness, Get/InvokeGateway); 1 added, 0 changed,
  0 destroyed
- [x] Published and deployed `8bfadab` (2026-10-01). InvokeHarness also
  needs `bedrock-agentcore:InvokeAgentRuntime` on the harness, added to the
  EC2 role policy (1 changed). Verified on EC2: the agent called
  search_knowledge_base (5 results, 0.742 top similarity, 6.2 s) and answered
  in 2 turns (13.2 s, 3,861 tokens in, 460 out)
- [x] Retrieval page is agentic RAG: the harness plans the searches; its
  tool is the app's own hybrid retrieval, run inline by the web app, so each
  search keeps the full retrieval metrics and there is one answer (replaces
  the side-by-side view). Verified locally: "Compare train travel in Austria
  and Belgium" → 2 searches, answer in 2 turns (12.9 s); a follow-up was
  answered from session memory
- [x] Multi-agent: supervisor + knowledge-base agent + entry-requirements
  agent (canienter.com free API, 5 checks/day per IP, 24 h cache, CC BY-NC
  4.0), all on the one harness via per-call prompt and tool overrides.
  Verified locally: Indian passport → Austria + trains → both specialists in
  parallel, 5 searches and 1 live check, one combined answer (33.7 s). Same
  role in parallel shares a runtime session and fails ("no pending
  handoff"), so tasks for one specialist queue
- [x] LangGraph orchestration: supervisor → Send fan-out → knowledge-base
  agent / external-APIs agent (generic, tools from `app/api_tools/`:
  canienter.com entry requirements, Open-Meteo weather) → join → supervisor,
  at most 2 rounds. Verified locally: visa + weather + trains → 3 parallel
  tasks, 1 round, one answer (30.9 s); weather + trains → 2 parallel tasks
- [x] Deployed `60bebb6` (2026-10-01). The first attempt (`d863e2e`) failed
  and rolled back: `stack.sh` ran `compose up` without `--build`, so the old
  image lacked langgraph; it now rebuilds (cached when unchanged). Verified on
  EC2: visa + weather + trains → 1 round, canienter and Open-Meteo calls, 5
  searches (33.8 s); one extra Open-Meteo call got HTTP 503 (overloaded) and
  the agent reported it
- [ ] Optional: retry Open-Meteo once on 503/429
- [x] Swiss public transport tools (transport.opendata.ch): swiss_connections
  and swiss_departures. Agents now get today's date in their prompt (without
  it, "tomorrow" made the API agent ask for the date). Verified locally:
  Zurich → Interlaken tomorrow 9am + weather → 09:02 IC 81 direct and an
  Interlaken forecast (29.7 s); Luzern departures render in the UI
- [x] Deployed `644af5d` (2026-10-01); on EC2 the same Zurich → Interlaken
  question called swiss_connections and get_weather and answered
- [x] Evidence Evaluator after the knowledge-base retrieval (LangGraph
  subgraph; hybrid: deterministic checks, Claude structured Pydantic output,
  deterministic routing). Only GOOD_EVIDENCE reaches the answer; the other
  four decisions go to placeholders. 11 unit tests (app/test_evidence.py, in
  CI). Live: Austria tickets → GOOD_EVIDENCE 0.84; Portugal → KNOWLEDGE_GAP
  → research placeholder. Fixed during testing: Haiku sometimes leaks
  tool-call markup into a field (now retried), and 3+ failed rephrased
  searches count as a knowledge gap, not a retrieval failure
- [x] Deployed `7d3f598` (2026-10-02); on EC2 Austria tickets → GOOD_EVIDENCE
  0.85 → answer, Portugal → KNOWLEDGE_GAP 0.48 → research placeholder
- [x] RETRIEVAL_FAILURE now retries once: query_rewriter (Claude, Pydantic
  QueryRewrite) diagnoses the missed searches and rewrites the queries, the
  retrieval agent searches again, and a second miss is a KNOWLEDGE_GAP
  (replaces the "3+ failed searches" rule). Chart rearranged in run order
  with the evaluator's score after the specialists and the answer agent
  last. Verified locally: Portugal → RETRIEVAL_FAILURE 0.49 → rewrite →
  KNOWLEDGE_GAP 0.50 → research placeholder; Austria → GOOD_EVIDENCE 0.80
- [x] Deployed `b694ebf` (2026-10-02, EC2 started without the agentcore-kb
  endpoints, which the web UI no longer needs). On EC2: Austria → GOOD_EVIDENCE
  0.85 → answer; Portugal → RETRIEVAL_FAILURE 0.49 → one rewrite → KNOWLEDGE_GAP
  0.49 → research placeholder
- [x] Research agent for knowledge gaps: Tavily web search, HTML/PDF
  extraction, source validation (authority, freshness, consistency,
  relevance), a LangGraph interrupt for the user's approval, ingestion through
  the existing pipeline, then retrieval again. 10 tests (app/test_research.py)
- [x] Tavily key stored by hand in SSM `/rag-systems/prod/tavily-api-key`;
  `terraform apply` added `aws_iam_role_policy.app_research` (1 added).
  Verified locally: Portugal → rewrite → gap → research (4 pages extracted,
  3 submitted) → 2 of 3 passed (official cp.pt pages; a blog rejected on
  freshness) → paused for approval → Reject → nothing ingested, answered.
  Fixed during testing: the research agent ran out of turns without
  submitting (now a search budget, and extracted pages become candidates),
  and a turn-limit stop left its AgentCore session waiting for a tool
  result (now answered, and stuck sessions are replaced)
- [x] Deployed `6bc804f` (2026-10-02). On EC2: Tavily key read through the
  instance role; Portugal → rewrite → gap → research → 1 of 3 passed (cp.pt)
  → paused → rejected via the API → "Ingestion declined by the user"
- [x] Answer evaluator after the summarizer: correctness, faithfulness,
  completeness, citation quality (checks + Claude, Pydantic); pass → the
  answer, fail → a standard message. 13 tests (app/test_answer_eval.py).
  Live: Austria tickets → PASS 0.85–0.91; Portugal after a rejected
  ingestion → FAIL on completeness → standard message. Tuning found during
  testing: the model listed 4 unsupported claims yet scored faithfulness
  0.7, so each named issue now costs 0.1; the summarizer prompt now forbids
  outside facts and moving citations
- [x] Deployed `9acfe81` (2026-10-02). The first deploy (`90531c7`) failed
  closed on EC2: a model reply did not validate, so every answer became the
  standard message; output shapes are now normalised and retried once. On
  EC2: Austria tickets → answer evaluation PASS 0.875 → answer sent
- [x] "are there any toilets in newark" failed: the supervisor asked which
  Newark without delegating, and the answer evaluator failed the clarifying
  question on completeness (standard message). Now the supervisor sends every
  factual question to a specialist, and a grounded clarification or honest
  "not available" skips the completeness check. Verified locally: research
  found Newark Northgate's official facilities page (passed validation);
  after a rejected ingestion the clarification passed and reached the user
- [x] Brief final answers: the summarizer has a hard 120-word limit, no
  headings (live: 60–103 words, all passing). Found while testing: the answer
  evaluator dropped cited chunks beyond its 40-chunk cap and called them
  missing (now every cited chunk is checked and shown); and the supervisor
  sent Austrian train questions to the Swiss-only transport tools (its
  prompt now scopes them to Switzerland)
- [x] Fixes found deploying `2950ff5`: the evidence evaluator also got list
  fields as JSON strings (shared clean-up in app/structured.py for all three
  judges); the supervisor still answered "newark" without delegating (it is
  now sent back once in code); a greeting failed the answer evaluator
  (new "conversational" type, allowed only with no evidence); and AgentCore
  Memory leaked facts between browser sessions because they all ran as one
  actor (each session is now its own actorId)
- [x] Agent memory on the Retrieval page (🧠 Agent memory: each agent's
  short-term events, long-term facts and summaries for this browser session;
  read-only IAM on the memory, applied: 1 changed). Deleted the shared
  `default` actor's 174 long-term records (24 facts, 150 summaries) that had
  pooled everyone's conversations; its events expire on their own (30 days)
- [x] Session & microVMs card: session ID, memory actor, each agent's
  runtime session and estimated microVM status (no AgentCore API reports it;
  estimated from the app's calls and the runtime's 15 min idle / 8 h limits)
- [ ] Investigation and clarification agents
  (placeholders today); the supervisor still adds generic advice from its own
  knowledge when the knowledge base has a gap
- [ ] After each session: `scripts/aws/session.sh stop` (endpoints are
  about $0.055 per hour while on)
