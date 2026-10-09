# Plan: ingestion plan and build (Build RAG)

Status: **Done — merged in PR #35 (`642a486`) and deployed to EC2 on
2026-10-09.**
The local install is at `EVALUATING`: plan 4 (National Rail: Railcards,
refreshed every 30 days, and Help and assistance; 17 pages) built, 49
chunks. Branch `init/ingestion-plan`. Update this file at the end of every
step: tick what is done, note what was found, say what
comes next.

GitHub: issue #22, part of epic #27 (RAG Initialization Agent); builds on
#21 (setup is then at `AWAITING_CONTENT_SELECTION` with sections and pages
chosen). Working agreement: phases with a stop at each; a branch and a pull
request; the user merges (AGENTS.md).

## Goal

The user's chosen content becomes a saved, versioned **ingestion plan**
that they review and approve with **Build RAG**. Only then is anything
fetched for ingestion, through the pipeline we already have. Afterwards
every chunk says **why it is in the knowledge base**: which plan version
and entry, which section, which source, who approved it and when.

## How the code is today

- **Pipeline** (`dags/common/ingest.py`, `ingest_url(client, entry)`):
  fetch, extract, dedup, chunk, embed, store, with a TTL per entry;
  `entry["metadata"]` is already copied into every chunk's payload (the
  research agent uses it). Skips fresh pages; refreshes the TTL of unchanged
  ones. **No `robots.txt` check.**
- **Worker** (`app/ingestion_worker.py`): one job at a time from
  `ingestion_jobs` (`ingest_urls` | `prune_expired_documents`), a
  checkpoint after every URL (`next_index`), resumes after a restart; the
  job's summary counts statuses and keeps the failures. One active job at a
  time (a unique index).
- **The cap**: `enqueue_job` refuses more than `MAX_URLS_PER_JOB` (10)
  entries; the Ingestion tab's job takes the first 10 of `kb_urls`.
- **Chosen content** (#21): `kb_content` sections with `status = selected`,
  their pages and `excluded_urls`, their areas, `kb_sources` for the sites.
- **Removing pages** from the knowledge base exists on the Sources page
  (`/api/sources/remove`, by source URL).
- The states `INGESTION_APPROVED → INGESTING → INDEXING → EVALUATING`
  exist; the setup page's step 5 is "Build and evaluate".

## Design

### 1. The plan (Ingestion Planner role) — rules in code, no model call

Built from the chosen content when the user opens the review:

```
kb_ingestion_plans       version, created_at, status (draft | approved | superseded),
                         page_limit, approved_by, approved_at, totals jsonb
kb_ingestion_plan_pages  version, position, url, source_id, content_id, section key,
                         areas[], kind (html | pdf), ttl_days,
                         status (pending | ingested | unchanged | skipped | failed | blocked),
                         chunks, error, ingested_at
```

- **Pages**: every page of every selected section, less its excluded pages;
  each URL once (the first section that lists it), the plan's order by
  site and then section.
- **TTL per section**: from the section's dates and kind, not a guess:
  a section whose newest `lastmod` is within 30 days, or a "live"-like
  path, changes often → 14 days; PDFs and sections unchanged for a year →
  180 days; the rest → the default 90 days. Shown on the review, one per
  section.
- **Page limit**: a plan holds at most `PLAN_PAGE_LIMIT` pages (default
  500, configurable); above it the review says so and Build RAG is refused
  until the selection is smaller.
- **Versions**: each review of a changed selection makes a new draft
  version; an unchanged selection reuses the draft. Approving supersedes
  the earlier approved version.

### 2. Review and approval

- The review (setup step 5, "Build"): per site and section — pages, PDFs,
  TTL, the areas covered — totals (pages, sites, sections; a rough time:
  pages × the polite delay), areas with no chosen content, and the
  differences from the last approved plan (pages added and removed).
- **Build RAG** records the approval (user, time, plan version) in the plan
  and in the audit log, moves `AWAITING_CONTENT_SELECTION →
  INGESTION_APPROVED → INGESTING`, and queues one job.
- **Nothing is fetched for ingestion before approval** (test).

### 3. Running it

- A new job kind `ingest_plan` with `{"plan_version": n}`: the worker
  reads the plan's pending pages itself (no 10-URL cap; the plan's own
  limit applies), so entries are not copied into the job.
- Per page, before fetching, **`robots.txt` is checked again** (cached
  per host for the job); a disallowed page is `blocked`, not fetched.
- Each page's result is written to `kb_ingestion_plan_pages` as it
  finishes (status, chunks, error) — that is the resumable checkpoint: a
  restart carries on with the pending pages.
- When no page is pending: `INGESTING → INDEXING`, a short indexing step
  (`ANALYZE` the chunk table, check every ingested page has chunks and
  vectors of the configured dimension), then `INDEXING → EVALUATING`. The
  evaluation itself is #24; until then the page says the build is done and
  evaluation follows.
