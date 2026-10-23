"""Microsoft Teams (R50): a bot; mention it in a channel or chat with it to ask.
The answer comes as a reply in the conversation, with its sources and 👍 / 👎
buttons (an Adaptive Card); later messages in the conversation keep it.

Azure Bot Service posts activities to /channels/teams/<channel_id>/messages
(the bot's messaging endpoint, shown on Admin → Integrations). Each carries a
Bot Framework token, checked against Microsoft's published keys (issuer
https://api.botframework.com, audience the bot's App ID, and the serviceUrl
it was issued for) before anything is read. Replies go back to the
activity's serviceUrl with a token for the bot's App ID and client secret.
"""

import os
import re
import threading
import time

import httpx

from . import ask, first_time, person, sources_list

OPENID_URL = os.environ.get("TEAMS_OPENID_URL", "https://login.botframework.com/v1/.well-known/openidconfiguration")
LOGIN_URL = os.environ.get("TEAMS_LOGIN_URL", "https://login.microsoftonline.com")
ISSUER = "https://api.botframework.com"
KEYS_FOR = 24 * 3600
REASONS = {"wrong": "Wrong", "incomplete": "Incomplete", "out_of_date": "Out of date",
           "not_cited": "Not cited", "other": "Other"}

_keys = {"at": 0.0, "set": None}
_tokens: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


class TeamsError(RuntimeError):
    pass


def _key_set():
    from authlib.jose import JsonWebKey
    with _lock:
        if _keys["set"] is None or time.time() - _keys["at"] > KEYS_FOR:
            jwks_uri = httpx.get(OPENID_URL, timeout=10).json()["jwks_uri"]
            _keys.update(set=JsonWebKey.import_key_set(httpx.get(jwks_uri, timeout=10).json()), at=time.time())
        return _keys["set"]


def reset_keys() -> None:
    _keys.update(set=None, at=0.0)
    _tokens.clear()


def verify(authorization: str, app_id: str, service_url: str) -> bool:
    """The Bot Framework token on an incoming activity: signed by Microsoft, for this bot and this serviceUrl."""
    from authlib.jose import JsonWebToken
    if not authorization.lower().startswith("bearer "):
        return False
    try:
        claims = JsonWebToken(["RS256"]).decode(authorization[7:].strip(), _key_set(), claims_options={
            "iss": {"essential": True, "value": ISSUER}, "aud": {"essential": True, "value": app_id}})
        claims.validate(leeway=300)
    except Exception:
        return False
    return claims.get("serviceUrl", service_url).rstrip("/") == (service_url or "").rstrip("/")


def _token(channel: dict) -> str:
    app_id = channel["config"]["app_id"]
    found = _tokens.get(app_id)
    if found and found[1] > time.time() + 60:
        return found[0]
    tenant = channel["config"].get("tenant_id") or "botframework.com"
    reply = httpx.post(f"{LOGIN_URL}/{tenant}/oauth2/v2.0/token", data={
        "grant_type": "client_credentials", "client_id": app_id,
        "client_secret": channel["secret"]["app_password"], "scope": "https://api.botframework.com/.default"}, timeout=15)
    if reply.status_code != 200:
        raise TeamsError(f"no bot token: HTTP {reply.status_code}")
    data = reply.json()
    _tokens[app_id] = (data["access_token"], time.time() + int(data.get("expires_in", 3600)))
    return data["access_token"]


def _send(channel: dict, activity: dict, reply: dict) -> None:
    url = f"{activity['serviceUrl'].rstrip('/')}/v3/conversations/{activity['conversation']['id']}/activities/{activity['id']}"
    sent = httpx.post(url, json={"type": "message", "replyToId": activity["id"], **reply},
                      headers={"Authorization": f"Bearer {_token(channel)}"}, timeout=15)
    if sent.status_code >= 300:
        raise TeamsError(f"reply: HTTP {sent.status_code}")


