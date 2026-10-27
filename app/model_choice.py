"""The chat model an admin chose in the app (Monitoring → Overview → Chat
model): the models .env names (local ones with scripts/use-local-models.sh),
or a frontier model on the admin's own API key. The key is tested before it
is kept, stored encrypted (secrets_box.py, INTEGRATIONS_KEY) and never sent
back to the browser; every process picks the choice up within WATCH_SECONDS
(watch()), and the one that saved it at once."""

import logging
import threading
import time

import psycopg
from psycopg.rows import dict_row

import llm
import secrets_box
from common import config

log = logging.getLogger(__name__)

WATCH_SECONDS = 10
# What the card offers. Claude's ids are the current ones (Opus the strongest,
# Haiku the cheapest); OpenAI's model is typed in, as its names change often.
PROVIDERS = {
    "anthropic": {"label": "Claude (Anthropic)", "models": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
                  "key_url": "https://console.anthropic.com/settings/keys", "key_prefix": "sk-ant-"},
    "openai": {"label": "OpenAI", "models": [], "key_url": "https://platform.openai.com/api-keys", "key_prefix": "sk-"},
}


class ChoiceError(Exception):
    """Something the admin can fix: said in words they can act on."""


def _connect():
    return psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                           password=config.PGPASSWORD, dbname=config.PGDATABASE, row_factory=dict_row)


_schema_ready = False
_schema_lock = threading.Lock()


def reset_schema_cache() -> None:
    global _schema_ready
    _schema_ready = False


def ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with _schema_lock:
        if _schema_ready:
            return
        with _connect() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(72616760)")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS app_model_choice (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    provider text NOT NULL,
                    model text NOT NULL,
                    sealed_key text NOT NULL,
                    key_hint text NOT NULL,
                    set_by text,
                    set_at timestamptz NOT NULL DEFAULT now()
                )""")
        _schema_ready = True


def _row() -> dict | None:
    ensure_schema()
    with _connect() as connection:
        return connection.execute("SELECT * FROM app_model_choice").fetchone()


def view() -> dict:
    """For the card: what runs now, what can be chosen. Never the key."""
    row = _row()
    chosen = {k: row[k] for k in ("provider", "model", "key_hint", "set_by")} | {"set_at": row["set_at"].isoformat()} \
        if row else None
    provider, model = llm.configured()
    return {"chosen": chosen, "local": {"provider": provider, "model": model,
                                         "label": f"{model} · {llm.LABELS.get(provider, provider)}"},
            "providers": PROVIDERS, "can_store": secrets_box.available()}


def check_key(provider: str, model: str, key: str) -> None:
    """One tiny call with the key: ChoiceError, in words, if it can't be used."""
    try:
        if provider == "anthropic":
            import anthropic
            client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=30)
            client.messages.create(model=model, max_tokens=64, messages=[{"role": "user", "content": "Say OK."}])
        else:
            import openai
            client = openai.OpenAI(api_key=key, max_retries=0, timeout=30)
            client.chat.completions.create(model=model, max_completion_tokens=64,
                                           messages=[{"role": "user", "content": "Say OK."}])
    except Exception as error:
        raise ChoiceError(_explain(error, provider, model)) from None


def _explain(error: Exception, provider: str, model: str) -> str:
    status = getattr(error, "status_code", None)
    text = str(getattr(error, "message", None) or error).lower()
    name = PROVIDERS[provider]["label"]
    if status in (401, 403) or "api key" in text or "authentication" in text:
        return f"{name} refused that key. Check it was copied whole, from {PROVIDERS[provider]['key_url']}"
    if "credit" in text or "billing" in text or "quota" in text or status == 402:
        return f"The key works, but its {name} account has no credit left. Add credit, then try again."
    if status == 404 or ("model" in text and ("not found" in text or "does not exist" in text)):
        return f"{name} doesn't offer {model!r} to this key. Pick another model."
    if status == 429:
        return f"{name} is rate-limiting this key right now. Try again in a minute."
    if "connection" in type(error).__name__.lower() or "timeout" in type(error).__name__.lower():
        return f"Couldn't reach {name} from the server. Check its internet connection."
    return f"{name} said: {getattr(error, 'message', None) or error}"


def save(provider: str, model: str, key: str, user: dict | None) -> dict:
    """Test, store and switch to a frontier model. An empty key keeps the stored one (changing only the model)."""
    if provider not in PROVIDERS:
        raise ChoiceError(f"Choose one of: {', '.join(p['label'] for p in PROVIDERS.values())}")
    model = (model or "").strip()
    if not model:
        raise ChoiceError("Choose a model.")
    key = (key or "").strip()
    row = _row()
    if not key:
        if not row or row["provider"] != provider:
            raise ChoiceError(f"Paste your {PROVIDERS[provider]['label']} API key.")
        key = secrets_box.open_(row["sealed_key"])["key"]
    if not secrets_box.available():
        raise ChoiceError("The server can't store keys safely yet: set INTEGRATIONS_KEY in .env "
                          "(scripts/use-local-models.sh makes one) and restart.")
    check_key(provider, model, key)
    with _connect() as connection:
        connection.execute("""
            INSERT INTO app_model_choice (singleton, provider, model, sealed_key, key_hint, set_by, set_at)
            VALUES (true, %s, %s, %s, %s, %s, now())
            ON CONFLICT (singleton) DO UPDATE SET provider = EXCLUDED.provider, model = EXCLUDED.model,
                sealed_key = EXCLUDED.sealed_key, key_hint = EXCLUDED.key_hint, set_by = EXCLUDED.set_by,
                set_at = now()""",
            (provider, model, secrets_box.seal({"key": key}), "…" + key[-4:], (user or {}).get("first_name")))
    apply()
    return view()


def clear(user: dict | None) -> dict:
    """Back to the models .env names; the stored key is deleted."""
    ensure_schema()
    with _connect() as connection:
        connection.execute("DELETE FROM app_model_choice")
    apply()
    return view()


_applied: tuple | None = ()


def apply() -> None:
    """Run this process on the stored choice (or .env's models), if that changed."""
    global _applied
    try:
        row = _row()
    except psycopg.Error as error:
        log.warning("model choice unavailable: %s", error)
        return
    stamp = (row["provider"], row["model"], row["set_at"]) if row else None
    if stamp == _applied:
        return
    if row:
        try:
            key = secrets_box.open_(row["sealed_key"])["key"]
        except Exception as error:     # INTEGRATIONS_KEY changed or missing: stay on .env's models
            log.warning("the chosen model's key can't be read (%s); using .env's models", error)
            llm.choose(None)
            _applied = stamp
            return
        llm.choose(row["provider"], row["model"], key)
        log.info("chat model: %s %s (chosen in the app)", row["provider"], row["model"])
    else:
        llm.choose(None)
        if _applied:
            log.info("chat model: back to %s %s (.env)", llm.PROVIDER, llm.MODEL)
    _applied = stamp


_watching = False


def watch() -> None:
    """Keep this process on the current choice: now, then every WATCH_SECONDS."""
    global _watching
    if _watching:
        return
    _watching = True
    apply()

    def loop():
        while True:
            time.sleep(WATCH_SECONDS)
            try:
                apply()
            except Exception as error:   # never let the watcher die
                log.warning("model choice check failed: %s", error)
    threading.Thread(target=loop, name="model-choice", daemon=True).start()