- The setup page polls the plan: pages done of total, by status, the
  failures with their reason, **Retry failed pages**.

### 4. Provenance on every chunk

`entry["metadata"]` (already copied to every chunk) carries
`{"plan_version", "plan_page", "source_id", "content_id", "section",
"areas", "approved_by", "approved_at", "origin": "setup"}`.

- **Why is this here?** `GET /api/knowledge/provenance?url=…` returns the
  plan entry, the section and its reason, the source and its reason, and
  the approval. Shown from the Sources page (per source URL) and from a
  retrieved chunk on the Retrieval page.
- Test: every chunk of a built knowledge base traces to an approved plan
  entry.

### 5. Changing it later

Changing the selection after a build (back to Content) makes a new plan
version; its review shows pages added and removed, and approving it
ingests the added pages and **removes the chunks of removed pages** (the
review says how many), through the existing remove code.

### 6. API and tracing

- `GET /api/setup/plan` (the draft or current plan, with totals and
  differences), `GET /api/setup/plan/pages?status=…`, `POST
  /api/setup/plan/approve` `{"version"}`, `POST /api/setup/plan/retry`,
  `POST /api/setup/back-to-content`, `GET /api/knowledge/provenance`.
- Langfuse: `ingestion_plan` (the build) with a span per site.

## Phases

- [x] **1. Plan** — this file.
- [x] **2. The plan**: tables, building it from the content (TTL rules,
  limit, versions, differences), approval and the states, tests (nothing
  fetched before approval).
  - `app/initialization/plan.py`: `kb_ingestion_plans` (version from a
    sequence, never reused, so a review of a dropped draft cannot pass for
    the current one; draft | approved | superseded; fingerprint; page
    limit; totals; approved_by/at) and `kb_ingestion_plan_pages` (per page:
    source, section, areas, kind, TTL, status pending, chunks, error);
    `kb_content.ttl_days` for the user's TTL. `planned_pages` (selected
    sections less unticked pages, each URL once), `suggested_ttl`,
    `set_ttl` (1–365 days, or back to the suggestion), `draft` (reuses the
    draft for an unchanged selection), `differences` (added, removed, and
    the removed pages' stored chunks), `review` (by section, totals with a
    time estimate, areas with no content, a problem: nothing chosen or over
    the limit), `approve` (only the version reviewed, if still current;
    supersedes the earlier approved plan; `AWAITING_CONTENT_SELECTION →
    INGESTION_APPROVED` with the version, page count and differences in
    the audit log), `back_to_content`, `view` (the approved plan's
    progress). A reset clears both tables.
  - API (`manage_settings`): `GET /api/setup/plan`, `POST
    /api/setup/plan/approve` `{"version"}`, `POST
    /api/setup/content/{id}/ttl` `{"ttl_days"}`, `POST
    /api/setup/back-to-content`. `GET /api/setup` gains `build`.
  - Found on the local selection: Help and assistance got 14 days because
    its newest page changed 10 days ago, the rest months before. The rule
    now looks at the share of a section's dated pages changed within 30
    days (half or more: often changing); it gets 90.
  - Tests: `IngestionPlan` in `test_setup.py` (the chosen pages, versions,
    TTL rules and the user's TTL, approval and its refusals, nothing
    fetched before approval, differences with chunk counts, the API, a
    reset); the routes in `test_auth.py`. Full suite 357 OK.
- [x] **3. Running it**: the `ingest_plan` job, robots check, per-page
  status, resuming, retry, indexing step, provenance on chunks and the
  provenance endpoint, tests (more than 10 pages; every chunk traces back)
  and a live build of the local selection.
  - `app/initialization/build.py`: `build_rag` (approve, then `start`:
    queue one `ingest_plan` job, `INGESTION_APPROVED → INGESTING`; if
    another job is active the plan stays approved and **start** can be
    called again), `run` (the worker's part: the previous plan's dropped
    pages' chunks removed; each pending page checked against `robots.txt`
    — `Robots`, one read per host — and ingested through
    `common.ingest.ingest_url` with its provenance; its result written to
    the plan page as it finishes, the checkpoint; a page still fresh is not
    fetched but takes the new provenance, `storage.merge_payload`; then
    `INGESTING → INDEXING`, `_check_index` (`ANALYZE`, chunks per page
    read), `INDEXING → EVALUATING`; if nothing could be read, setup stays
    at `INGESTING` with a `build_failed` event), `retry` (failed pages back
    to pending and a new job; also carries on a build whose job stopped,
    at `INGESTING` or `INDEXING`), `provenance` (a stored page → plan
    version and approval, plan entry, section and its reason, source), `view`
    (progress, failures, the job, the check).
  - Storage: job kind `ingest_plan` (a named check constraint, replaced on
    start so existing tables take the new kind; no URL cap — the plan has
    its own limit); `merge_payload`; the worker runs `build.run`.
    `knowledge_system.record_event`.
  - API: `POST /api/setup/plan/approve` is now Build RAG; `POST
    /api/setup/build/start` `{"version"}`, `POST /api/setup/build/retry`
    (`manage_settings`); `GET /api/knowledge/provenance?url=`
    (`view_knowledge`).
  - Live, locally: Build RAG on plan 2 (12 pages); the worker read all 12
    (35 chunks) but failed recording the check: `kb_ingestion_plans` had
    been created in phase 2, before its `build` column, and `CREATE TABLE
    IF NOT EXISTS` keeps an older table. Fixed: the column is added to an
    existing table; and a build stuck at `INDEXING` had no way on, so
    **retry** now carries it on. Carried on: 3 s, `EVALUATING`, 12 of 12
    pages with chunks; "why is this here?" for the compensation page gives
    plan 2 and its approval, the section and its reason, National Rail
    (authority high), 4 chunks, TTL 90.
  - Tests: `Build` in `test_setup.py` (a 258-page plan through the worker
    with a robots-disallowed page; every chunk traces back to an approved
    plan entry; a stopped build carries on without reading a page twice;
    a build where nothing could be read waits for a retry; a changed plan
    removes dropped pages and gives the rest the new provenance without
    fetching them; why is this here; one job at a time and the API; a
    build stopped at indexing carries on; an older plan table is brought up
    to date). Full suite 366 OK.
