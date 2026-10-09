"""The readiness report (epic #19, #17): how well the knowledge system covers
what the blueprint says it should, from data only, and what to do about
each gap.

    report()   the scores, each with its definition and numbers; the overall
               score and its formula; the gaps per knowledge area, each with
               a suggested action; whether the evaluation is current; at
               READY, what went live
    go_live()  the candidate flow the evaluation measured becomes what users
               get, and EVALUATING → READY; refused without a current
               evaluation, and with gaps until the user confirms them

Scores, each 0–1 (None: not measured yet):

    source_coverage      sourced areas with a chosen source covering them (by
                         the source's own assessment or one of its chosen
                         sections) ÷ sourced areas
    knowledge_coverage   sourced areas with at least MIN_CHUNKS chunks from the
                         approved plan ÷ sourced areas
    retrieval_quality    mean retrieval relevance of the areas with answer
                         questions (the latest run)
    groundedness         mean groundedness of those areas (the latest run)
    evaluation_coverage  questions whose expectation was met ÷ questions (the
                         latest run)
    overall              the plain mean of the five; not measured until all are

The run-based scores count only a finished run of the current preparation
(the approved plan's set, on its candidate flow): what goes live is what was
measured. Sourced areas: STATIC_KNOWLEDGE and STRUCTURED_DATA; live areas
are answered by tools and only appear in the evaluation.
"""

import statistics

import flows
import knowledge_system
from common import storage

from . import content, evaluation, plan, sources, state

MIN_CHUNKS = 10
LIVE_CLASSES = evaluation.LIVE_CLASSES

SCORES = [
    ("source_coverage", "Source coverage",
     "Knowledge areas with a chosen source covering them ÷ knowledge areas that need sources."),
    ("knowledge_coverage", "Knowledge coverage",
     f"Knowledge areas with at least {MIN_CHUNKS} chunks in the knowledge base ÷ knowledge areas that need sources."),
    ("retrieval_quality", "Retrieval quality",
     "Mean, over the areas with answer questions, of the share whose expected page was retrieved."),
    ("groundedness", "Answer groundedness",
     "Mean, over those areas, of the answer check's faithfulness score."),
    ("evaluation_coverage", "Evaluation coverage",
     "Evaluation questions that did what they should (answered, said not available, used a live tool, or "
     "were refused) ÷ questions."),
]
OVERALL = "The plain mean of the five scores."


def _area_chunks(plan_version: int) -> dict[str, int]:
    """Chunks per knowledge area from the approved plan."""
    client = storage.get_client()
    try:
        with client.connection() as connection:
            rows = connection.execute(f"""
                SELECT area, count(*) AS n FROM {client.table},
                       jsonb_array_elements_text(payload->'areas') AS area
                WHERE payload->>'plan_version' = %s GROUP BY area""", (str(plan_version),)).fetchall()
    except Exception:
        return {}
    return {r["area"]: r["n"] for r in rows}


def _current_run(approved_version: int | None) -> tuple[dict | None, dict | None]:
    """(the latest preparation, its run if finished and current)."""
    prepared = evaluation.view()
    if not prepared or prepared["status"] != "ready" or prepared["plan_version"] != approved_version:
        return prepared, None
    run = prepared.get("run")
    return prepared, run if run and run["status"] == "done" and (run.get("summary") or {}).get("areas") else None


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _mean(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 3) if values else None


def _missed(question: dict, result: dict) -> str:
    """What a question that missed its expectation did, in a few words."""
    if result.get("failed"):
        return "the run failed"
    if result.get("input_blocked") or result.get("output_blocked"):
        return "refused"
    kind = question.get("kind")
    if kind == "out_of_scope":
        return "answered, though out of scope"
    if kind == "live":
        return "no live tool was called"
    if kind == "answer":
        if result.get("expected_retrieved") is False:
            return "its page was not found"
        if result.get("answer_type") not in (None, "answer"):
            return "said the information is not available, though its page was found"
        if result.get("passed") is False:
            return "the answer failed the answer check"
    if kind == "not_covered":
        return "the answer failed the answer check"
    return "missed"


