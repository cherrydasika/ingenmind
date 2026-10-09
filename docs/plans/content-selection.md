# Plan: source analysis and content selection (approval 2)

Status: **Done — merged in PR 33 (earlier repository) (`e91cc18`) and deployed to EC2 on
2026-10-09.** The local install is at `AWAITING_CONTENT_SELECTION` with National Rail's Help and assistance chosen (12 of 13 pages) — #14's
starting point. The user took every recommendation (2026-10-09): 15
requests and 2,000 listed pages a site, same host only, sections with
single pages unticked within them, a warning for very large sections. Branch `init/content-selection`. Update this file at
the end of every step: tick what is done, note what was found, say what
comes next.

GitHub: issue #13, part of epic #19 (RAG Initialization Agent); builds on
#12 (the chosen sources; setup is then at `ANALYSING_SOURCES`). Working
agreement: phases with a stop at each; a branch and a pull request; the
user merges (AGENTS.md).

## Goal

For each site the user chose, the setup agent works out **what is on it**
— its sections and pages — maps them to the blueprint's knowledge areas and
recommends the relevant ones. The user then ticks **exactly** which
sections, and within them which pages, enter the knowledge base. This is
approval 2: still nothing is ingested (the ingestion plan and "Build RAG"
are #14). Reading a site is **polite and bounded**, not a crawl.

## How the code is today

- **Fetching** (`dags/common/scraping/scraper.py`): `fetch_bytes` /
  `fetch_html` with a jittered 1–2.5 s delay before every request and
  backoff on 429/5xx; user agent `rag-ingest-bot/1.0`. **No `robots.txt`
  handling, and links are dropped**: `extract_text` (trafilatura) keeps the
  text only; `research.fetch_source` returns text, title and dates.
- **Chosen sources** (`kb_sources`, #12): host, base URL, the areas each
  covers. Locally: National Rail, ORR, Transport for Wales.
- **Background runs** with polling, as discovery and the blueprint do.
- `lxml` and `urllib.robotparser` are available in the image.

## Design

### 1. Link-aware fetching (`scraper.py`)

`extract_links(html, base_url)`: every `<a href>` resolved to an absolute
URL, with its anchor text and whether it sits in navigation (`<nav>`,
`<header>`, `role="navigation"`, menus). `research.fetch_source` also
returns `links` for HTML pages. (This also unblocks the backlog item "Read a
website's terms page".)

### 2. Site analysis (Source Analysis role), per chosen site, in the background

1. **`robots.txt`**: read with `urllib.robotparser` for our user agent;
   disallowed paths are never listed or fetched; a `Crawl-delay` longer than
   our delay is honoured; a site that disallows everything is shown as
   "cannot be read" with the reason.
2. **Sitemap**: the `Sitemap:` lines in `robots.txt`, else
   `/sitemap.xml`; a sitemap index is followed into its child sitemaps;
   page URLs with their `lastmod`, same host only, at most 2,000 listed.
3. **No sitemap**: the home page's links, preferring navigation, then one
   level down: up to 12 section pages, their links collected.
4. **Budget**: at most 15 requests per site (robots, sitemaps, pages); a
   403 or bot check ends the site's analysis with "refuses automated
   access".
5. **Sections**: URLs grouped by path — the first path segment, or the
   first two when the first is a generic container (`help`,
   `travel-information`, `en`, …) — each with a name (navigation anchor text
   when one points there, else the path made readable), its URL count,
   sample URLs and the newest `lastmod`; PDFs counted.
6. **Mapping** (one forced-tool call per site): per section, the
   knowledge areas it serves, relevance, whether to recommend it, a reason,
   and flags for low-value content (terms and conditions, news, careers,
   press, corporate, cookie and privacy pages).

### 3. Storage

```
kb_content   content_id, source_id, section_key, name, path_prefix, urls jsonb (url, lastmod),
             url_count, pdf_count, areas[], relevance, recommended, flags[], reason,
             status (candidate | selected | removed), excluded_urls[], updated_at
kb_site_analyses   source_id, status (analysing | ready | blocked | failed), robots, sitemap,
                   requests made, error, created_at
```

A reset clears both. The state moves `ANALYSING_SOURCES →
AWAITING_CONTENT_SELECTION` once every chosen site is analysed (ready,
blocked or failed).

### 4. Selection

Per site, a tree of sections with checkboxes, URL counts and badges
(recommended, PDFs, flagged); a section expands to its URLs, each with a
checkbox (unticking one adds it to `excluded_urls`). Recommended sections
are **suggested, not pre-ticked**. A running total of pages chosen, and
coverage per knowledge area. Going back to source selection is allowed
(the state machine has it). Choosing content is where #13 ends; **Review
the plan / Build RAG** is #14.

### 5. API and tracing

- `GET /api/setup` gains the sites' analyses and their sections.
- `POST /api/setup/content/analyse` (again), `POST
  /api/setup/content/{id}` `{"status"}`, `POST
  /api/setup/content/{id}/urls` `{"excluded"}`, `POST /api/setup/back-to-sources`.
- Langfuse: `site_analysis` per site (requests, sections) with its mapping
  generation.

## Phases

- [x] **1. Plan** — this file.
- [x] **2. Link-aware fetching, `robots.txt`, sitemaps, navigation,
  sections**, with tests on fixture sites (no network).
  - `scraping.extract_links(html, base_url)`: absolute links without
    fragments, anchor text, `nav` (inside `<nav>`/`<header>` or a
    navigation or menu role); `research.fetch_source` returns them for
    HTML. `scraping.USER_AGENT`.
  - `app/initialization/site_map.py`: `Fetcher` (a request budget; a
    `Crawl-delay` above our delay is slept on top), `read_robots`
    (`urllib.robotparser`; a 401/403 is "refuses automated access"),
    `parse_sitemap` (index or urlset, gzip, namespaces; no entities or
    network), `analyse_site` (home page for navigation names; sitemaps —
    robots' lines or `/sitemap.xml`, children of an index, same host,
    robots-allowed, at most 2,000 — else the navigation and up to 12 pages
    one level down; the home page itself not listed), `section_key`
    (first path segment, two under a container such as `help`,
    `travel-information`, `en`), `sections` (named from the navigation).
  - Live, first run: National Rail's first of 21 sitemaps
    ("destinations") filled all 2,000 places (one section); ORR's flat
    paths made 197 sections; names like "See all", "View all Media centre
    content", "www.tfw.wales". Fixed: sitemaps read general first and
    changing ones (news, live, incidents) last, each with a fair share;
    on a flat site (five or more paths under three pages) those are
    gathered into "Other pages"; generic link texts are not names. Second
    run: National Rail 10 sections (Tickets, railcards and offers 196;
    Help and assistance; On the train; Railcards; Timetables …), ORR 59,
    TfW 9; 8–28 s a site.
  - Tests: `SiteAnalysis` in `test_setup.py` (links, a sitemap site with
    an index, disallows, other hosts, a PDF and a crawl delay; a site read
    from its navigation; robots and 403 blocks; the budget and the listing
    cap; fair shares; flat sites; section keys).
- [x] **3. Mapping, storage, background run, selection, API**, with
  tests and a live check on the three local sites.
  - `app/initialization/content.py`: `map_with_llm` (per site, one call:
    `SectionAssessment` — areas, relevance, recommended, flags from a fixed
    list, reason — from section names, page counts and sample paths),
    `apply_rules` (flagged or under 0.2 relevance not recommended; areas
    outside the blueprint dropped; recommended first, "Other pages" last),
    storage `kb_site_analyses` (status, how, robots, requests, pages; an
    analysis older than 20 minutes still "analysing" fails) and
    `kb_content` (one row per section, its pages, the mapping, the user's
    status and excluded pages; analysing again keeps the choice for a
    section found again and drops the rest), `start` (every chosen site, or
    one, in a background thread; then `ANALYSING_SOURCES →
    AWAITING_CONTENT_SELECTION`), `choose`, `exclude` (only the section's
    own pages), `back_to_sources`, `view` (each chosen site with its
    analysis and sections — page lists on demand — pages chosen, sections
    over 200 pages marked large, coverage per area). Continuing from
    sources starts the analysis; a reset clears both tables.
  - API (`manage_settings`): `POST /api/setup/content/analyse`, `GET
    /api/setup/content/{id}` (the section's pages), `POST
    /api/setup/content/{id}` `{"status"}`, `POST
    /api/setup/content/{id}/urls` `{"excluded"}`, `POST
    /api/setup/back-to-sources`.
  - Live, locally (120 s, three sites): National Rail — Tickets, railcards
    and offers (196), Railcards, Help and assistance, On the train
    recommended; Destinations, Discover by train not; "Other pages" flagged
    live/search. ORR — the annual rail consumer reports and passenger
    information recommended, accessibility consultations listed. TfW — Book
    Passenger Assistance and Help and contact recommended; Discover Wales
    (358 tourism pages) not; "All updates" flagged live.
  - Tests: `ContentStep` in `test_setup.py`; the routes in `test_auth.py`.
    Full suite 349 OK.
- [x] **4. The Content step on the setup page**, checked in the browser.
  - `web/static/js/setup.js`: from `ANALYSING_SOURCES` on, the Content card
    (sources, blueprint and conversation fold away below): per chosen
    site, "Reading the site…" while it is analysed (polled), why a blocked
    or failed site cannot be read with **Analyse again**, how it was read
    ("486 pages listed from its sitemap, following its robots.txt"); its
    recommended or chosen sections, the rest folded ("6 more sections, not
    recommended"); per section a checkbox, path, pages ("12 of 13 pages"
    when some are unticked), badges (recommended, large, PDFs, flags), the
    reason, the areas covered, **Pages** (opens the section's pages, the
    first 200, each with a checkbox and its date), **Remove**; the
    running total of pages chosen and **Change the sources**. The side
    panel is coverage by chosen section.
  - Found in the browser: reasons named areas by their keys
    ("delay_compensation knowledge area"); keys are now replaced by names
    in code, and the mapper is asked for names.
  - Browser, locally: ticking Help and assistance updated coverage at
    once; its pages opened with dates; unticking the live-departures how-to
    made it "12 of 13 pages", 12 chosen in all; no console errors.
- [x] **5. Docs, PR.** README (guided setup gains the Content step; the
  scraper section says site analysis follows `robots.txt` and ingestion
  does not yet; Langfuse gains `site_analysis`; the layout gains
  `site_map.py`, `content.py`); the backlog's "Read a website's terms
  page" notes that links are now returned.
  - **For #14**: ingestion (`common.ingest.ingest_url`) does not check
    `robots.txt`; the ingestion plan takes its pages from analysed
    sections, which only list robots-allowed pages, but a plan's URLs
    should be checked again when ingested.

## Open questions for the user

1. **Budget per site**: at most 15 requests and 2,000 listed URLs
   (recommended: with the polite delay that is about 30–60 seconds a site).
   More requests find more without a sitemap, but take longer.
2. **Same host only**: a site's analysis lists pages on its own host, not
   its subdomains or other domains (recommended: `www.gov.uk` stays apart
   from `assets.publishing.service.gov.uk`). Or follow subdomains?
3. **Granularity**: choose sections, and untick single pages within them
   (recommended), or choose page by page?
4. **Large sections**: a section is listed with all its pages (up to the
   2,000 listed); how many are ingested is the ingestion plan's limit
   (#14). Show a warning when a chosen section is very large (for example
   over 200 pages)? (Recommended.)
