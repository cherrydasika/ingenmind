"""Channels (R50, app/channels): the web widget, Slack and Teams ask through
the Ask page's path, keep a session per thread and take feedback."""

import hashlib
import hmac
import json
import time
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet

import answer_feedback
import channels
import identity
import sessions
import web_api
from channels import slack, teams, widget
from test_auth import AuthDatabase

SOURCES = [{"url": "https://rail.example/bikes", "cited": [1], "organisation": "Rail Example"}]


class ChannelDatabase(AuthDatabase):
    def setUp(self):
        for module in (channels, answer_feedback):      # each test class has its own database
            module.reset_schema_cache()
            self.addCleanup(module.reset_schema_cache)
        key = patch.dict("os.environ", {"INTEGRATIONS_KEY": Fernet.generate_key().decode()})
        key.start()
        self.addCleanup(key.stop)
        self.asked = []
        live = patch.object(web_api, "_ask_live", side_effect=self.fake_live)
        ready = patch.object(web_api.knowledge_system, "is_ready", return_value=True)
        for p in (live, ready):
            p.start()
            self.addCleanup(p.stop)

    def fake_live(self, question, session_id, body, max_iterations, emit):
        """The live flow, as the Ask page records it (the turn, its channel)."""
        self.asked.append((question, session_id, body))
        result = {"answer": "Bikes go **free** [1].", "sources": SOURCES, "answer_check": {"passed": True},
                  "channel": body["channel"]}
        result["turn_id"] = sessions.record_turn(session_id, body["user_id"], question, result, "trace-x")["turn_id"]
        return result

    def person(self, role="user"):
        return identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", role)


class Widget(ChannelDatabase):
    def test_its_settings_are_checked(self):
        self.assertEqual(widget.check({"allowed_origins": "https://help.example.com/, http://localhost:8080"})["allowed_origins"],
                         ["http://localhost:8080", "https://help.example.com"])
        for bad in ({"allowed_origins": []}, {"allowed_origins": ["https://x.com/page"]}, {"allowed_origins": ["ftp://x.com"]},
                    {"allowed_origins": ["https://x.com"], "mode": "everyone"}):
            with self.subTest(bad=bad), self.assertRaises(channels.ChannelError):
                widget.check(bad)

    def test_a_public_widget_asks_keeps_the_visitors_session_and_takes_feedback(self):
        w = channels.add("widget", "Help", {"allowed_origins": ["https://help.example.com"]})
        account = identity.get_user(w["user_id"])
        self.assertEqual(account["permissions"], ["query_rag"])          # its own account, asking only
        client = self.client()
        page = client.get(f"/widget/{w['channel_id']}")
        self.assertIn("frame-ancestors https://help.example.com", page.headers["content-security-policy"])
        self.assertNotIn("x-frame-options", page.headers)                    # the policy decides who frames it
        self.assertEqual(client.get("/").headers["x-frame-options"], "DENY")  # the app itself still can't be framed
        self.assertIn(b"data-widget", page.content)
        self.assertEqual(client.get("/widget.js").status_code, 200)
        visitor = str(uuid.uuid4())
        first = client.post(f"/widget/{w['channel_id']}/ask", json={"question": "Bikes?", "visitor": visitor}).json()
        again = client.post(f"/widget/{w['channel_id']}/ask", json={"question": "And dogs?", "visitor": visitor}).json()
        other = client.post(f"/widget/{w['channel_id']}/ask", json={"question": "Bikes?", "visitor": str(uuid.uuid4())}).json()
        self.assertEqual((first["answer"], first["sources"]), ("Bikes go **free** [1].", SOURCES))
        self.assertEqual(first["session_id"], again["session_id"])            # a follow-up: the same session
        self.assertNotEqual(first["session_id"], other["session_id"])
        self.assertEqual(self.asked[0][2]["channel"], "widget")
        self.assertEqual(self.asked[0][2]["trace_metadata"], {"channel": "widget"})
        self.assertEqual(sessions.get_session(first["session_id"])["turns"][0]["result"]["channel"], "widget")
        # Feedback: only on the visitor's own answers.
        rate = lambda v, turn: client.post(f"/widget/{w['channel_id']}/feedback", json={"visitor": v, "turn_id": turn, "rating": "down",
                                                                                   "reason": "incomplete"})
        self.assertEqual(rate(visitor, first["turn_id"]).status_code, 200)
        self.assertEqual(rate(str(uuid.uuid4()), first["turn_id"]).status_code, 404)
        self.assertEqual(channels.usage()["channels"]["widget"], 3)
        self.assertEqual(client.post(f"/widget/{w['channel_id']}/ask", json={"question": "x"}).status_code, 400)   # no visitor
        channels.remove(w["channel_id"])
        self.assertEqual(client.get(f"/widget/{w['channel_id']}").status_code, 404)
        self.assertEqual(identity.get_user(w["user_id"])["status"], "disabled")

    def test_a_signed_in_widget_needs_the_visitors_account(self):
        w = channels.add("widget", "Intranet", {"allowed_origins": ["https://intranet.example.com"], "mode": "signed_in"})
        visitor = str(uuid.uuid4())
        self.assertEqual(self.client().post(f"/widget/{w['channel_id']}/ask", json={"question": "Bikes?", "visitor": visitor}).status_code, 401)
        ada = self.person()
        reply = self.signed_in(ada).post(f"/widget/{w['channel_id']}/ask", json={"question": "Bikes?", "visitor": visitor})
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(self.asked[-1][2]["user_id"], ada["user_id"])