def _email(channel: dict, activity: dict) -> str | None:
    """The sender's email or UPN, from the conversation's member list."""
    try:
        url = (f"{activity['serviceUrl'].rstrip('/')}/v3/conversations/{activity['conversation']['id']}"
               f"/members/{activity['from']['id']}")
        member = httpx.get(url, headers={"Authorization": f"Bearer {_token(channel)}"}, timeout=10).json()
        return member.get("email") or member.get("userPrincipalName")
    except Exception:
        return None


def card(result: dict, turn_id: int | None) -> dict:
    body = [{"type": "TextBlock", "text": result.get("answer") or "", "wrap": True}]
    cited = sources_list(result)
    if cited:
        body.append({"type": "TextBlock", "wrap": True, "isSubtle": True, "size": "Small",
                     "text": "  ".join(f"{n} [{label}]({url})" for n, url, label in cited)})
    check = result.get("answer_check") or {}
    if check and not check.get("passed"):
        body.append({"type": "TextBlock", "text": "This answer didn't pass the answer check.", "isSubtle": True, "size": "Small"})
    actions = [{"type": "Action.Submit", "title": t, "data": {"feedback": r, "turn_id": turn_id}}
               for r, t in (("up", "👍"), ("down", "👎"))] if turn_id else []
    return {"contentType": "application/vnd.microsoft.card.adaptive", "content": {
        "type": "AdaptiveCard", "version": "1.4", "body": body, "actions": actions}}


def _feedback(channel: dict, activity: dict, user: dict | None, value: dict) -> None:
    import answer_feedback
    turn_id, rating = value.get("turn_id"), value.get("feedback")
    if not isinstance(turn_id, int) or user is None:
        return
    try:
        if rating == "up":
            answer_feedback.give(turn_id, user["user_id"], "up")
            _send(channel, activity, {"text": "Thanks for the feedback."})
        elif rating == "down":
            answer_feedback.give(turn_id, user["user_id"], "down", value.get("reason"))
            if value.get("reason") in REASONS:
                _send(channel, activity, {"text": f"Noted: {REASONS[value['reason']].lower()}. Thank you."})
            else:
                _send(channel, activity, {"text": "Thanks. What was wrong with it?", "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive", "content": {
                        "type": "AdaptiveCard", "version": "1.4", "body": [], "actions": [
                            {"type": "Action.Submit", "title": label, "data": {"feedback": "down", "reason": reason, "turn_id": turn_id}}
                            for reason, label in REASONS.items()]}}]})
    except LookupError:
        _send(channel, activity, {"text": "Only the person who asked can rate this answer."})


def answer_activity(channel: dict, activity: dict) -> None:
    """One message from Teams, answered in its conversation (on a background thread)."""
    try:
        user = person(channel, _email(channel, activity))
        if isinstance(activity.get("value"), dict) and activity["value"].get("feedback"):
            return _feedback(channel, activity, user, activity["value"])
        question = re.sub(r"<at>.*?</at>", "", activity.get("text") or "").strip()
        if user is None:
            return _send(channel, activity, {"text": "I can only answer people with an IngenMind account. Ask an admin to add you."})
        if not question:
            return _send(channel, activity, {"text": "What would you like to know?"})
        _send(channel, activity, {"type": "typing"})
        out = ask(channel, activity["conversation"]["id"], question, user)
        if out.get("refused"):
            return _send(channel, activity, {"text": out["refused"]})
        result = out["result"]
        _send(channel, activity, {"text": "", "attachments": [card(result, result.get("turn_id"))]})
    except Exception as error:
        print(f"teams: couldn't answer: {type(error).__name__}: {error}")


def handle(channel: dict, activity: dict) -> None:
    """An activity (already verified): a message or a card's button, answered on a thread."""
    if activity.get("type") != "message" or not activity.get("serviceUrl") or not activity.get("conversation"):
        return
    if not first_time(channel["channel_id"], activity.get("id") or ""):
        return
    threading.Thread(target=answer_activity, args=(channel, activity), daemon=True).start()
