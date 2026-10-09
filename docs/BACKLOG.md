# Backlog

Work not started yet, roughly in the order to pick it up. Ask for an item
by name. When one starts, move it into a plan (`docs/plans/`) or
PROGRESS.md; when it is done, delete it here. Details and history for most
items are in [PROGRESS.md](../PROGRESS.md).

Last updated: 2026-10-08

## Finish the identity work

- [ ] **Langfuse session ID check** (the user will do it) — confirm a
  trace for a signed-in question carries the app's session ID (left over
  from Phase 3). Easiest in the Langfuse UI: tunnel `3000` → `23000`, then
  Traces → newest `question` trace → Session filled and User is the user;
  the Sessions tab groups a chat's traces. The agent cannot read the EC2
  databases itself: auto mode blocks it as a production read even with an
  allow rule for `aws ssm send-command`. If doing it from the databases:
  `app_session_turns` (columns `session_id`, `user_id`, `trace_id`,
  `asked_at`) is not in the `rag` database of the `pgvector` container,
  so find the webapp's `PGDATABASE` first; ClickHouse queries go through
  stdin to avoid shell quoting
- [ ] **Close out Phase 7** — mark it done in
  [the plan](plans/identity-sessions-memory.md), update PROGRESS.md, commit

## Security and operations

- [ ] **Rotate the Langfuse secrets** on EC2 (they match the local
  development `.env`)
- [ ] **Backups** — EBS snapshots, a restore test, backup frequency
- [ ] **Public access** (only if the app should work without the tunnel).
  Authentication is done; still needed: domain and HTTPS, inbound path,
  public IP decision, alarms and logs, Terraform plan and security review
- [ ] **Protect `main`** — GitHub allows it on a private repo only with
  GitHub Pro (or a public repo); declined for now (2026-10-07), so PRs are
  a habit, not enforced. When available: require a PR, require the
  `validate`, `pgvector-tests` and `private-compose` checks, block force
  pushes and deletion, include admins
- [ ] Optional: GitHub Actions deploy (publishing is already automatic
  after CI); longer SSM idle timeout
  (tunnels drop after 20 minutes idle)

## Bugs and gaps

- [ ] **Token usage undercounts on EC2** — the AgentCore harness reports
  usage only on an agent's final (end_turn) invocation, not on turns that
  stop for a tool call, so flow metrics' token totals are low. Inspect the
  raw stream for where tool-call turns' usage goes
- [ ] **Workflow graph after a reload** cannot show which evidence branch
  was taken (the saved public result has no decision)
- [ ] Optional: retry Open-Meteo once on 503/429

## Research: data sources (follow-ups to issue #10)

Plan and live-run history:
[plans/research-source-discovery.md](plans/research-source-discovery.md).

- [ ] **Create a tool stub from a report** — an admin action on a data
  source report that writes a draft `app/api_tools` module (an `ApiTool`
  like `weather.py`) from the recommended source's integration sketch,
  for review in a pull request; nothing registered automatically
- [ ] **Prefer an on-demand API for lookups** — the judge recommended
  Darwin's push feed (`feed_consumer`) over its SOAP departure-board API
  for "live UK departures" (live run 5); the issue's ideal is an
  `api_tool` for "next trains from X". Nudge the judge's prompt: for
  on-demand lookups prefer a request/response API; a push feed only when
  history or analytics are needed. Check with `test_research_live` on EC2
- [ ] **Read a website's terms page** — website profiles keep their terms
  as "unknown" because the agent submits one page for them. `fetch_source`
  now returns a page's links (#21): follow a "terms" link for web-page
  candidates
- [ ] **Live data sources in setup** — setup's Sources step (#20) lists
  live knowledge areas (departures, flight status) as "answered by live
  tools" and finds no sources for them. Run data-source research (#10) for
  those areas during setup, show the recommended API or feed next to the
  chosen sites, and record which live tool each area needs
- [ ] **Tell users the recommended source** (option b in the plan) — today
  a live-data question answers "not available here"; naming the official
  source and its link needs the answer evaluator to accept research
  findings as evidence

## Content and UI

- [ ] **Station facilities** — answers about UK station facilities (for
  example London Victoria's toilets) say the knowledge base has nothing; St
  Pancras is covered. Ingest National Rail facility pages for the main
  stations
- [ ] **Sources list** on the Retrieval page shows only the page path; add
  the site's domain
- [ ] **Screenshots** — retake `docs/images/multi-agent-answer.png` and
  `docs/images/flow-builder.png` (both show the old layouts)
- [ ] **Investigation and clarification agents** are placeholders; the
  supervisor still adds generic advice from its own knowledge when the
  knowledge base has a gap
