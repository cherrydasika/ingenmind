"""Read-only access to evaluation runs produced by the former local workflow."""

import json
import os
from pathlib import Path

EVAL_DIR = Path(os.environ.get("EVAL_DIR", "/data/evaluations"))


def get(run_id: str) -> dict | None:
    if not run_id or not all(char.isalnum() or char == "-" for char in run_id):
        return None
    try:
        run = json.loads((EVAL_DIR / f"{run_id}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    run.setdefault("scores", {model: [None] * len(run["questions"]) for model in run["models"]})
    run.setdefault("langfuse", None)
    if run["status"] in ("running", "queued"):
        run["status"] = "interrupted"
    return run


def list_runs() -> list[dict]:
    runs = []
    for path in EVAL_DIR.glob("*.json") if EVAL_DIR.exists() else []:
        run = get(path.stem)
        if run:
            runs.append({key: run[key] for key in
                         ("id", "name", "status", "created_at", "finished_at", "models", "progress", "error")}
                        | {"questions": len(run["questions"]),
                           "recorded": bool((run.get("langfuse") or {}).get("recorded_at"))})
    return sorted(runs, key=lambda run: run["created_at"], reverse=True)