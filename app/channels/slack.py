"""Slack (R50): mention the app in a channel, or message it directly, to ask;
the answer comes in the thread with its sources and 👍 / 👎 buttons, and a
follow-up in the thread keeps the conversation.

Slack calls two URLs of this app (shown on Admin → Integrations):
    /channels/slack/<channel_id>/events       the Events API (app_mention, message.im)
    /channels/slack/<channel_id>/interactive  button clicks
Each request is signed with the app's signing secret (X-Slack-Signature),
checked before anything is read. Slack wants a reply within 3 seconds, so the
question is answered on a thread: a "Looking that up…" message first, then
replaced by the answer. The app needs the bot scopes app_mentions:read,
chat:write, im:history and users:read.email (the manifest is in the README).
"""

import hashlib
import hmac
import json
import os
import re
import threading
import time

import httpx

from . import ask, first_time, person, sources_list

API_URL = os.environ.get("SLACK_API_URL", "https://slack.com/api")
MAX_AGE = 300            # seconds: an older signed request is a replay
SECTION_CHARS = 2900     # Slack's limit is 3000 per section block
REASON_ACTIONS = {"wrong": "Wrong", "incomplete": "Incomplete", "out_of_date": "Out of date",
                  "not_cited": "Not cited", "other": "Other"}


class SlackError(RuntimeError):
    pass


def verify(signing_secret: str, headers: dict, body: bytes, now: float | None = None) -> bool:
    """Slack's v0 signature: HMAC-SHA256 of "v0:<timestamp>:<body>", the timestamp recent."""
    stamp, signature = headers.get("x-slack-request-timestamp", ""), headers.get("x-slack-signature", "")
    if not stamp.isdigit() or abs((now or time.time()) - int(stamp)) > MAX_AGE:
        return False
    expected = "v0=" + hmac.new(signing_secret.encode(), f"v0:{stamp}:".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def call(token: str, method: str, **payload) -> dict:
    reply = httpx.post(f"{API_URL}/{method}", json=payload, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    data = reply.json()
    if not data.get("ok"):
        raise SlackError(f"{method}: {data.get('error')}")
    return data


def mrkdwn(text: str) -> str:
    """The answer's Markdown as Slack's mrkdwn: bold, links and headings."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text or "")
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    return re.sub(r"(?m)^#{1,6}\s+(.+)$", r"*\1*", text)


def blocks(result: dict, turn_id: int | None) -> list[dict]:
    answer = mrkdwn(result.get("answer") or "")
    out = [{"type": "section", "text": {"type": "mrkdwn", "text": answer[i:i + SECTION_CHARS]}}
           for i in range(0, max(len(answer), 1), SECTION_CHARS)] if answer else []
    cited = sources_list(result)
    if cited:
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "  ".join(
            f"{n} <{url}|{label}>" for n, url, label in cited)[:2900]}]})
    check = result.get("answer_check") or {}
    if check and not check.get("passed"):
        out.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "_This answer didn't pass the answer check._"}]})
    if turn_id:
        out.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "👍"}, "action_id": "fb_up", "value": str(turn_id)},
            {"type": "button", "text": {"type": "plain_text", "text": "👎"}, "action_id": "fb_down", "value": str(turn_id)}]})
    return out


def _email(token: str, slack_user: str) -> str | None:
    try:
        return call(token, "users.info", user=slack_user)["user"]["profile"].get("email")
    except Exception:
        return None


def answer_event(channel: dict, event: dict, team: str) -> None:
    """One question from Slack, answered in its thread (on a background thread)."""
    token = channel["secret"]["bot_token"]
    where, thread = event["channel"], event.get("thread_ts") or event["ts"]
    question = re.sub(r"<@[A-Z0-9]+>", "", event.get("text") or "").strip()
    try:
        user = person(channel, _email(token, event["user"]))
        if user is None:
            call(token, "chat.postMessage", channel=where, thread_ts=thread,
                 text="I can only answer people with an IngenMind account. Ask an admin to add you.")
            return
        if not question:
            call(token, "chat.postMessage", channel=where, thread_ts=thread, text="What would you like to know?")
            return
        placeholder = call(token, "chat.postMessage", channel=where, thread_ts=thread, text="Looking that up…")
        out = ask(channel, f"{team}:{where}:{thread}", question, user)
        if out.get("refused"):
            call(token, "chat.update", channel=where, ts=placeholder["ts"], text=out["refused"])
            return
        result = out["result"]
        call(token, "chat.update", channel=where, ts=placeholder["ts"], text=(result.get("answer") or "")[:3000],
             blocks=blocks(result, result.get("turn_id")))
    except Exception as error:
        print(f"slack: couldn't answer: {type(error).__name__}: {error}")
        try:
            call(token, "chat.postMessage", channel=where, thread_ts=thread,
                 text="Something went wrong answering that. Please try again.")
        except Exception:
            pass


def handle_event(channel: dict, payload: dict) -> dict:
    """The Events API request (already verified): the URL check, or a question to answer."""
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    event = payload.get("event") or {}
    asked = event.get("type") == "app_mention" or (event.get("type") == "message" and event.get("channel_type") == "im")
    if not asked or event.get("bot_id") or event.get("subtype") or not event.get("user"):
        return {}
    if not first_time(channel["channel_id"], payload.get("event_id") or event.get("client_msg_id") or event["ts"]):
        return {}   # Slack sent it again: it's being answered
    threading.Thread(target=answer_event, args=(channel, event, payload.get("team_id") or ""), daemon=True).start()
    return {}


def _respond(url: str, text: str, replace: bool = False, blocks_: list | None = None) -> None:
    if not url.startswith(("https://hooks.slack.com/", API_URL)):
        return   # only where Slack said to answer
    httpx.post(url, json={"response_type": "ephemeral", "replace_original": replace, "text": text,
                          **({"blocks": blocks_} if blocks_ else {})}, timeout=10)


def handle_action(channel: dict, payload: dict) -> None:
    """A 👍 / 👎 or a reason, from the person who asked (already verified)."""
    import answer_feedback
    action = (payload.get("actions") or [{}])[0]
    action_id, value = action.get("action_id", ""), action.get("value", "")
    url = payload.get("response_url") or ""
    user = person(channel, _email(channel["secret"]["bot_token"], (payload.get("user") or {}).get("id", "")))
    if not value.isdigit() or user is None:
        return
    try:
        if action_id == "fb_up":
            answer_feedback.give(int(value), user["user_id"], "up")
            _respond(url, "Thanks for the feedback.")
        elif action_id == "fb_down":
            answer_feedback.give(int(value), user["user_id"], "down")
            _respond(url, "Thanks. What was wrong with it?", blocks_=[
                {"type": "section", "text": {"type": "mrkdwn", "text": "Thanks. What was wrong with it?"}},
                {"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": label},
                                                  "action_id": f"fb_reason:{reason}", "value": value}
                                                 for reason, label in REASON_ACTIONS.items()]}])
        elif action_id.startswith("fb_reason:") and action_id[10:] in REASON_ACTIONS:
            answer_feedback.give(int(value), user["user_id"], "down", action_id[10:])
            _respond(url, f"Noted: {REASON_ACTIONS[action_id[10:]].lower()}. Thank you.", replace=True)
    except LookupError:
        _respond(url, "Only the person who asked can rate this answer.")


def parse_interactive(body: bytes) -> dict:
    from urllib.parse import parse_qs
    return json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
