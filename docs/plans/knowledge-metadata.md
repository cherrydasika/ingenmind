# Plan: knowledge metadata from the blueprint, and retrieval that uses it

Status: **Phase 4 (UI) done and pushed (2026-10-10); next is phase 5
(docs, PR out of draft, deploy) when the user asks.** Phase 3 with #57 (topic as a
preference) is committed and pushed to `init/metadata` (draft PR #56,
`a7e5e82`, CI passes). Update this file at the end of every step.

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
  - **#57, topic as a preference** (see
    [its section](#57-topic-as-a-preference)): A chosen and written
    2026-10-10, 409 pass; compared (after 10/12 met vs before 8/12).
- [x] **4. UI**: labels on the Sources and Ingestion pages and in citations;
  the relabel action on the setup page; checked in the browser. Stop.
  - `qdrant_overview.page_labels()`: each Sources row and each research row
    carries `labels` (topics, organisation, content type, date, the
    blueprint's fields, who labelled it), None when unlabelled. The answer's
    cited sources (`agent._cited_sources`) carry the page's `organisation`
    and `effective_date` when it has them.
  - `ui.js`: `labelsCell()` (topic badges, then kind of page and publisher)
    in a Labels column on the Sources page and in "Pages added by research";
    `sourceItem()` shows the publisher and date before the citation numbers.
  - The setup Build card (after the build): a Labels section with pages
    labelled (and how many by the model), content types, publishers and the
    blueprint's fields with counts, the latest relabel run, and **Relabel**
    (`POST /api/setup/relabel`; disabled while one runs; the page polls
    until it finishes).
  - Tests: the research overview's labels (and none when unlabelled); cited
    sources carry publisher and date. Full suite: 409 pass.
  - Browser (Playwright, local build): Sources shows the labels of all 17
    pages; the Build card shows 17 of 17 labelled, by the model; no page
    errors. Citations could not be seen in a real answer (questions are
    refused until setup is READY), so the citation line was rendered from
    the served `ui.js` with sample data. The 5 research pages showed no
    labels (added before research pages were labelled; Relabel covered
    only the plan's pages), so at the user's request **Relabel now covers
    research pages too** (`labels.research_urls()`; the summary has
    `research`: pages and labelled, shown on the Build card). Test: relabel
    labels a research page; full suite 410 pass. Live: Relabel clicked in
    the browser labelled 22 of 22 pages (17 plan, 5 research), all by the
    model; the research table shows their labels.
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

## #57: topic as a preference

GitHub: issue #57. The agent put `topic` on nearly every search, and a
strict topic filter hides pages labelled with a neighbouring topic that
still answer the question (*Refunds and changes* and *Ticket acceptance*
explain ticket types but are labelled refunds and passenger rights).

**What changes**

- `organisation` and `content_type` stay strict filters (facts about a
  page), with the unfiltered retry when nothing matches, as now.
- `topic` becomes a **preference**: the search runs over the whole
  knowledge base (within any strict filters), and pages with the topic rank
  higher. `hybrid_search(..., prefer={"topic": [...]})`; `meta_filter` is
  unchanged. No preference: results exactly as today (a test).
- The tool's description says `topic` *favours* that knowledge area and
  never hides other pages; the search record shows `prefer` apart from
  `filters`; the result line says "preferring topic=…".

**How a preference ranks: the open question.** Ranking "topic pages first,
then fill the remaining slots" as the issue words it does not fix the
example: "save money booking" had more than 5 `tickets_and_railcards`
hits, so no slot is left and the refunds page stays hidden. Two ways that
do:

- **A. The topic-filtered ranking as extra votes in the fusion**
  (recommended). Run the dense and full-text searches with and without the
  topic and fuse all four rankings by reciprocal rank, as today's two. A
  page with the topic that ranks well gets extra votes; an off-topic page
  that both searches put first still makes the top 5. No new constant;
  costs two more queries per search (milliseconds).
- **B. Reserved slots.** Of the top 5, up to 3 from the topic's pages and
  at least 2 from the whole knowledge base by the usual ranking. A firm
  guarantee, but the split is a fixed number to tune.

**Tests:** a preference reorders but never excludes (an off-topic best
match stays in the results); no preference = today's results; strict
filters unchanged; the tool schema and wording; the search record.

**The comparison, twice per side** (one pass moves by 2–3 questions by
chance): `travel_basics` and the setup evaluation on the local build,
"before" with filters off, "after" with the change; two runs per side for
each set, eight in all. Report the mean and both runs, and recheck "How can I save money when booking a
train ticket?" in the after runs for *Refunds and changes*.

**Done (2026-10-10): A, as the user chose.**

- `retrieval._fuse(..., preferred=())`: more rankings vote by reciprocal
  rank; the explain rows gain `preferred_part` only when there is a
  preference. `hybrid_search(..., prefer=None)` runs the dense and
  full-text searches again over `filters` plus the preference and passes
  them as `preferred`; the result and the span carry `prefer`.
- The agent: `topic` goes to `prefer`, `organisation` and `content_type`
  stay `filters`; the unfiltered retry drops only the filters and keeps the
  preference. The search record has `prefer`; the result line says
  "(filtered by …; preferring topic=…)". The tool's description and the
  topic's description say a topic never hides other pages.
- Tests: `test_pg_store` (a preferred topic ranks first and hides nothing;
  none = as before; an off-topic page first in both searches stays in);
  `test_flows` (topic preferred not filtered, the retry keeps it, the
  wording). Full suite: 409 pass.
- **The comparison (2026-10-10)**, shortened at the user's request to a
  6-question set `topic_preference_check` (saved locally, not in git): the
  "save money", "how far in advance" and "travelling with children"
  questions from `travel_basics` with their gold answers, and the Railcard
  savings, refund and Delay Repay questions from the setup evaluation; all
  on the setup flow `setup_uk_train_information` v4. "Before" ran phase 2
  (`d6eb73d`, no filters), "after" this change; alternated before, after,
  before, after.

  | Run | Met | Pass rate | Overall |
  |---|---|---|---|
  | Before 1 | 4/6 | 67% | 0.80 |
  | Before 2 | 4/6 | 67% | 0.79 |
  | After 1 | 4/6 | 67% | 0.80 |
  | After 2 | 6/6 | 100% | 0.99 |

  Railcard savings, refunds and Delay Repay pass in every run. "How far in
  advance" failed both before runs and passed both after runs. "Travelling
  with children" failed three times and passed only in after 2. "Save
  money" was met both times before by declining ("not enough on ticket
  types") though both pages were retrieved; after, one run failed the
  answer check and one passed. Reading: no question is worse, but six
  questions are mostly noise, and whether each search used a topic could
  not be checked (search records are not stored, and the local Langfuse is
  down). "Save money" depends more on how the answer is written than on
  what is retrieved.
