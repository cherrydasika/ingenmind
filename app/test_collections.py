"""Collections (R35, knowledge_collections.py): several knowledge bases, a knowledge agent per
collection, a router, and per-role access."""

import threading
import unittest
import uuid
from unittest.mock import patch

import psycopg
from psycopg import sql

import agent
import identity
import knowledge_collections as kc
from common import config, storage
from flows.architectures import architectures


class Collections(unittest.TestCase):
    def setUp(self):
        self.original_database = config.PGDATABASE
        self.database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=self.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.database)))
        config.PGDATABASE = self.database
        kc._schema_ready = identity._schema_ready = False
        self.addCleanup(self.drop)

    def drop(self):
        config.PGDATABASE = self.original_database
        kc._schema_ready = identity._schema_ready = False
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER, password=config.PGPASSWORD,
                             dbname=self.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(self.database)))

    def two(self):
        kc.update(kc.MAIN, "UK rail travel", "Tickets, fares and railcards.")
        kc.create("hr", "HR handbook", "Staff travel and expenses.", ["admin"])

    def test_collections_and_their_sources(self):
        self.assertEqual([c["collection_id"] for c in kc.all_collections()], [kc.MAIN])
        self.two()
        self.assertEqual([(c["collection_id"], c["name"]) for c in kc.all_collections()],
                         [(kc.MAIN, "UK rail travel"), ("hr", "HR handbook")])
        for bad in (lambda: kc.create("main", "x"), lambda: kc.create("HR!", "x"), lambda: kc.create("hr", "again"),
                    lambda: kc.create("ops", ""), lambda: kc.create("ops", "Ops", roles=["wizard"]),
                    lambda: kc.delete(kc.MAIN), lambda: kc.assign(["u"], "nowhere")):
            with self.assertRaises(kc.CollectionError):
                bad()
        self.assertEqual(kc.assign(["adhoc://policy", "adhoc://policy"], "hr"), 1)
        self.assertEqual(kc.of_sources(["adhoc://policy", "https://rail.example"]),
                         {"adhoc://policy": "hr", "https://rail.example": kc.MAIN})
        kc.assign(["adhoc://policy"], kc.MAIN)                                   # back to the main one
        self.assertEqual(kc.of_sources(["adhoc://policy"]), {"adhoc://policy": kc.MAIN})
        kc.assign(["adhoc://policy"], "hr")
        self.assertTrue(kc.delete("hr"))                                         # its sources go back to main
        self.assertEqual(kc.of_sources(["adhoc://policy"]), {"adhoc://policy": kc.MAIN})

    def test_a_user_only_searches_the_collections_their_role_may_see(self):
        self.two()
        staff = identity.create_user("Sam", f"sam-{uuid.uuid4().hex[:6]}@example.com", "user")
        boss = identity.create_user("Ada", f"ada-{uuid.uuid4().hex[:6]}@example.com", "admin")
        self.assertEqual(kc.scope(staff["user_id"]), [kc.MAIN])
        self.assertIsNone(kc.scope(boss["user_id"]))                              # sees them all: no filter
        self.assertIsNone(kc.scope(None))                                         # a system run
        self.assertIs(kc.scope(staff["user_id"], "hr"), False)
        self.assertEqual(kc.scope(staff["user_id"], kc.MAIN), [kc.MAIN])
        self.assertIs(kc.scope(boss["user_id"], "nowhere"), False)
        brief = "Answers from: {collections}"
        self.assertNotIn("HR", kc.expand(brief, staff["user_id"]))
        self.assertIn("HR handbook (Staff travel and expenses.)", kc.expand(brief, boss["user_id"]))
        self.assertIn("HR handbook", kc.expand(brief, everyone=True))

    def test_the_search_filter_follows_the_collections(self):
        self.two()
        client = storage.get_client()
        storage.ensure_collection(client)
        vector = [1.0] + [0.0] * (config.EMBEDDING_DIM - 1)
        for url, text in (("https://rail.example/railcard", "A Railcard saves a third on fares."),
                          ("adhoc://policy", "Staff may claim a Railcard on expenses.")):
            storage.upsert_chunks(client, url, [text], [vector], {"source_url": url, "title": url}, 1)
        kc.assign(["adhoc://policy"], "hr")
        found = lambda within: sorted(h["payload"]["source_url"] for h in storage.search_text(
            client, "railcard", 10, {"collection": within} if within else None))
        self.assertEqual(found(None), ["adhoc://policy", "https://rail.example/railcard"])
        self.assertEqual(found(["hr"]), ["adhoc://policy"])
        self.assertEqual(found([kc.MAIN]), ["https://rail.example/railcard"])
        self.assertEqual(len(storage.search_dense(client, vector, 10, {"collection": ["hr"]})), 1)


