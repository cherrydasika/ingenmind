# Plan: readiness report, gaps and go live

Status: **Phase 3 done (Go live) — waiting for the user's check before
phase 4** (the UI). Branch `init/readiness`, draft PR #45. Update this file
at the end of every step: tick what is done, note what was found, say what
comes next.

GitHub: issue #17, part of epic #19 (RAG Initialization Agent); builds on
#16 (the evaluation: setup is at `EVALUATING` with a candidate flow, a set
and its run). Working agreement: phases with a stop at each; a branch and a
pull request; the user merges (AGENTS.md). Nothing goes into the knowledge
base without the user's approval (their rule, 2026-10-09).

## Goal

An honest **readiness report** at `EVALUATING`: how well the knowledge
system covers what the blueprint says it should, every score defined and
computed (never a model's opinion), the gaps per knowledge area with what
to do about each, and the actions: review sources, review content, run the
evaluation again, or **Go live**.

## How the code is today

- **States** (`initialization/state.py`): from `EVALUATING` setup may go
  back to `AWAITING_SOURCE_SELECTION` or `AWAITING_CONTENT_SELECTION`
  (choices kept), and forward to `READY` (`set_state` records origin
  `setup`, who and when). Rebuilding makes a new plan version; at
  `EVALUATING` again the evaluation prepares itself for the new plan.
- **What each score can come from**:
  - the blueprint: sourced areas (`STATIC_KNOWLEDGE`, `STRUCTURED_DATA`) and
    live areas (`DYNAMIC_KNOWLEDGE`, `EXTERNAL_TOOL_API`);
  - selected sources (`kb_sources`, status `selected`, with the `areas` they
    cover) and selected content sections (`kb_content`, with `areas`);
  - the approved plan's chunks (`rag_chunks`, `payload.plan_version`,
    `payload.areas`);
  - the latest evaluation run (`summary.areas`: met, retrieval relevance,
    correctness, groundedness, citation accuracy, coverage, refusals).
- **Go live** pieces exist: `flows.store.set_live` (checked to run) makes
  the candidate flow what users get; the previous live version stays in the
  flow's history, so it can be put back from the flow builder.
- **Home** shows services, shortcuts and project links only. Every other
  page shows a "being set up" notice until the state is `READY`.

## Design

### 1. Scores (`initialization/readiness.py`), each 0–1, shown with its definition

| Score | Definition |
|---|---|
| Source coverage | sourced areas with at least one selected source covering them ÷ sourced areas |
| Knowledge coverage | sourced areas with at least `MIN_CHUNKS` chunks from the approved plan ÷ sourced areas |
| Retrieval quality | mean retrieval relevance over the areas with answer questions (latest run) |
| Answer groundedness | mean groundedness over those areas (latest run) |
| Evaluation coverage | questions whose expectation was met ÷ questions (latest run) |
| **Overall** | the formula chosen (question 1), shown on the page with its numbers |

An area with no source scores 0 for its source coverage, so the overall
score cannot reach 100% (acceptance criterion). Without a finished run on
the current preparation, the run-based scores and the overall score are
"not measured yet", with **Run the evaluation** as the action.

### 2. Gaps per knowledge area, each with a suggested action

| Gap | Shown when | Suggested action |
|---|---|---|
| No source | a sourced area no selected source covers | Review sources: add a source for *area* |
| No content | it has a source, but no chosen section covers it | Review content: choose a section for *area* |
| Few pages | fewer than `MIN_CHUNKS` chunks from the plan | Review content: add pages for *area* |
| Failing questions | questions of the area that missed their expectation | named, with what happened (page not found, not cited, said "not available"); run again after a fix |
| Live area not answered | a live area's question missed (refused, or no tool called) | there is no live tool for it: narrow the scope, or add an API tool (outside setup) |
| Refusals | out-of-scope questions answered | review the blueprint's scope |

### 3. Actions and API (`manage_settings`)

- `GET /api/setup/readiness`: the scores, their definitions and numbers,
  the gaps; also in the setup view at `EVALUATING` and `READY`.
- **Review sources** / **Review content**: the existing back transitions
  (`/api/setup/back-to-sources`, `/api/setup/back-to-content`).
- **Run the evaluation again**: #16's run.
- **Go live** (`POST /api/setup/go-live`): the candidate flow version is
  made live (`flows.store.set_live`, checked to run), then `EVALUATING →
  READY`; recorded in the history with the scores and gaps at that moment
  and the flow version it replaced. Conditions: question 2 and 3.

### 4. UI

