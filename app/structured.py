"""Clean-up for Claude's forced-tool (structured) output before Pydantic
validates it. The model sometimes sends a list as a JSON string or plain
text, a score as a string or slightly above 1, or leaks tool-call markup
("</field><parameter name=...>") into a string field. Used by the evidence
evaluator, the source validator and the answer evaluator.
"""

import json


def _strip_markup(value):
    if isinstance(value, str) and "<parameter" in value:
        return value.split("</")[0].split("<parameter")[0].strip()
    return value


def _score(value):
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return min(max(float(value), 0.0), 1.0)
    return value


def as_list(value) -> list:
    """None → [], a JSON-array string → its items, other text → [text]."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except ValueError:
                pass
        return [text] if text else []
    return value if isinstance(value, list) else [value]


def normalise(raw, scores=(), lists=(), strings=()) -> dict:
    """scores: fields clamped to 0–1 (strings parsed); lists: fields that must
    be lists of strings; strings: fields that must be strings ('' if missing)."""
    raw = {k: _strip_markup(v) for k, v in (raw if isinstance(raw, dict) else {}).items()}
    for key in scores:
        if key in raw:
            raw[key] = _score(raw[key])
    for key in lists:
        raw[key] = [v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for v in as_list(raw.get(key))]
    for key in strings:
        if not isinstance(raw.get(key), str):
            raw[key] = "" if raw.get(key) is None else str(raw[key])
    return raw
