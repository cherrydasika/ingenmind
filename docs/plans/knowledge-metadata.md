# Plan: knowledge metadata from the blueprint, and retrieval that uses it

Status: **Phase 1 — plan written; waiting for the user's answers** (open
questions at the end). Branch `init/metadata`. Update this file at the end
of every step: tick what is done, note what was found, say what comes next.

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
- [ ] **2. Labels at ingestion**: the deterministic fields, the page
  labeller with its checks, the relabel step; tests (the built knowledge
  base carries the blueprint's fields); a live relabel of the local build.
  Stop.
- [ ] **3. Retrieval with filters and authority**: `hybrid_search` filters,
  the agent tool's optional filters with the unfiltered retry, authority in
  fusion, dates in citations; tests (a filtered search returns only matching
  chunks; filters off: unchanged); `travel_basics` and the setup evaluation
  run before and after, scores not lower. Stop.
- [ ] **4. UI**: labels on the Sources and Ingestion pages and in citations;
  the relabel action on the setup page; checked in the browser. Stop.
- [ ] **5. Docs, PR, deploy.**

## Open questions for the user

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