- [x] **4. The Build step on the setup page** (review, Build RAG,
  progress) and "Why is this here?" on the Sources and Retrieval pages,
  checked in the browser.
  - `web/static/js/setup.js`: the Content step's **Review the plan** opens
    the Ingestion plan card: version, pages, sections, sites, PDFs and the
    time it takes; what is wrong when it cannot be built; the differences
    from the plan built before (pages added, removed, and the removed
    pages' chunks); areas no chosen content covers; per section the site,
    pages, **Refresh every N days** (editable; "suggested N" when
    changed; back to the suggestion when set to it) and the areas;
    **Build RAG** and **Back to the content**. From `INGESTION_APPROVED`
    on, the Build card: what is happening, a progress bar, pages by status
    and chunks written, the plan version and approval, the index check,
    pages not read with why, **Start the build** (approved but not
    queued), **Retry failed pages** or **Carry on** (a stopped build),
    **Change the content**; polled while the job is queued or running.
    The content, sources, blueprint and conversation fold away below.
  - `web/static/js/why.js`: **Why?** — source and authority, section and
    its reason, plan version, who approved it and when, when it was read,
    chunks, refresh. On every page of the Sources page (curated
    knowledge) and every cited web page under a Retrieval answer.
    `build.provenance` gains the approver's name.
  - Browser, locally: the Build card for plan 2; **Change the content**,
    Railcards ticked, **Review the plan**: version 3, 17 pages, "5 pages
    added, 0 removed" against version 2, three areas uncovered; Railcards
    set to 30 days made version 4 ("suggested 90"); **Build RAG**: the bar
    moved as pages were read (polled), 5 read and 12 unchanged (not
    fetched) in about 40 s, index check 17 pages with 49 chunks. Sources
    page: **Why?** on the Railcards page gave National Rail (high), the
    section and reason, plan 4 approved by the local admin, 4 chunks,
    every 30 days; its TTL column 30. No console errors. Found: the
    chunk count beside the progress counts this build's chunks only
    (14, against 49 stored) — now "chunks written". The Retrieval page
    does not answer until setup is live (#17), so **Why?** under an answer
    is checked when #25 goes live. A reason stored before #21's fix still
    shows an area key until its site is analysed again.
- [x] **5. Docs, PR.** README: guided setup gains the Build step; the
  scraper section says a setup build checks `robots.txt` page by page and
  the `ingest_urls` job does not; the Ingestion jobs say a setup-built
  install builds from its plan; Langfuse gains `ingestion_plan`; the layout
  gains `plan.py` and `build.py`.
  - **For #24**: setup waits at `EVALUATING` after a build; the evaluation
    set and run start from there. **For #25**: check **Why?** under a
    Retrieval answer once live.

## Open questions for the user

1. **Page limit per plan**: 500 pages by default (recommended: with the
   polite delay about 15–25 minutes to build), configurable. Higher or
   lower?
2. **TTL from rules** (14 days for often-changing sections, 180 for PDFs
   and long-unchanged ones, 90 otherwise), shown on the review and
   editable per section? (Recommended: rules, editable.) Or one TTL for
   everything?
3. **The old Ingestion tab job** (first 10 of `kb_urls`): keep it as it is
   for installs set up before guided setup (recommended; EC2 uses it), and
   for a setup-built install show the plan there instead?
4. **After a change**, remove the chunks of pages no longer in the plan
   when the new plan is approved (recommended, with the count shown first),
   or keep them until they expire?