def signed(secret, body: bytes, stamp=None):
    stamp = str(int(stamp or time.time()))
    return {"X-Slack-Request-Timestamp": stamp, "Content-Type": "application/json",
            "X-Slack-Signature": "v0=" + hmac.new(secret.encode(), f"v0:{stamp}:".encode() + body, hashlib.sha256).hexdigest()}


class Slack(ChannelDatabase):
    def setUp(self):
        super().setUp()
        self.channel = channels.add("slack", "Company", {}, {"bot_token": "xoxb-1", "signing_secret": "shh"})
        self.calls, self.emails = [], {}

        def call(token, method, **payload):
            self.calls.append((method, payload))
            if method == "users.info":
                return {"user": {"profile": {"email": self.emails.get(payload["user"])}}}
            return {"ts": "111.2"}
        p = patch.object(slack, "call", side_effect=call)
        p.start()
        self.addCleanup(p.stop)

    def test_requests_must_be_signed_recently(self):
        body = b'{"type":"url_verification","challenge":"abc"}'
        url = f"/channels/slack/{self.channel['channel_id']}/events"
        client = self.client()
        self.assertEqual(client.post(url, content=body, headers=signed("shh", body)).json(), {"challenge": "abc"})
        self.assertEqual(client.post(url, content=body, headers=signed("wrong", body)).status_code, 401)
        self.assertEqual(client.post(url, content=body, headers=signed("shh", body, time.time() - 600)).status_code, 401)
        with self.assertRaises(channels.ChannelError):
            channels.add("slack", "x", {}, {"bot_token": "nope", "signing_secret": "s"})

    def test_a_mention_is_answered_in_its_thread_as_the_person(self):
        ada = self.person()
        self.emails["U1"] = ada["email"]
        channel = channels.get(self.channel["channel_id"], with_secret=True)
        event = {"type": "app_mention", "user": "U1", "text": "<@UBOT> can I take a bike?", "channel": "C1", "ts": "100.1"}
        slack.answer_event(channel, event, "T1")
        slack.answer_event(channel, {**event, "text": "<@UBOT> and a dog?", "ts": "100.9", "thread_ts": "100.1"}, "T1")
        self.assertEqual([q for q, _, _ in self.asked], ["can I take a bike?", "and a dog?"])
        self.assertEqual(self.asked[0][1], self.asked[1][1])                    # the thread keeps its session
        self.assertEqual(self.asked[0][2]["user_id"], ada["user_id"])
        update = [p for m, p in self.calls if m == "chat.update"][0]
        self.assertEqual(update["blocks"][0]["text"]["text"], "Bikes go *free* [1].")
        self.assertIn("[1] <https://rail.example/bikes|Rail Example>", update["blocks"][1]["elements"][0]["text"])
        buttons = update["blocks"][-1]["elements"]
        self.assertEqual([b["action_id"] for b in buttons], ["fb_up", "fb_down"])
        # 👎 then a reason, from the asker.
        with patch.object(slack, "_respond") as respond:
            for action in ("fb_down", "fb_reason:out_of_date"):
                slack.handle_action(channel, {"user": {"id": "U1"}, "response_url": "https://hooks.slack.com/x",
                                              "actions": [{"action_id": action, "value": buttons[1]["value"]}]})
            self.emails["U2"] = self.person()["email"]
            slack.handle_action(channel, {"user": {"id": "U2"}, "response_url": "https://hooks.slack.com/x",
                                          "actions": [{"action_id": "fb_up", "value": buttons[1]["value"]}]})
        self.assertEqual(answer_feedback.for_turns([int(buttons[1]["value"])])[int(buttons[1]["value"])]["reason"], "out_of_date")
        self.assertIn("Only the person who asked", respond.call_args.args[1])

    def test_someone_without_an_account_and_retried_events(self):
        channel = channels.get(self.channel["channel_id"], with_secret=True)
        slack.answer_event(channel, {"type": "app_mention", "user": "U9", "text": "hi", "channel": "C1", "ts": "1.0"}, "T1")
        self.assertIn("IngenMind account", self.calls[-1][1]["text"])
        self.assertEqual(self.asked, [])
        payload = {"event_id": "Ev1", "event": {"type": "message", "channel_type": "im", "user": "U9", "text": "x",
                                                "channel": "D1", "ts": "2.0"}}
        with patch.object(slack.threading, "Thread") as thread:
            slack.handle_event(channel, payload)
            slack.handle_event(channel, payload)                                 # Slack sent it again
            slack.handle_event(channel, {"event_id": "Ev2", "event": {**payload["event"], "bot_id": "B1"}})   # our own reply
        self.assertEqual(thread.call_count, 1)


