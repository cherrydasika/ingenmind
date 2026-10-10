# Progress

Running log of what is done and what is open. Update it after every step;
read it first when picking work back up. Decisions and the longer-term
plan live in [AWS_MIGRATION.md](AWS_MIGRATION.md).

Last updated: 2026-10-10

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
(learning from live use, after the MVP). **No active work:** the next item
is the user's choice from the backlog.

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

- EC2 `<instance-id>` (eu-west-2) is **stopped** (2026-10-10, with
  `session.sh stop`: the stack first, then EC2; no AgentCore endpoints were
  on, and no other instance runs in any region). Start it with
  `session.sh start`. Its active release is
  `70d15d4` (PR #56, #15 knowledge metadata, deployed by GitHub Actions on
  2026-10-10; #16, #17, #25, the two research fixes and the rebrand before
  it; setup there is READY/existing with its 5 URLs and no confirmed
  blueprint, so no page is labelled and the setup steps don't show until
  #39; the live flow is unchanged).
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