- Setup page, "Build and evaluate" step: the **readiness report** under the
  Evaluation card: the scores (each with its definition and numbers), the
  overall score with its formula, the gaps by area with their actions, and
  the buttons. At `READY`, the "Go live" step shows what went live, when and
  by whom, and the report at that moment.
- Home: a **Knowledge system** card (question 5).

## Phases

- [x] **1. Plan**: this file. Stop for the user's answers.
- [x] **2. Scores and gaps** (`readiness.py`), with tests: a knowledge area
  with no source is a gap and keeps the overall score below 100%; the
  numbers come from stubbed runs. Plus a live check on the local build.
  Stop.
  - `app/initialization/readiness.py`: `report()` gives the five scores
    (definition, value, numbers), the overall score (the plain mean) with
    its working shown, the gaps (`no_source`, `no_content`, `few_pages`,
    `failing_question`, `live_unanswered`, `not_refused`, each with an
    action and a suggestion), whether the evaluation is current, and
    `can_go_live` (at `EVALUATING` with a finished run of the current
    preparation). An area counts as covered by a source when a chosen
    source lists it **or** one of its chosen sections does, because a site
    the user adds has no areas of its own. API: `GET /api/setup/readiness`;
    the setup view has `readiness` from `EVALUATING` on.
  - Tests: 5 in `test_setup.Readiness` (before a run only coverage is
    measured; a finished run measures the rest and lists its misses; an
    area with no source is a gap and keeps the overall score below 100%,
    the acceptance criterion; a run for an earlier plan is not current;
    the API). The evaluation tests' fixture is now the shared class
    `Evaluated`. Full suite: 387 pass.
  - Live on the local build: source and knowledge coverage 0.57 (4 of 7
    areas), retrieval 1.00, groundedness 0.98, evaluation coverage 0.94
    (15/16), overall **0.81**. Gaps: no source for Accessibility, Station
    facilities and Eurostar (the same three the Coverage panel shows);
    Live departures refused (no live tool). The local run was scored
    before #16's last expectation fix, so a new run may count one more
    miss.
- [x] **3. Go live**: the API, the conditions, the state move, the history
  record, tests. The acceptance check, run locally: going back to sources,
  adding a source, rebuilding and re-running updates the report. Stop.
  - `readiness.go_live()` and `POST /api/setup/go-live {confirm_gaps}`.
    Refused away from `EVALUATING`, without a finished run of the current
    preparation ("what goes live is what was measured"), and with gaps
    until confirmed (409, `confirm: true`). It makes the candidate flow
    version live (`flows.store.set_live`, checked to compile), then
    `EVALUATING → READY`. The state event records `went_live`: the flow
    and version, the one it replaced, the overall score, the scores, the
    gaps (area and kind), the run and the plan. If the state moved on
    meanwhile, the previous live flow is put back. `report()` at `READY`
    shows `went_live` (when, by whom, what).
  - The acceptance check is an end-to-end test, not a run on the local
    database. Adding a source and approving a rebuild are the user's
    approvals to give (their rule: nothing goes into the database without
    them). The test starts from an area with no source, then goes back to
    sources, adds a site (stubbed fetch and scope check), analyses it, the
    user chooses its section, rebuilds and evaluates again: the "no source"
    gap is gone and source coverage is 3/3. The same can be done by hand
    on the local build once the UI is in (phase 4).
  - Tests: 6 more in `test_setup.Readiness` (go live after confirming the
    gaps; it needs a current evaluation; a flow that cannot run does not
    go live; a race puts the live flow back; the acceptance check; the
    API). Full suite: 393 pass.
- [ ] **4. UI**: the readiness report on the setup page, the Go live step,
  the Home card, checked in the browser. Stop.
- [ ] **5. Docs, PR, deploy.**

## The user's answers (2026-10-09): the recommendations, all five

1. **The overall score**: the plain mean of the five scores (recommended:
   simple, and each gap visibly costs points); or weighted towards
   coverage; or the lowest of the five (strictest: one weak score is the
   overall).
2. **Go live with gaps**: allowed after you confirm the listed gaps
   (recommended: you decide, the report is honest); or only above a
   threshold (for example 70%); or only with no gaps at all.
3. **Go live needs a current evaluation**: the latest run must be on the
   current plan's set and candidate flow (recommended), so what goes live
   is what was measured.
4. **"Few pages"**: an area with fewer than 10 chunks from the plan
   (recommended; the local build has 14–35 per covered area).
5. **Home**: a "Knowledge system" card with the state, the setup step, the
   overall score and a link to setup (recommended); or leave Home as it is.