def report() -> dict:
    status = knowledge_system.status()
    bp = content._confirmed_blueprint()
    approved = plan.latest("approved")
    version = approved["version"] if approved else None
    areas = bp["knowledge_areas"]
    sourced = [a for a in areas if a["knowledge_class"] in sources.SOURCED_CLASSES]
    live = [a for a in areas if a["knowledge_class"] in LIVE_CLASSES]
    names = {a["key"]: a["name"] for a in areas}

    chosen = [s for s in sources.list_sources() if s["status"] == "selected"]
    chosen_ids = {s["source_id"] for s in chosen}
    sections = [c for c in content.list_sections() if c["status"] == "selected" and c["source_id"] in chosen_ids]
    by_source = {k for s in chosen for k in (s.get("areas") or [])}
    by_section = {k for c in sections for k in (c.get("areas") or [])}
    chunks = _area_chunks(version) if version else {}
    prepared, run = _current_run(version)
    per_area = (run or {}).get("summary", {}).get("areas", {})

    gaps = []
    for a in sourced:
        key, name = a["key"], a["name"]
        if key not in by_source | by_section:
            gaps.append({"area": key, "name": name, "kind": "no_source", "action": "review_sources",
                         "detail": "No chosen source covers it.", "suggestion": f"Add a source for {name}."})
        elif key not in by_section:
            gaps.append({"area": key, "name": name, "kind": "no_content", "action": "review_content",
                         "detail": "A chosen source covers it, but no chosen section does.",
                         "suggestion": f"Choose a section for {name}."})
        elif chunks.get(key, 0) < MIN_CHUNKS:
            gaps.append({"area": key, "name": name, "kind": "few_pages", "action": "review_content",
                         "detail": f"{chunks.get(key, 0)} chunks in the knowledge base (fewer than {MIN_CHUNKS}).",
                         "suggestion": f"Add pages for {name}."})
    if run:
        for result in run["results"]:
            question = run["questions"][result["idx"]] if result["idx"] < len(run["questions"]) else {}
            if result["expectation_met"] is not False:
                continue
            key = result.get("area") or question.get("area") or ""
            kind = result.get("kind") or question.get("kind")
            gap = {"area": key, "name": names.get(key, "Out of scope" if key == "out_of_scope" else key),
                   "question": question.get("question", ""), "detail": _missed(question, result)}
            if kind == "live":
                gaps.append({**gap, "kind": "live_unanswered", "action": None,
                             "suggestion": "No live tool answers it: narrow the blueprint's scope, or add an API "
                                           "tool for it."})
            elif kind == "out_of_scope":
                gaps.append({**gap, "kind": "not_refused", "action": "review_blueprint",
                             "suggestion": "Review the blueprint's scope, then prepare the evaluation again."})
            else:
                gaps.append({**gap, "kind": "failing_question", "action": "run_again",
                             "suggestion": "Fix its source or content, rebuild, then run the evaluation again."})

    answered = [m for m in per_area.values() if m.get("answer_questions")]
    summary = (run or {}).get("summary") or {}
    numbers = {
        "source_coverage": (len([a for a in sourced if a["key"] in by_source | by_section]), len(sourced)),
        "knowledge_coverage": (len([a for a in sourced if chunks.get(a["key"], 0) >= MIN_CHUNKS]), len(sourced)),
        "evaluation_coverage": (summary.get("expectations_met"), summary.get("expectations")) if run else None,
    }
    values = {
        "source_coverage": _ratio(*numbers["source_coverage"]),
        "knowledge_coverage": _ratio(*numbers["knowledge_coverage"]),
        "retrieval_quality": _mean([m.get("retrieval_relevance") for m in answered]) if run else None,
        "groundedness": _mean([m.get("groundedness") for m in answered]) if run else None,
        "evaluation_coverage": _ratio(*numbers["evaluation_coverage"]) if run else None,
    }
    measured = all(v is not None for v in values.values())
    overall = round(statistics.fmean(values.values()), 3) if measured else None
    scores = [{"key": key, "label": label, "definition": definition, "value": values[key],
               "numbers": numbers.get(key)} for key, label, definition in SCORES]
    return {
        "state": status["state"], "plan_version": version, "measured": measured,
        "scores": scores,
        "overall": {"value": overall, "formula": OVERALL,
                    "worked": " + ".join(f"{values[k]:.2f}" for k, _, _ in SCORES) + f" ÷ 5 = {overall:.2f}"
                    if measured else None},
        "gaps": gaps,
        "areas": {"sourced": len(sourced), "live": len(live), "names": names},
        "evaluation": {"current": run is not None, "run_id": run["id"] if run else None,
                       "prepared": prepared["status"] if prepared else None,
                       "finished_at": run.get("finished_at") if run else None,
                       "flow_id": prepared.get("flow_id") if prepared else None,
                       "flow_version": prepared.get("flow_version") if prepared else None},
        "can_go_live": status["state"] == state.EVALUATING and run is not None,
        "went_live": went_live() if status["state"] == state.READY else None,
    }


# ---------- going live ----------

class GapsNotConfirmed(ValueError):
    """Going live with gaps needs the user to confirm them."""


def went_live() -> dict | None:
    """The latest go-live: when, by whom, the flow it made live and the one it replaced, the report then."""
    event = next((e for e in knowledge_system.events(200) if e["event"] == "state"
                  and (e["details"] or {}).get("to") == state.READY and (e["details"] or {}).get("went_live")), None)
    return {"at": event["at"], "user_id": event["user_id"], **event["details"]["went_live"]} if event else None


def go_live(user_id: str | None, confirm_gaps: bool = False, check=None) -> dict:
    """Make the measured candidate flow live and move setup to READY; the report it went live with."""
    current = report()
    if current["state"] != state.EVALUATING:
        raise state.TransitionNotAllowed("setup goes live from its evaluation")
    if not current["can_go_live"]:
        raise ValueError("run the evaluation on the current plan first: what goes live is what was measured")
    if current["gaps"] and not confirm_gaps:
        raise GapsNotConfirmed(f"there {'is' if len(current['gaps']) == 1 else 'are'} {len(current['gaps'])} "
                               f"gap{'' if len(current['gaps']) == 1 else 's'}: confirm them to go live anyway")
    builtin = flows.load_flow()
    flow_id, version = current["evaluation"]["flow_id"], current["evaluation"]["flow_version"]
    previous = flows.store.live_pointer() or {"flow_id": builtin.id, "version": 0}
    flows.store.set_live(flow_id, version, builtin, check or evaluation._check_flow)
    record = {"flow_id": flow_id, "flow_version": version,
              "replaced": {"flow_id": previous["flow_id"], "version": previous["version"]},
              "overall": current["overall"]["value"], "scores": {s["key"]: s["value"] for s in current["scores"]},
              "gaps": [{"area": g["area"], "kind": g["kind"]} for g in current["gaps"]],
              "run_id": current["evaluation"]["run_id"], "plan_version": current["plan_version"]}
    try:
        state.transition(state.READY, user_id, expected=state.EVALUATING, went_live=record)
    except state.TransitionNotAllowed:
        flows.store.set_live(previous["flow_id"], previous["version"], builtin, lambda spec: None)   # put it back
        raise
    return report()
