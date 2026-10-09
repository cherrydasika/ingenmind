# Plan: knowledge metadata from the blueprint, and retrieval that uses it

Status: **Paused in phase 3 (2026-10-09), at the user's request.** The
code for filters and authority is written and tested (407 pass) and pushed
to branch `init/metadata` (draft PR #56), not merged. The comparison found
the agent over-uses the topic filter, which hides pages filed under a
neighbouring topic. The fix (topic as a preference, not an exclusion, and a
re-run of the comparison twice per side) is in the backlog as **#57**.
Resume there. Update this file at the end of every step.

GitHub: issue #15, part of epic #19 (RAG Initialization Agent); builds on
#11 (the blueprint and its `metadata_fields`) and #14 (the build: each page's
source, section and areas). Related: #2 (reranker), #3 (chunking along
headings), #1 (measuring search quality): this builds the labels they can
use, and repeats none of them. Working agreement: phases with a stop at
each; a branch and a pull request; the user merges (AGENTS.md). Nothing goes
into the knowledge base without the user's approval (their rule).

## Goal

Every chunk says **what it is about**, from the blueprint's own vocabulary,
and retrieval can **use** it: narrow a search to a topic, organisation or
content type, and prefer authoritative sources when results are otherwise
equal. With no filters, search behaves exactly as today.

## How the code is today

- **A chunk's payload** (`rag_chunks.payload`): `source_url`, `text`,
  `chunk_index`, `ingested_at`, `expires_at`, `ttl_days`, `content_hash`,
  dedup counts, and for setup's pages the provenance from #14: `origin`,
  `plan_version`, `plan_position`, `source_id`, `source` (host),
  `content_id`, `section`, `areas`, `approved_by`, `approved_at`. Research
  pages (`research_chunks`) carry `origin: research_agent`, the task, the
  publisher, a date and the validator's scores, but no areas.
- **One version per page:** a changed page replaces its old chunks
  (`ingest_url`, `replace_existing`), so "keep the newest version" already
  holds; what is missing is the page's own date.
- **The blueprint** has `metadata_fields` (locally: `ticket_type`,
  `passenger_category`, `station_type`, `facility_type`…, each with a
  description and examples), `organisations` (name, role, website),
  `knowledge_areas`, `entities`, `regions`.
- **Sources** have `authority` (high, medium, low) from discovery;
  **sections** have `areas` and `flags` (news, live, terms…).
- **Search** (`retrieval.hybrid_search`): dense (pgvector, HNSW) and
  full-text (`tsvector`) over both tables, fused by reciprocal rank; no
  filters. The knowledge-base agent's tool `search_knowledge_base` takes
  only `query`.
- **Extraction** already reads a page's title, date and site name
  (`scraping.extract_metadata`), used by research only.

## Design

### 1. The labels (payload field `meta`)

| Field | From | How |
|---|---|---|
| `topic` | the section's knowledge areas | deterministic (setup's `areas`) |
| `organisation` | the source's host matched to the blueprint's organisations (by website) | deterministic; else the source's name |
| `source_type` | the organisation's role (regulator, government, operator…) | deterministic |
| `authority` | the source's authority from discovery: high 1.0, medium 0.6, low 0.3 | deterministic |
| `country` / `region` | the blueprint's regions (one region: that one) | deterministic |
| `effective_date` | the page's own date (published or modified), when it states one | deterministic (`extract_metadata`) |
| `retrieved_at` | when the page was read | deterministic |
| `content_type` | policy, guide, FAQ, form, news, timetable, contact | the section's flags when they decide it; else the page labeller |
| the blueprint's own fields (`ticket_type`, `passenger_category`…) | the page's text | **one forced-tool call per page** (the page labeller), values only from each field's examples or `none`; checked in code |

Per page, not per chunk: a page's chunks share its labels (cost: one small
call per page; the local plan has 17 pages, a plan may have up to 500).

### 2. Retrieval that uses them

- `hybrid_search(..., filters=None)`: optional `topic`, `organisation`,
  `content_type` and any blueprint field, as conditions on `payload->'meta'`
  in both searches (a GIN index on `payload->'meta'`; pgvector's iterative
  index scan so a filtered vector search still returns `top_k`).
- **Filters off: results identical to today** (the acceptance test).
- **The agent may filter:** `search_knowledge_base` gains optional
  `topic`, `organisation` and `content_type` (the allowed values listed in
  the tool description, from the blueprint). A filtered search that finds
  nothing runs again unfiltered, and says so.
- **Authority as a tie-breaker** in fusion: question 3.
- Citations show the organisation and the page's date, so users see how
  current a source is.

### 3. Existing pages

A **relabel** step labels the pages already stored from their stored text
(no fetching), for the current plan, on request from the setup page; new
builds label as they ingest. Research pages: question 4.

## Phases

