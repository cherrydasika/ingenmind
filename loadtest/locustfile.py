"""Load tests (R61): how many people one instance serves, and where it breaks.

Three kinds of simulated user, against the public API (/api/v1) with API keys:
    Asker       asks a question every few seconds, sometimes a follow-up in
                the same session (the bulk of real traffic)
    Ingester    adds a short document now and then (admin key)
    Evaluator   starts an evaluation of the live flow on a small set, rarely
                (admin key; runs in the background on the server)

    LOADTEST_KEYS   a JSON file: {"users": [key, ...], "admin": key}
                    (make_keys.py writes one)
    LOADTEST_SET    the question set evaluations run (default travel_basics)

Run against an instance whose chat model is the stub (LLM_PROVIDER=stub,
LLM_STUB_SECONDS ≈ a real model's latency) to measure the app, or a real
model to measure the whole thing (costs money; mind the provider's limits).
See loadtest/README.md for the runs behind the capacity number.
"""

import json
import os
import random
import threading

from locust import HttpUser, between, events, task

KEYS = json.load(open(os.environ.get("LOADTEST_KEYS", "keys.json")))
SET_ID = os.environ.get("LOADTEST_SET", "travel_basics")
QUESTIONS = [
    "Can I take a folding bike on the train?",
    "Do I need to book a space for a full-size bike?",
    "Can my dog travel with me on the train?",
    "How do I get a refund for a cancelled train?",
    "What is a Railcard and how much can I save?",
    "Are there toilets at London Victoria station?",
    "Can children travel alone on trains?",
    "What happens if my train is delayed by more than an hour?",
    "Can I change my ticket to a different time?",
    "Is there step-free access at London Victoria?",
]
FOLLOW_UPS = ["And on the way back?", "What about at weekends?", "Is that the same for Scotland?"]
_next = iter(range(10 ** 9))
_lock = threading.Lock()


def _key(users: list[str]) -> str:
    with _lock:                       # one key (person) per simulated user, round robin
        return users[next(_next) % len(users)]


class Asker(HttpUser):
    weight = 20
    wait_time = between(5, 15)        # think time between questions

    def on_start(self):
        self.client.headers["Authorization"] = f"Bearer {_key(KEYS['users'])}"
        self.session_id = None

    @task(4)
    def ask(self):
        self._ask(random.choice(QUESTIONS), new=True)

    @task(1)
    def follow_up(self):
        if self.session_id:
            self._ask(random.choice(FOLLOW_UPS), new=False)

    def _ask(self, question: str, new: bool):
        body = {"question": question, **({} if new or not self.session_id else {"session_id": self.session_id})}
        with self.client.post("/api/v1/ask", json=body, name="ask" if new else "ask (follow-up)",
                              catch_response=True, timeout=180) as response:
            if response.status_code == 429:
                response.failure("rate limited")        # a limit, not an error: still counted
            elif response.status_code != 200:
                response.failure(f"{response.status_code}: {response.text[:120]}")
            else:
                data = response.json()
                self.session_id = data.get("session_id") or self.session_id
                if data.get("status") not in ("done", None):
                    response.failure(f"status {data.get('status')}")


class Ingester(HttpUser):
    weight = 1
    wait_time = between(30, 90)

    def on_start(self):
        self.client.headers["Authorization"] = f"Bearer {KEYS['admin']}"

    @task
    def add_document(self):
        n = random.randrange(10 ** 6)
        text = (f"Load-test notice {n}. Engineering works this weekend between two example stations; "
                "replacement buses run every 30 minutes and tickets are accepted on them. ") * 20
        self.client.post("/api/v1/documents", json={"title": f"Load test notice {n}", "text": text, "ttl_days": 1},
                         name="ingest document", timeout=180)


class Evaluator(HttpUser):
    weight = 1
    wait_time = between(300, 600)     # an evaluation every 5–10 minutes at most

    def on_start(self):
        self.client.headers["Authorization"] = f"Bearer {KEYS['admin']}"

    @task
    def evaluate(self):
        self.client.post("/api/v1/evaluations", json={"set_id": SET_ID, "flow_id": "travel_assistant", "versions": [0]},
                         name="start evaluation", timeout=60)


@events.quitting.add_listener
def _summary(environment, **_):
    stats = environment.stats.get("ask", "POST")
    if stats.num_requests:
        print(f"ask: {stats.num_requests} requests, {stats.num_failures} failed, "
              f"p50 {stats.get_response_time_percentile(0.5) / 1000:.1f}s, "
              f"p95 {stats.get_response_time_percentile(0.95) / 1000:.1f}s")