def _run(user_id, tree=None):
    run = agent._Run.__new__(agent._Run)
    run.lock, run.searches, run.settings, run.emit = threading.Lock(), [], {}, (lambda e: None)
    run.user_id, run.agent_runs, run.tree = user_id, [], tree or {}
    run.retrieval_view = lambda search: {"chunks": search["rankings"]["fused"], "sources": []}
    return run


class Searching(unittest.TestCase):
    def test_a_knowledge_agent_searches_only_its_collection_and_never_one_hidden_from_the_user(self):
        spec = agent.flows.FlowSpec.model_validate(next(
            a for a in architectures([{"collection_id": "main", "name": "Rail", "description": "Trains."},
                                      {"collection_id": "hr", "name": "HR", "description": "Staff rules."}])
            if a["id"] == "knowledge_agents")["flow"])
        tree = agent.agent_tree.tree(spec)
        self.assertEqual(tree["kb_hr"].collection, "hr")
        run = _run("u_staff", tree)
        run.agent_runs = [{"n": 1, "node": "kb_hr", "parent": None}, {"n": 2, "node": "kb_main", "parent": None}]
        found = {"rankings": {"fused": [{"source_url": "https://rail.example", "text": "t", "meta": {}}]}}
        use = lambda: {"name": "search_knowledge_base", "input": '{"query": "railcard"}', "turn": 1, "toolUseId": "t"}
        scopes = {"hr": False, "main": ["main"], "": ["main"]}
        with patch.object(agent.knowledge_collections, "scope", side_effect=lambda user, wanted="": scopes[wanted]), \
                patch.object(agent, "hybrid_search", return_value=found) as search, \
                patch.object(agent, "_label_collections"):
            refused = run._search(use(), 1)
            self.assertIn("isn't available", refused["toolResult"]["content"][0]["text"])
            search.assert_not_called()
            run._search(use(), 2)
            self.assertEqual(search.call_args.kwargs["filters"], {"collection": ["main"]})
            self.assertEqual(run.visible_subagents("router"), ["kb_main"])          # HR isn't even offered

    def test_the_gallery_card_needs_two_collections(self):
        one = next(a for a in architectures([{"collection_id": "main", "name": "Rail"}]) if a["id"] == "knowledge_agents")
        self.assertFalse(one["ready"])
        two = next(a for a in architectures([{"collection_id": "main", "name": "Rail", "description": "Trains."},
                                              {"collection_id": "hr", "name": "HR"}]) if a["id"] == "knowledge_agents")
        spec = agent.flows.FlowSpec.model_validate(two["flow"])
        self.assertEqual([i for i in agent.flows.validate(spec) if i["level"] == "error"], [])
        router = next(n for n in spec.nodes if n.id == "router")
        self.assertIn(kc.TOKEN, router.config["brief"])                            # filled per user at run time
        guard = next(n for n in spec.nodes if n.type == "input_guardrail")
        self.assertEqual(guard.config["scope"], "")                                # follows the brief


if __name__ == "__main__":
    unittest.main()