class Teams(ChannelDatabase):
    APP = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        super().setUp()
        from authlib.jose import JsonWebKey
        self.key = JsonWebKey.generate_key("RSA", 2048, is_private=True)
        keys = patch.object(teams, "_key_set", return_value=JsonWebKey.import_key_set({"keys": [self.key.as_dict(is_private=False)]}))
        keys.start()
        self.addCleanup(keys.stop)

    def token(self, **claims):
        from authlib.jose import jwt
        now = int(time.time())
        body = {"iss": teams.ISSUER, "aud": self.APP, "exp": now + 600, "nbf": now - 10,
                "serviceUrl": "https://smba.trafficmanager.net/emea/", **claims}
        return "Bearer " + jwt.encode({"alg": "RS256"}, body, self.key).decode()

    def test_only_tokens_for_this_bot_and_service_url_pass(self):
        url = "https://smba.trafficmanager.net/emea/"
        self.assertTrue(teams.verify(self.token(), self.APP, url))
        self.assertFalse(teams.verify(self.token(aud="someone-else"), self.APP, url))
        self.assertFalse(teams.verify(self.token(iss="https://evil.example"), self.APP, url))
        self.assertFalse(teams.verify(self.token(exp=int(time.time()) - 3600), self.APP, url))
        self.assertFalse(teams.verify(self.token(), self.APP, "https://evil.example/"))   # replies can't be sent elsewhere
        self.assertFalse(teams.verify("Bearer nonsense", self.APP, url))

    def test_a_message_is_answered_with_a_card_and_feedback(self):
        c = channels.add("teams", "Teams", {"app_id": self.APP, "guests": True}, {"app_password": "pw"})
        channel = channels.get(c["channel_id"], with_secret=True)
        sent = []
        activity = {"type": "message", "id": "a1", "text": "<at>IngenMind</at> can I take a bike?",
                    "serviceUrl": "https://smba.trafficmanager.net/emea/", "conversation": {"id": "conv-1"}, "from": {"id": "29:x"}}
        with patch.object(teams, "_send", side_effect=lambda ch, act, reply: sent.append(reply)), \
                patch.object(teams, "_email", return_value=None):              # no account: a guest, allowed here
            teams.answer_activity(channel, activity)
            card = sent[-1]["attachments"][0]["content"]
            self.assertEqual(card["body"][0]["text"], "Bikes go **free** [1].")
            self.assertEqual(self.asked[0][0], "can I take a bike?")
            self.assertEqual(self.asked[0][2]["user_id"], c["user_id"])
            turn = card["actions"][1]["data"]["turn_id"]
            teams.answer_activity(channel, {**activity, "id": "a2", "text": "", "value": {"feedback": "down", "turn_id": turn}})
            self.assertEqual(len(sent[-1]["attachments"][0]["content"]["actions"]), 5)   # the reasons
        client = self.client()
        reply = client.post(f"/channels/teams/{c['channel_id']}/messages", json=activity, headers={"Authorization": "Bearer bad"})
        self.assertEqual(reply.status_code, 401)


class CachedAnswers(AuthDatabase):
    def test_a_cached_answer_takes_this_questions_channel_and_turn(self):
        from unittest.mock import Mock
        sessions.reset_schema_cache()
        ada = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "user")
        session_id = sessions.current(ada["user_id"], None)
        cached = {"answer": "Bikes go free.", "channel": "slack", "turn_id": 999999, "flow": {"id": "f", "version": 1}}
        with patch.object(web_api.human, "waiting_for", return_value=[]), \
                patch.object(web_api.conversation, "recent_questions", return_value=[]), \
                patch.object(web_api.agent, "live_flow", return_value=(Mock(nodes=[], id="f"), 1)), \
                patch.object(web_api.answer_cache, "eligible_question", return_value=True), \
                patch.object(web_api.answer_cache, "lookup", return_value=cached):
            on_page = web_api._ask_live("Bikes?", session_id, {"user_id": ada["user_id"]}, None, lambda *e: None)
            on_widget = web_api._ask_live("Bikes?", session_id, {"user_id": ada["user_id"], "channel": "widget"}, None, lambda *e: None)
        self.assertNotIn("channel", on_page)                                       # asked on the Ask page
        self.assertEqual(on_widget["channel"], "widget")
        self.assertNotEqual(on_page["turn_id"], 999999)
        turns = sessions.get_session(session_id)["turns"]
        self.assertEqual([t["result"].get("channel") for t in turns], [None, "widget"])


if __name__ == "__main__":
    import unittest
    unittest.main()
