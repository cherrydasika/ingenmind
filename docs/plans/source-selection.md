# Plan: source discovery and selection (approval 1)

Status: **Done — pull request open (closes #20). Deploy when EC2 next runs**
(it was stopped on 2026-10-09; EC2 stays `READY`, so setup is not exercised
there). The local install went through the Sources step in the
browser and is at `ANALYSING_SOURCES` with National Rail, ORR and Transport
for Wales chosen — the starting point for #21. The user took every recommendation (2026-10-09): nothing
fetched at this step (one check of a user's URL), live areas left to
data-source research, at most 8 searches and 15 sites, recommended sites
only suggested. Branch `init/source-selection`. Update this file at
the end of every step: tick what is done, note what was found, say what
comes next.

GitHub: issue #20, part of epic #27 (RAG Initialization Agent); builds on
#19 (the confirmed Domain Blueprint; setup is then at
`DISCOVERING_SOURCES`). Working agreement: phases with a stop at each; a
branch and a pull request; the user merges (AGENTS.md).

## Goal

With the blueprint confirmed, the setup agent finds the **sites** that
could feed the knowledge base and presents them like a consultant ("these
three look the most authoritative; here is why"). The user ticks the ones
they trust, removes any, and can add their own. **Nothing is scraped at
this step**: the agent judges from the blueprint and from search results.
Continuing (with at least one source chosen) moves setup to
`ANALYSING_SOURCES`, where #21 maps each chosen site's content.

## How the code is today

- **The blueprint** (`app/initialization/blueprint.py`): knowledge areas
  with a class (static, structured, dynamic, tool), organisations with role
  and website, source requirements, the search country. Locally: UK rail
  plus Eurostar, 7 static areas, 1 live area, 6 organisations (ORR, DfT,
  Transport Scotland, Network Rail, Transport Focus, Trainline) — National
  Rail itself is not among them, a good test that discovery finds sites the
  blueprint did not name.
- **Research tools** (`app/research.py`): `web_search` (Tavily, search
  country), `OFFICIAL_DOMAINS` (government and public-body domains).
- **Background runs and polling**: as the blueprint step does
  (`blueprint_run.py`, the setup page).
- **Data sources** (#10, `source_profiles.py`): profiles of APIs and feeds
  for live data, found when a question needs them.

## Design

### 1. The source registry (`app/initialization/sources.py`)

```
kb_sources   source_id, name, base_url, host (unique), kind (website | documents | api),
             origin (blueprint | search | user), authority (high | medium | low),
             relevance (0–1), areas[] (knowledge area keys it covers), reason,
             evidence jsonb (the search results that found it), recommended,
             status (candidate | selected | removed), created_at, updated_at
```

One row per **site** (host), not per page. A reset clears it. `kb_urls`
(#17) stays the list of URLs to ingest; the ingestion plan (#22) fills it
from the content chosen in #21.

### 2. Discovery (Source Discovery role), in the background

Starts when the blueprint is confirmed (as research starts when the
requirements are), shown with progress; state `DISCOVERING_SOURCES` →
`AWAITING_SOURCE_SELECTION` when done.

1. **Seeds**: the blueprint's organisations with a website.
2. **Search**: for each static or structured knowledge area, one search
   naming the region and asking for official guidance (at most 8 searches,
   the blueprint's search country); results grouped by host, with which
   areas each host turned up for.
3. **Assess** (one forced-tool call over seeds and hosts, with their
   search titles and snippets): per site its name, kind, authority,
   relevance, the areas it covers, a one-line reason, and whether to
   recommend it; at most 15 sites kept, the rest dropped.
4. **Rules in code**: an official domain (`OFFICIAL_DOMAINS`) or a
   regulator / government / operator from the blueprint is at least
   "high" authority; forums, social media and question-and-answer sites are
   never recommended; with `authoritative_only` in the blueprint, low
   authority sites are not recommended.

Live and tool areas are not sourced here: they are listed as "answered by
live tools", and their data sources come from data-source research (#10) —
a backlog item links them to setup later.

### 3. Selection

- The user selects or deselects any site, removes it (hidden, kept for the
  history), or **adds their own URL**: one request to check it answers and
  to read its title, and the research guardrail checks it fits the scope;
  it is stored with origin `user`. Recommended sites are **suggested, not
  pre-selected**: the user ticks them.
- **Coverage**: each static area shows the selected sites covering it; an
  area with none is flagged (not blocking).
- **Continue** needs at least one selected source and moves to
  `ANALYSING_SOURCES`. Going back to the blueprint is not offered here
  (the state machine goes back to the requirements or to this selection);
  changing the blueprint means changing the requirements.

### 4. API (`manage_settings`)

- `GET /api/setup` gains the sources (with coverage) and discovery status.
- `POST /api/setup/sources/discover` (start again), `POST
  /api/setup/sources/{id}` `{"status": selected | candidate | removed}`,
  `POST /api/setup/sources/add` `{"url"}`, `POST
  /api/setup/sources/continue`.

### 5. UI: the Sources step

A checklist: recommended sites first, each with name, host, kind,
authority and relevance badges, the areas it covers, the reason, and its
evidence on demand; a box to add a URL; the coverage panel; **Continue**.

### 6. Tracing

One trace per discovery run (`source_discovery`, `source:setup`) with
its searches and the assessment.

## Phases

- [x] **1. Plan** — this file.
- [x] **2. Registry and discovery**, with stubbed tests and a live check.
  - `app/initialization/sources.py`: `discover(blueprint)` — seeds from the
    blueprint's organisations with a website (and the areas whose evidence
    pages are on their site), one search per static or structured area
    (`"<domain> <area> <region> official"`, the search country), results
    grouped by host, one `assess_with_llm` call (`SiteAssessment`: name,
    kind, authority, relevance, areas, reason, recommended), then the rules:
    official domains and the blueprint's regulators, government bodies and
    operators are high authority and those organisations recommended;
    forums and social media never recommended; low authority not
    recommended when the blueprint wants authoritative sources; searched
    sites under 0.15 relevance dropped; areas limited to the blueprint's
    sourced ones; at most 15, recommended and high authority first.
    Registry `kb_sources` (one row per host): `save_discovered` (replaces
    earlier candidates, keeps the user's choices and own sites),
    `list_sources`, `set_status`, `coverage`; a reset clears it.
  - Live, first try (local blueprint): searches like "Delay Compensation UK
    official guidance" found airlines and no National Rail; the blueprint's
    own regulator and operator were not recommended ("little in the search
    results"). Fixed: searches name the domain; seeds carry the blueprint's
    evidence and its high-role organisations are recommended; near-zero
    relevance dropped. Second try (28 s): National Rail, Eurostar, ORR,
    Network Rail, GOV.UK, Transport Focus recommended; operators (Avanti,
    GWR, Thameslink) optional; no airlines.
  - `test_blueprint_live.py` now also runs discovery on the UK trains
    blueprint: **passed** (National Rail and GOV.UK/ORR recommended).
  - Tests: `SourceDiscovery` in `test_setup.py` (36 in the file).
- [x] **3. Selection, adding a URL, continue; background run and API**,
  with tests.
  - `app/initialization/sources_run.py`: discovery runs
    (`app_source_discoveries`: status, record, error; one at a time;
    expired after 15 minutes) in the background; done, they save the
    sites and move `DISCOVERING_SOURCES → AWAITING_SOURCE_SELECTION`; run
    again from the selection, the user's choices stay. `choose` (selected,
    candidate, removed; only while choosing), `add_site` (a web address;
    one request; refused if it does not answer, or refuses automated
    access (401/403), or the research guardrail finds it out of scope;
    stored selected, origin `user`; a listed site is selected instead),
    `continue_` (at least one selected; `→ ANALYSING_SOURCES`, the chosen
    hosts in the event, a turn in the conversation), `view` (run, sites,
    coverage, live areas). Confirming the blueprint starts discovery. A
    reset clears the runs.
  - API (`manage_settings`): `POST /api/setup/sources/discover`,
    `/sources/{id}` `{"status"}`, `/sources/add` `{"url"}`,
    `/sources/continue`; `GET /api/setup` carries the Sources step.
  - Live, locally: discovery ready in 21 s (15 sites, 8 recommended, the
    live area listed). Adding sites: a cooking site refused as out of
    scope; **Transport for Wales was refused at first** — the guardrail
    judged its home page's live disruption notices, which the scope sends
    to live tools. Fixed: the question asks whether the *site* could be a
    source, judging its subject, not the day's announcements; it was then
    added. ScotRail answers 403 to automated requests: refused, now with
    "refuses automated access" instead of "did not answer".
  - Tests: `SourcesStep` in `test_setup.py` (44 in the file); the routes
    in `test_auth.py`. Full suite 333 OK.
- [x] **4. The Sources step on the setup page**, checked in the browser.
  - `web/static/js/setup.js`: from `DISCOVERING_SOURCES` on, the Sources
    card replaces the blueprint (the blueprint and the conversation fold
    away below): "Looking for sources…" while discovery runs (polled);
    a failure with **Try again**; "I found N sites. These M look the most
    authoritative…"; a checklist, recommended first — name, host link,
    authority badge, "recommended", "added by you", kind, the reason, the
    areas covered, **Why it was found** (the search results), **Remove**
    for a site not chosen; **Add a site**; **Continue with N sources**
    (disabled until one is ticked); **Look again**. The side panel becomes
    **Coverage**: each sourced area with the chosen sites covering it, or
    "⚠ no chosen source yet", and the areas answered by live tools. After
    continuing, only the chosen sources are listed, read-only.
  - `site_name()`: a page title as a site name ("Homepage | Transport for
    Wales" → "Transport for Wales"), seen in the browser.
  - Browser, locally: ticking National Rail and ORR updated coverage at
    once; Seat61 removed; **Continue with 3 sources** moved to the Content
    step; no console errors.
- [x] **5. Docs, PR.** README (guided setup gains the Sources step;
  Langfuse gains `source_discovery`; the layout gains `sources.py`,
  `sources_run.py`); backlog: "Live data sources in setup". Deploy when
  EC2 next runs.

## Open questions for the user

1. **Nothing fetched at this step**: judge sites from the blueprint and
   search results only (recommended, as the issue says), except one request
   to check a URL the user adds. Or allow reading each candidate's home page
   for a better judgement (about 15 more fetches)?
2. **Live areas**: list them as "answered by live tools" and leave their
   data sources to the data-source research from #10, with a backlog item
   to bring those into setup (recommended), or search for APIs here too?
3. **Budget**: at most 8 searches and one assessment call per discovery,
   at most 15 sites kept (recommended).
4. **Recommended ≠ selected**: recommended sites are only suggested, the
   user ticks every source (recommended, as the issue says), or pre-tick
   the recommended ones?