- [x] **1. Plan** — this file. Stop for the user's answers.
- [x] **2. Labels at ingestion**: the deterministic fields, the page
  labeller with its checks, the relabel step; tests (the built knowledge
  base carries the blueprint's fields); a live relabel of the local build.
  Stop.
  - `app/initialization/labels.py`: `deterministic()` (topic, organisation
    and source type from the blueprint's organisations by the page's or its
    source's host, authority, region, effective date, retrieved at, content
    type from section flags), the page labeller (one forced-tool call per
    page; the schema is built from the blueprint: content type, each
    metadata field's examples as an enum, the knowledge areas), `checked()`
    (only allowed values, in their canonical spelling), `label_url()`
    (labels a **stored** page from its payload and stored text and merges
    `meta` onto all its chunks: one path for builds, unchanged pages,
    research pages and relabelling), `relabel()` (the approved plan's pages
    in the background; table `kb_labelling_runs`, emptied by a reset) and
    `summary()`. Ingestion now stores the page's own date (`page_date`, HTML
    only). The build labels each page it reads (`build.run`), and research
    labels the pages it adds (`agent._ingest_sources`), when the install has
    a confirmed blueprint. A failed labeller keeps the rules' labels and
    never stops a build or research. API: `POST /api/setup/relabel`; the
    Build view has `labels` (the latest run and the summary).
  - Changed from the design: **topics are the page's own**, from the
    labeller, always, with the section's areas only as the fallback. The
    first live relabel gave every page of National Rail's "Help and
    assistance" the section's three areas (refunds, delay compensation,
    passenger rights), so "Advice for autistic passengers" was not
    accessibility. Now it is. The organisation is matched by the page's
    host, else by its source's host.
  - Tests: 6 in `test_setup.Labels` (a built page carries the blueprint's
    labels, on every chunk; a failed labeller keeps the rules; the rules;
    a research page gets its topic from the labeller; relabel from stored
    text, one run at a time; the API). The build fixture stubs the
    labeller. Full suite: 402 pass.
  - Live on the local build (17 pages): relabelled in about 24 s, all 17 by
    the model. Content types: guide 14, policy 2, contact 1. Fields:
    passenger category (disabled 6, young person 4, adult 3, elderly 3,
    child 2), ticket type (season 5, single 3, advance 2, flexible 2,
    return 2), accessibility feature, facility type. The pages' dates are
    missing because they were stored before ingestion recorded them; new
    builds have them.
- [ ] **3. Retrieval with filters and authority**: `hybrid_search` filters,
  the agent tool's optional filters with the unfiltered retry, authority in
  fusion, dates in citations; tests (a filtered search returns only matching
  chunks; filters off: unchanged); `travel_basics` and the setup evaluation
  run before and after, scores not lower. Stop.
  - `storage.meta_filter()`: all given keys, any value of each, as JSON
    containment on `payload->'meta'` (keys `topic`, `organisation`,
    `content_type`, `fields.<name>`; anything else is refused). A GIN index
    (`jsonb_path_ops`) on the labels in both tables. A filtered vector
    search sets `hnsw.iterative_scan = relaxed_order` (pgvector 0.8.6, local
    and EC2) so it still fills its limit. `search_dense` / `search_text` /
    `hybrid_search` take `filters`; none or `{}` gives exactly the old
    results.
  - Fusion: equal fused scores order by authority, then id as before; a
    result's `meta` (organisation, effective date, authority, topic,
    content type) travels with it.
  - The knowledge-base agent: `labels.vocabulary()` (the values stored
    chunks actually carry, topics with their names) goes into the run's
    settings. `_apply_settings` adds optional `topic`, `organisation` and
    `content_type` (enums) to the search tool only when there is a
    vocabulary. `_search` keeps only known values, retries without the
    filter when nothing matches, says so in the result, records `filters`
    and `unfiltered_retry` on the search, and shows each result's
    organisation and date in its citation line.
  - Tests: storage filters (each key, combinations, fields, no match, off =
    identical, unknown key refused), hybrid search with filters, authority
    breaks ties only, the tool schema, the filtered search and its retry.
    Full suite: 407 pass.
  - The comparison (one pass each, local build; the "before" ran the old
    code): built-in set 11/15 met before, 9/15 after (pass rate 73% to
    55%); setup evaluation 11/16 to 12/16 (pass rate 77% both). Re-running
    the four built-in questions that flipped, with filters on and off,
    showed the flips are mostly noise (the same questions failed or passed
    either way; the local knowledge base lacks Kids for a Quid and Advance
    fares). **The real finding:** the agent applied `topic` to nearly every
    search (all 12 for "how can I save money booking"), hiding *Refunds and
    changes* and *Ticket acceptance*, which explain ticket types but are
    labelled refunds and passenger rights. Next: #57 (topic as a
    preference), then the comparison twice per side.
- [ ] **4. UI**: labels on the Sources and Ingestion pages and in citations;
  the relabel action on the setup page; checked in the browser. Stop.
- [ ] **5. Docs, PR, deploy.**

## The user's answers (2026-10-09): the recommendations, all five

1. **The blueprint's own fields** (`ticket_type`, `passenger_category`…):
   fill them with one small model call per page (recommended: they are the
   domain's real vocabulary, and the call is cheap, about the cost of a
   question); or use only the deterministic fields (topic, organisation,
   source type, authority, dates).
2. **Who filters**: the knowledge-base agent chooses filters when the
   question clearly names a topic or organisation, with an automatic
   unfiltered retry when nothing matches (recommended); or no agent
   filters yet, only the API, for later use.
3. **Authority**: a tie-breaker only, when two results rank equally
   (recommended: changes nothing else); or a small boost for high-authority
   sources in every search.
4. **Research pages** (added when users' questions find gaps): label them
   the same way when they are added (recommended); or leave them unlabelled
   (then a filtered search never returns them).
5. **Pages already stored**: relabel them from their stored text when you
   press **Relabel** on the setup page (recommended: no fetching, about one
   small call per page); or only label new builds.
