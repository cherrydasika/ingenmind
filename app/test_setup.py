"""Setup (the RAG Initialization Agent, epic #19): its state machine and its
conversation. Runs against its own database (needs the pgvector service).

    PYTHONPATH=app:dags python -m unittest -v app/test_setup.py
"""

import asyncio
import json
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg
from psycopg import sql

import knowledge_system
from common import config
import llm
from initialization import (blueprint, blueprint_run, build, content, conversation, evaluation, plan, requirements,
                            site_map, sources, sources_run, state, supervisor)
import flows
from common import scraping, storage
import guardrails


class SetupDatabase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_database = config.PGDATABASE
        cls.test_database = f"rag_test_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.test_database)))
        config.PGDATABASE = cls.test_database

    @classmethod
    def tearDownClass(cls):
        config.PGDATABASE = cls.original_database
        cls.reset_caches()
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=cls.original_database, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.test_database)))

    @staticmethod
    def reset_caches():
        knowledge_system.reset_schema_cache()
        conversation.reset_schema_cache()
        blueprint.reset_schema_cache()
        sources.reset_schema_cache()
        sources_run.reset_schema_cache()
        content.reset_schema_cache()
        plan.reset_schema_cache()
        evaluation.reset_schema_cache()
        flows.evals.reset_schema_cache()
        flows.store.reset_schema_cache()

    def setUp(self):
        """A fresh, empty install: not set up, no URL list."""
        with psycopg.connect(host=config.PGHOST, port=config.PGPORT, user=config.PGUSER,
                             password=config.PGPASSWORD, dbname=self.test_database, autocommit=True) as connection:
            for table in ("app_knowledge_system", "app_knowledge_system_events", "kb_urls", "app_setup_turns",
                          "app_setup", "app_domain_blueprints", "kb_sources",
                          "app_source_discoveries", "kb_site_analyses", "kb_content", "kb_ingestion_plan_pages",
                          "kb_ingestion_plans", "rag_chunks", "ingestion_jobs", "app_setup_evaluations",
                          "agent_eval_results", "agent_eval_runs", "agent_eval_sets", "agent_flow_versions",
                          "agent_live_flow", "agent_flow_runs", "agent_flows"):
                connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table)))
        self.reset_caches()
        with patch.object(config, "URLS_CONFIG_PATH", Path("/nonexistent/urls.json")):
            knowledge_system.ensure_schema()

    def walk(self, to: str) -> None:
        """Move forward, one allowed step at a time, until `to`."""
        while knowledge_system.status()["state"] != to:
            state.transition(state.FORWARD[knowledge_system.status()["state"]], "admin-1")


class StateMachine(SetupDatabase):
    def test_setup_moves_forward_one_state_at_a_time(self):
        self.assertEqual(knowledge_system.status()["state"], state.NEW)
        self.assertEqual(state.transition(state.DISCOVERING_DOMAIN, "admin-1"), state.NEW)
        with self.assertRaises(state.TransitionNotAllowed):          # no skipping ahead
            state.transition(state.INGESTING, "admin-1")
        with self.assertRaises(state.TransitionNotAllowed):
            state.transition("SOMEWHERE", "admin-1")
        self.walk(state.READY)
        status = knowledge_system.status()
        self.assertEqual((status["state"], status["origin"], status["set_up_by"]), (state.READY, "setup", "admin-1"))
        moves = [(e["details"]["from"], e["details"]["to"]) for e in reversed(knowledge_system.events())
                 if e["event"] == "state"]
        self.assertEqual(moves, list(zip(state.STATES, state.STATES[1:])))

    def test_setup_can_go_back_to_change_requirements_or_choices(self):
        self.walk(state.AWAITING_CONTENT_SELECTION)
        state.transition(state.CLARIFYING, "admin-1")
        self.walk(state.EVALUATING)
        state.transition(state.AWAITING_SOURCE_SELECTION, "admin-1")    # fix a gap the evaluation showed
        self.walk(state.READY)
        state.transition(state.AWAITING_CONTENT_SELECTION, "admin-1")   # built by setup: may go back once live
        self.walk(state.INGESTING)
        with self.assertRaises(state.TransitionNotAllowed):             # too late to change requirements mid-build
            state.transition(state.CLARIFYING, "admin-1")

    def test_an_install_setup_did_not_build_cannot_go_back(self):
        knowledge_system.mark_ready("existing")
        for to in (state.AWAITING_SOURCE_SELECTION, state.AWAITING_CONTENT_SELECTION, state.CLARIFYING):
            with self.assertRaises(state.TransitionNotAllowed):
                state.transition(to, "admin-1")

    def test_a_transition_from_a_state_that_moved_on_is_refused(self):
        with self.assertRaises(state.TransitionNotAllowed):
            state.transition(state.DISCOVERING_DOMAIN, expected=state.CLARIFYING)
        # Compare-and-set: a second request moving the same state loses.
        self.assertTrue(knowledge_system.set_state(state.NEW, state.DISCOVERING_DOMAIN))
        self.assertFalse(knowledge_system.set_state(state.NEW, state.DISCOVERING_DOMAIN))

    def test_every_state_belongs_to_one_user_facing_step(self):
        self.assertEqual([state.step(s) for s in state.STATES],
                         ["purpose"] * 3 + ["blueprint"] + ["sources"] * 2 + ["content"] * 2 + ["build"] * 4 + ["live"])


class Conversation(SetupDatabase):
    def test_turns_are_kept_in_order_with_their_state(self):
        conversation.add_turn("agent", "What would you like your knowledge system to help users with?", state.NEW)
        conversation.add_turn("user", "  Information about trains.  ", state.DISCOVERING_DOMAIN, trace_id="t1")
        conversation.add_turn("agent", "Which country should it cover?", state.CLARIFYING)
        turns = conversation.turns()
        self.assertEqual([(t["role"], t["state"]) for t in turns],
                         [("agent", "NEW"), ("user", "DISCOVERING_DOMAIN"), ("agent", "CLARIFYING")])
        self.assertEqual((turns[1]["text"], turns[1]["trace_id"]), ("Information about trains.", "t1"))
        self.assertEqual([t["text"] for t in conversation.turns(last=1)], ["Which country should it cover?"])
        for role, text in (("system", "x"), ("user", "   ")):
            with self.assertRaises(ValueError):
                conversation.add_turn(role, text, state.NEW)

    def test_requirements_are_saved_and_confirmed_only_when_complete(self):
        self.assertEqual(conversation.requirements()["requirements"], {})
        conversation.save_requirements({"purpose": "UK train information"}, complete=False)
        with self.assertRaises(ValueError):
            conversation.confirm("admin-1")
        conversation.save_requirements({"purpose": "UK train information", "country": "United Kingdom"}, complete=True)
        conversation.confirm("admin-1")
        saved = conversation.requirements()
        self.assertEqual((saved["requirements"]["country"], saved["complete"], saved["confirmed_by"]),
                         ("United Kingdom", True, "admin-1"))
        conversation.save_requirements({"purpose": "UK and Irish trains"}, complete=True)   # a change: confirm again
        self.assertIsNone(conversation.requirements()["confirmed_at"])

    def test_a_reset_clears_the_setup_conversation(self):
        conversation.add_turn("user", "Flights.", state.DISCOVERING_DOMAIN)
        conversation.save_requirements({"purpose": "flights"}, complete=False)
        knowledge_system.mark_ready("existing")
        deleted = knowledge_system.reset("RESET", "admin-1")
        self.assertEqual((deleted["app_setup_turns"], deleted["app_setup"]), (1, 1))
        self.assertEqual((conversation.turns(), conversation.requirements()["requirements"]), ([], {}))



def scripted(*turns):
    """A stand-in for llm.chat: each call returns the next scripted turn and
    records what it was sent. A turn is text, or ("tool", name, input)."""
    calls = []

    def chat(system, messages, tools=None, **kwargs):
        calls.append({"system": system, "messages": [dict(m) for m in messages]})
        step = turns[len(calls) - 1]
        if isinstance(step, Exception):
            raise step
        if isinstance(step, tuple):
            _, name, args = step
            return llm.Turn([{"toolUse": {"toolUseId": f"t{len(calls)}", "name": name, "input": args}}],
                            "tool_use", 10, 5)
        return llm.Turn([{"text": step}], "end_turn", 10, 5)
    chat.calls = calls
    return chat


COMPLETE = {"purpose": "Answer passengers' questions about UK trains", "audience": ["general passengers"],
            "regions": ["United Kingdom"], "question_types": ["tickets", "refunds", "accessibility"]}


class Supervisor(SetupDatabase):
    def test_the_first_answer_starts_clarifying(self):
        chat = scripted(("tool", "record_requirements", {"purpose": "Information about trains"}),
                        "Which country or countries should it cover?")
        view = supervisor.respond("Information about trains.", "admin-1", chat=chat)
        self.assertEqual((view["state"], view["step"]), (state.CLARIFYING, "purpose"))
        self.assertEqual([(t["role"], t["text"]) for t in view["turns"]],
                         [("agent", supervisor.OPENING), ("user", "Information about trains."),
                          ("agent", "Which country or countries should it cover?")])
        self.assertEqual(view["requirements"]["purpose"], "Information about trains")
        self.assertIn("which countries or regions it covers", view["missing"])
        # The model saw the user's message first (the opening is in the system prompt) and the saved requirements.
        first = chat.calls[0]
        self.assertEqual([m["role"] for m in first["messages"]], ["user"])
        self.assertIn(supervisor.OPENING, first["system"])
        self.assertIn("Information about trains", chat.calls[1]["system"])        # after the tool call
        self.assertEqual(chat.calls[1]["messages"][-1]["content"][0]["toolResult"]["status"], "success")
        moves = [(e["details"]["from"], e["details"]["to"]) for e in reversed(knowledge_system.events())
                 if e["event"] == "state"]
        self.assertEqual(moves, [(state.NEW, state.DISCOVERING_DOMAIN), (state.DISCOVERING_DOMAIN, state.CLARIFYING)])

    def test_text_written_alongside_a_tool_call_is_the_reply(self):
        # Seen live: the question came with the tool call; after the result the model said nothing.
        def chat(system, messages, tools=None, **kwargs):
            if len(messages) == 1:
                return llm.Turn([{"text": "Which country should it cover?"},
                                 {"toolUse": {"toolUseId": "t1", "name": "record_requirements",
                                              "input": {"purpose": "Trains"}}}], "tool_use", 10, 5)
            return llm.Turn([], "end_turn", 10, 0)
        view = supervisor.respond("Information about trains.", chat=chat)
        self.assertEqual(view["turns"][-1]["text"], "Which country should it cover?")
        self.assertEqual(view["requirements"]["purpose"], "Trains")

    def test_complete_is_refused_while_something_required_is_missing(self):
        chat = scripted(("tool", "record_requirements", {"purpose": "Trains"}), ("tool", "requirements_complete", {}),
                        "Who will ask the questions?")
        view = supervisor.respond("Trains in the UK.", chat=chat)
        refusal = chat.calls[2]["messages"][-1]["content"][0]["toolResult"]
        self.assertEqual(refusal["status"], "error")
        self.assertIn("who will ask the questions", refusal["content"][0]["text"])
        self.assertFalse(view["complete"])

    def test_requirements_merge_and_the_transcript_is_sent_each_turn(self):
        supervisor.respond("Information about trains.", chat=scripted(
            ("tool", "record_requirements", {"purpose": "Trains", "regions": ["United Kingdom"]}), "Who will ask?"))
        chat = scripted(("tool", "record_requirements", {"audience": "general passengers"}), "What questions?")
        view = supervisor.respond("Passengers.", chat=chat)
        self.assertEqual((view["requirements"]["regions"], view["requirements"]["audience"]),
                         (["United Kingdom"], ["general passengers"]))   # kept, and a string read as a list
        self.assertEqual([m["content"][0]["text"] for m in chat.calls[0]["messages"]],
                         ["Information about trains.", "Who will ask?", "Passengers."])

    def test_confirming_complete_requirements_moves_on_to_the_blueprint(self):
        view = supervisor.respond("UK trains for passengers: tickets, refunds, accessibility.", chat=scripted(
            ("tool", "record_requirements", COMPLETE), ("tool", "requirements_complete", {}),
            "- UK trains\n- general passengers\nPress Looks right or say what to change."))
        self.assertTrue(view["complete"])
        self.assertEqual(view["missing"], [])
        view = supervisor.confirm("admin-1")
        self.assertEqual((view["state"], view["step"]), (state.DOMAIN_READY, "blueprint"))
        self.assertEqual(view["turns"][-1]["text"], supervisor.CONFIRMED)
        self.assertIsNotNone(view["confirmed_at"])
        event = knowledge_system.events()[0]
        self.assertEqual((event["details"]["to"], event["details"]["requirements"]["regions"]),
                         (state.DOMAIN_READY, ["United Kingdom"]))
        with self.assertRaises(supervisor.NotConversing):                 # the conversation is over
            supervisor.respond("Actually, flights.", chat=scripted("x"))
        with self.assertRaises(supervisor.NotConversing):
            supervisor.confirm("admin-1")

    def test_requirements_are_complete_once_nothing_required_is_missing(self):
        # The model summarised without calling requirements_complete (seen live): still confirmable.
        chat = scripted(("tool", "record_requirements", COMPLETE), "- UK trains, passengers… Does that look right?")
        view = supervisor.respond("UK trains for passengers: tickets, refunds, accessibility.", chat=chat)
        self.assertTrue(view["complete"])
        self.assertIn("ask the user to confirm", chat.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"][0]["text"])
        self.assertEqual(supervisor.confirm()["state"], state.DOMAIN_READY)

    def test_confirming_incomplete_requirements_is_refused(self):
        supervisor.respond("Trains.", chat=scripted("Which country?"))
        with self.assertRaises(ValueError):
            supervisor.confirm("admin-1")
        self.assertEqual(knowledge_system.status()["state"], state.CLARIFYING)

    def test_a_set_up_knowledge_system_has_no_conversation(self):
        knowledge_system.mark_ready("existing")
        with self.assertRaises(supervisor.NotConversing):
            supervisor.respond("Trains.", chat=scripted("x"))
        with self.assertRaises(ValueError):
            supervisor.respond("   ", chat=scripted("x"))

    def test_a_failed_model_call_leaves_setup_resumable(self):
        with self.assertRaises(RuntimeError):
            supervisor.respond("Trains.", chat=scripted(RuntimeError("model down")))
        self.assertEqual(knowledge_system.status()["state"], state.DISCOVERING_DOMAIN)
        chat = scripted("Which country?")
        view = supervisor.respond("In the UK.", chat=chat)
        self.assertEqual(view["state"], state.CLARIFYING)
        self.assertEqual(chat.calls[0]["messages"], [{"role": "user", "content": [{"text": "Trains.\n\nIn the UK."}]}])

    def test_a_turn_out_of_tool_calls_still_answers(self):
        chat = scripted(*[("tool", "record_requirements", {"purpose": "Trains"})] * supervisor.MAX_TOOL_TURNS)
        view = supervisor.respond("Trains.", chat=chat)
        self.assertEqual(len(chat.calls), supervisor.MAX_TOOL_TURNS)
        self.assertTrue(view["turns"][-1]["text"])

    def test_requirements_take_only_known_fields(self):
        merged = requirements.merge({"purpose": "Trains"}, {"regions": "UK", "budget": "high", "purpose": " Rail "})
        self.assertEqual((merged.purpose, merged.regions), ("Rail", ["UK"]))
        self.assertNotIn("budget", merged.model_dump())


class SetupApi(SetupDatabase):
    class Request:
        def __init__(self, body=None):
            self.state = Mock(user={"user_id": "admin-1", "status": "active", "permissions": ["manage_settings"]})
            self.body = body or {}

        async def json(self):
            return self.body

    def call(self, handler, body=None):
        response = asyncio.run(handler(self.Request(body)))
        return response.status_code, json.loads(response.body)

    def test_a_turn_through_the_api(self):
        import web_api
        status, view = 200, json.loads(web_api.setup_view(self.Request()).body)
        self.assertEqual((view["state"], view["turns"][0]["text"]), (state.NEW, supervisor.OPENING))
        with patch.object(supervisor.llm, "chat", scripted("Which country should it cover?")):
            status, view = self.call(web_api.setup_message, {"text": "Trains."})
        self.assertEqual((status, view["state"], view["turns"][-1]["text"]),
                         (200, state.CLARIFYING, "Which country should it cover?"))
        self.assertEqual(knowledge_system.events()[0]["user_id"], "admin-1")

    def test_api_errors(self):
        import web_api
        for body in ({}, {"text": "   "}, {"text": 5}, {"text": "x" * 4001}):
            self.assertEqual(self.call(web_api.setup_message, body)[0], 400, body)
        with patch.object(supervisor.llm, "chat", side_effect=RuntimeError("model down")):
            status, error = self.call(web_api.setup_message, {"text": "Trains."})
        self.assertEqual(status, 502)
        self.assertIn("send it again", error["error"])
        self.assertEqual(self.call(web_api.setup_confirm)[0], 409)          # not clarifying yet (discovering)
        knowledge_system.set_state(state.DISCOVERING_DOMAIN, state.CLARIFYING)
        self.assertEqual(self.call(web_api.setup_confirm)[0], 400)          # nothing complete to confirm
        knowledge_system.mark_ready("existing")
        self.assertEqual(self.call(web_api.setup_message, {"text": "Trains."})[0], 409)


def page(url, text="Facts about the domain. " * 40):
    return {"url": url, "title": url.split("/")[2], "text": text, "chars": len(text), "error": None}


def blueprint_for(domain: str, live: str, static: str, regulator: str, evidence: str) -> dict:
    return {"name": domain, "category": "transportation", "regions": ["UK"], "audience": ["passengers"],
            "purpose": f"Answer questions about {domain}",
            "knowledge_areas": [
                {"key": static, "name": static.title(), "description": "Rules", "knowledge_class": "STATIC_KNOWLEDGE",
                 "example_questions": [f"What are the {static} rules?"], "evidence_urls": [evidence]},
                {"key": live, "name": live.title(), "description": "Now", "knowledge_class": "live data",
                 "evidence_urls": ["https://never-fetched.example/x"]}],
            "entities": [{"name": "operator"}], "metadata_fields": [{"field": "topic", "examples": "refunds"}],
            "organisations": [
                {"name": regulator, "role": "Regulator", "website": "https://" + evidence.split("/")[2],
                 "evidence_urls": [evidence]},
                {"name": "Invented Body", "role": "other", "website": "https://invented.example"}],
            "flow": {"brief": "B" * 1500, "search_country": "", "domain": f"{domain} " * 20,
                     "scope": "In: rules. Out: live data.", "supervisor_instructions": "Rules to the knowledge base."},
            "unknowns": ["exact fares"]}


class Blueprint(SetupDatabase):
    def stubs(self, raw: dict, pages: dict):
        searched = []

        def search(query, max_results, country):
            searched.append((query, country))
            return [{"url": u, "title": "t", "score": 0.5} for u in pages]

        def fetch(url):
            return pages[url]

        def planner(req):
            return blueprint.ResearchPlan(queries=["q1", "q2"], search_country="united kingdom"), {}

        def writer(req, fetched, feedback=None, previous=None):
            self.written_from = [p["url"] for p in fetched]
            return blueprint.DomainBlueprint.model_validate(blueprint._normalise(raw)), {}
        return dict(planner=planner, writer=writer, search=search, fetch=fetch), searched

    def test_two_domains_through_the_same_schema(self):
        for domain, live, static, regulator, site in (
                ("UK rail", "live_departures", "refunds", "Office of Rail and Road", "https://www.orr.gov.uk/rights"),
                ("UK flights", "flight_status", "baggage", "Civil Aviation Authority", "https://www.caa.co.uk/rights")):
            pages = {site: page(site), "https://blog.example.com/post": page("https://blog.example.com/post")}
            kw, searched = self.stubs(blueprint_for(domain, live, static, regulator, site), pages)
            bp, record = blueprint.generate(requirements.SetupRequirements(purpose=domain, regions=["UK"]), **kw)
            areas = {a.key: a for a in bp.knowledge_areas}
            self.assertEqual(areas[static].knowledge_class, "STATIC_KNOWLEDGE")
            self.assertEqual(areas[live].knowledge_class, "STATIC_KNOWLEDGE")      # unknown class → static
            self.assertEqual(areas[live].evidence_urls, [])                       # not a fetched page: dropped
            self.assertEqual([o.name for o in bp.organisations], [regulator])     # invented body dropped …
            self.assertIn("Invented Body: named without a fetched page", bp.unknowns)   # … and said so
            self.assertEqual((bp.organisations[0].role, bp.organisations[0].website),
                             ("regulator", "https://" + site.split("/")[2]))
            self.assertEqual((len(bp.flow.brief), len(bp.flow.domain) <= 60), (1000, True))
            self.assertEqual(bp.flow.search_country, "united kingdom")           # from the plan when left blank
            self.assertEqual(searched, [("q1", "united kingdom"), ("q2", "united kingdom")])
            self.assertEqual(self.written_from[0], site)                          # official site first
            self.assertEqual(record["queries"], ["q1", "q2"])

    def test_gather_ranks_official_sites_skips_bad_pages_and_caps(self):
        pages = {f"https://site{i}.example/p": page(f"https://site{i}.example/p") for i in range(12)}
        pages["https://www.gov.uk/rules"] = page("https://www.gov.uk/rules")
        pages["https://site0.example/p"] = {**page("https://site0.example/p"), "error": "HTTPError: 404"}
        pages["https://site1.example/p"] = page("https://site1.example/p", text="short")

        def search(query, max_results, country):
            if query == "broken":
                raise RuntimeError("search down")
            return [{"url": u, "score": 0.1} for u in pages]
        gathered = blueprint.gather(blueprint.ResearchPlan(queries=["broken", "rules"]), search, pages.get)
        urls = [p["url"] for p in gathered["pages"]]
        self.assertEqual(urls[0], "https://www.gov.uk/rules")
        self.assertEqual(len(urls), blueprint.MAX_PAGES)
        self.assertNotIn("https://site0.example/p", urls)
        self.assertEqual({s["url"] for s in gathered["skipped"]}, {"https://site0.example/p", "https://site1.example/p"})
        self.assertIn("search down", gathered["searches"][0]["error"])

    def test_versions_are_stored_one_research_at_a_time(self):
        first = blueprint.start_version()
        with self.assertRaises(RuntimeError):
            blueprint.start_version()
        with self.assertRaises(ValueError):
            blueprint.confirm(first, "admin-1")                  # still researching
        blueprint.fail(first, "Tavily down")
        second = blueprint.start_version(feedback="add flights")
        bp = blueprint.DomainBlueprint.model_validate(blueprint._normalise(
            blueprint_for("UK rail", "live", "refunds", "ORR", "https://www.orr.gov.uk/x")))
        blueprint.finish(second, bp, {"queries": ["q"]})
        latest = blueprint.latest()
        self.assertEqual((latest["version"], latest["status"], latest["feedback"]), (second, "ready", "add flights"))
        self.assertEqual(blueprint.get(first)["error"], "Tavily down")
        blueprint.confirm(second, "admin-1")
        self.assertEqual(blueprint.get(second)["confirmed_by"], "admin-1")
        knowledge_system.mark_ready("existing")
        self.assertEqual(knowledge_system.reset("RESET", "admin-1")["app_domain_blueprints"], 2)
        self.assertIsNone(blueprint.latest())

    def test_nested_fields_sent_as_text_are_read(self):
        raw = blueprint_for("UK rail", "live", "refunds", "ORR", "https://www.orr.gov.uk/x")
        raw = {**raw, "knowledge_areas": json.dumps(raw["knowledge_areas"]), "flow": json.dumps(raw["flow"])}
        bp = blueprint.DomainBlueprint.model_validate(blueprint._normalise(raw))
        self.assertEqual((len(bp.knowledge_areas), bp.flow.scope), (2, "In: rules. Out: live data."))


UK_RAIL = blueprint_for("UK rail", "live_departures", "refunds", "Office of Rail and Road", "https://www.orr.gov.uk/x")


class BlueprintStep(SetupDatabase):
    """Running the blueprint step: research in the background (inline here), revise, confirm."""

    def setUp(self):
        super().setUp()
        inline = patch.object(blueprint_run, "_start_thread", side_effect=lambda work: work())
        inline.start()
        self.addCleanup(inline.stop)
        self.generated = []
        self.generator = patch.object(blueprint, "generate", side_effect=self.generate)
        self.generator.start()
        self.addCleanup(self.generator.stop)
        knowledge_system.set_state(state.NEW, state.DISCOVERING_DOMAIN)
        knowledge_system.set_state(state.DISCOVERING_DOMAIN, state.CLARIFYING)
        conversation.save_requirements(COMPLETE, complete=True)

    def generate(self, req):
        self.generated.append(req.purpose)
        if getattr(self, "fail_next", False):
            self.fail_next = False
            raise RuntimeError("Tavily down")
        page_url = "https://www.orr.gov.uk/x"
        return (blueprint.check(blueprint.DomainBlueprint.model_validate(blueprint._normalise(UK_RAIL)),
                                [page(page_url)]),
                {"queries": ["UK rail regulator"], "pages": [{"url": page_url, "title": "ORR", "chars": 900}]})

    def test_confirming_the_requirements_starts_the_research(self):
        view = supervisor.confirm("admin-1")
        self.assertEqual((view["state"], view["step"]), (state.DOMAIN_READY, "blueprint"))
        self.assertEqual(self.generated, [COMPLETE["purpose"]])
        bp = view["blueprint"]
        self.assertEqual((bp["version"], bp["status"], bp["blueprint"]["name"]), (1, "ready", "UK rail"))
        self.assertEqual(bp["research"], {"queries": ["UK rail regulator"],
                                          "pages": [{"url": "https://www.orr.gov.uk/x", "title": "ORR", "chars": 900}]})

    def test_a_failed_research_can_be_started_again(self):
        self.fail_next = True
        view = supervisor.confirm("admin-1")
        self.assertEqual((view["blueprint"]["status"], view["blueprint"]["error"]), ("failed", "RuntimeError: Tavily down"))
        blueprint_run.start()
        self.assertEqual(supervisor.view()["blueprint"]["status"], "ready")

    def test_research_cut_off_by_a_restart_expires(self):
        supervisor.confirm("admin-1")
        with blueprint._connect() as connection:
            connection.execute("UPDATE app_domain_blueprints SET status = 'researching', "
                               "created_at = now() - interval '1 hour'")
        view = supervisor.view()
        self.assertEqual((view["blueprint"]["status"], view["blueprint"]["error"]),
                         ("failed", "interrupted: please start again"))
        self.assertEqual(blueprint_run.start(), 2)

    def test_revising_rewrites_from_the_same_pages_without_searching(self):
        supervisor.confirm("admin-1")
        fetched, written = [], []

        def writer(req, pages, feedback=None, previous=None):
            written.append((feedback, previous["name"], [p["url"] for p in pages]))
            return blueprint.DomainBlueprint.model_validate(
                blueprint._normalise({**UK_RAIL, "name": "UK rail and Eurostar"})), {}
        with patch.object(blueprint.research, "web_search", side_effect=AssertionError("no new searches")):
            blueprint_run.revise("Add Eurostar.", writer=writer, fetch=lambda url: fetched.append(url) or page(url))
        bp = supervisor.view()["blueprint"]
        self.assertEqual((bp["version"], bp["status"], bp["feedback"], bp["blueprint"]["name"]),
                         (2, "ready", "Add Eurostar.", "UK rail and Eurostar"))
        self.assertEqual(written, [("Add Eurostar.", "UK rail", ["https://www.orr.gov.uk/x"])])
        self.assertEqual(fetched, ["https://www.orr.gov.uk/x"])
        with self.assertRaises(ValueError):
            blueprint_run.revise("   ")

    def test_confirming_the_blueprint_moves_on_to_sources(self):
        supervisor.confirm("admin-1")
        self.assertEqual(blueprint_run.confirm("admin-1"), 1)
        view = supervisor.view()
        self.assertEqual((view["state"], view["step"]), (state.DISCOVERING_SOURCES, "sources"))
        self.assertEqual(knowledge_system.status()["blueprint_version"], 1)
        self.assertIsNotNone(view["blueprint"]["confirmed_at"])
        self.assertEqual(view["turns"][-1]["text"], blueprint_run.CONFIRMED)
        with self.assertRaises(state.TransitionNotAllowed):          # the step is over
            blueprint_run.confirm("admin-1")

    def test_nothing_to_confirm_while_researching_or_failed(self):
        self.fail_next = True
        supervisor.confirm("admin-1")
        with self.assertRaises(ValueError):
            blueprint_run.confirm("admin-1")
        self.assertEqual(knowledge_system.status()["state"], state.DOMAIN_READY)

    def test_going_back_to_change_the_requirements(self):
        supervisor.confirm("admin-1")
        blueprint_run.back_to_conversation("admin-1")
        self.assertEqual(knowledge_system.status()["state"], state.CLARIFYING)
        conversation.save_requirements({**COMPLETE, "regions": ["United Kingdom", "Ireland"]}, complete=True)
        view = supervisor.confirm("admin-1")                           # confirmed again: a new blueprint
        self.assertEqual((view["state"], view["blueprint"]["version"], len(self.generated)), (state.DOMAIN_READY, 2, 2))
        with self.assertRaises(ValueError):                            # unconfirmed requirements: no research
            knowledge_system.set_state(state.DOMAIN_READY, state.CLARIFYING)
            conversation.save_requirements(COMPLETE, complete=True)
            knowledge_system.set_state(state.CLARIFYING, state.DOMAIN_READY)
            blueprint_run.start()

    def test_the_blueprint_api(self):
        import web_api
        api = SetupApi()
        self.assertEqual(api.call(web_api.setup_blueprint)[0], 409)    # not in the blueprint step yet
        supervisor.confirm("admin-1")
        for body in ({}, {"feedback": " "}, {"feedback": "x" * 2001}):
            self.assertEqual(api.call(web_api.setup_blueprint_revise, body)[0], 400, body)
        status, view = api.call(web_api.setup_blueprint)                # start again: a new version
        self.assertEqual((status, view["blueprint"]["version"]), (200, 2))
        status, view = api.call(web_api.setup_blueprint_confirm)
        self.assertEqual((status, view["state"]), (200, state.DISCOVERING_SOURCES))
        self.assertEqual(api.call(web_api.setup_back)[0], 409)          # past the blueprint step


RAIL_BLUEPRINT = {
    "name": "UK rail", "category": "transportation", "purpose": "Answer passengers' questions", "regions": ["UK"],
    "audience": ["passengers"],
    "knowledge_areas": [
        {"key": "refunds", "name": "Refunds", "description": "Refund rules", "knowledge_class": "STATIC_KNOWLEDGE",
         "evidence_urls": ["https://www.orr.gov.uk/refunds"]},
        {"key": "accessibility", "name": "Accessibility", "description": "Help", "knowledge_class": "STATIC_KNOWLEDGE",
         "evidence_urls": []},
        {"key": "live_departures", "name": "Live departures", "description": "Now",
         "knowledge_class": "DYNAMIC_KNOWLEDGE", "evidence_urls": []}],
    "organisations": [{"name": "Office of Rail and Road", "role": "regulator", "website": "https://www.orr.gov.uk"},
                      {"name": "Trainline", "role": "other", "website": "https://www.thetrainline.com"}],
    "source_requirements": {"authoritative_only": True},
    "flow": {"domain": "UK train information", "search_country": "united kingdom"},
}


class SourceDiscovery(SetupDatabase):
    def discover_with(self, assessed, results=None):
        searched, seen = [], []

        def search(query, max_results, country):
            searched.append((query, country))
            return results if results is not None else [
                {"url": "https://www.nationalrail.co.uk/refunds/", "title": "Refunds", "snippet": "Official"},
                {"url": "https://www.reddit.com/r/uktrains/x", "title": "Thread"},
                {"url": "https://www.britishairways.com/delays", "title": "Flight delays"},
                {"url": "https://blog.example.com/post", "title": "My tips"}]

        def assessor(summary, candidates):
            seen.append((summary, {c["host"]: c for c in candidates}))
            return sources.SiteAssessments(sites=[sources.SiteAssessment.model_validate(a) for a in assessed]), {}
        sites, record = sources.discover(RAIL_BLUEPRINT, search=search, assessor=assessor)
        return {s["host"]: s for s in sites}, record, searched, seen

    def test_discovery_judges_sites_and_applies_the_rules(self):
        sites, record, searched, seen = self.discover_with([
            {"host": "nationalrail.co.uk", "name": "National Rail", "authority": "high", "relevance": 0.8,
             "areas": ["refunds", "live_departures", "made_up"], "reason": "Official", "recommended": True},
            {"host": "reddit.com", "name": "Reddit", "authority": "medium", "relevance": 0.5,
             "areas": ["refunds"], "reason": "Many answers", "recommended": True},
            {"host": "britishairways.com", "name": "BA", "authority": "high", "relevance": 0.0,
             "areas": [], "reason": "Flights", "recommended": False},
            {"host": "blog.example.com", "name": "Blog", "authority": "low", "relevance": 0.4,
             "areas": ["refunds"], "reason": "Tips", "recommended": True},
            {"host": "orr.gov.uk", "name": "ORR", "authority": "medium", "relevance": 0.3, "areas": ["refunds"],
             "reason": "Regulator", "recommended": False}])
        # Searches name the domain and the region, one per static area; live areas are not sourced.
        self.assertEqual(searched, [("UK train information Refunds UK official", "united kingdom"),
                                    ("UK train information Accessibility UK official", "united kingdom")])
        self.assertEqual(record["queries"], [q for q, _ in searched])
        self.assertNotIn("live_departures", seen[0][0])
        # The blueprint's evidence on a seed's site is shown to the assessor.
        self.assertIn({"area": "refunds", "url": "https://www.orr.gov.uk/refunds",
                       "title": "Blueprint evidence for Refunds"}, seen[0][1]["orr.gov.uk"]["results"])
        self.assertEqual((sites["nationalrail.co.uk"]["areas"], sites["nationalrail.co.uk"]["recommended"]),
                         (["refunds"], True))                                     # unknown and live areas dropped
        self.assertFalse(sites["reddit.com"]["recommended"])                       # forums never
        self.assertNotIn("britishairways.com", sites)                              # another domain: dropped
        self.assertFalse(sites["blog.example.com"]["recommended"])                 # low authority, authoritative only
        self.assertEqual((sites["orr.gov.uk"]["authority"], sites["orr.gov.uk"]["recommended"]),
                         ("high", True))                                           # official domain, blueprint regulator
        self.assertEqual(sites["orr.gov.uk"]["origin"], "blueprint")
        # A blueprint organisation the assessor left out stays, not recommended unless its role is high.
        self.assertEqual((sites["thetrainline.com"]["recommended"], sites["thetrainline.com"]["authority"]),
                         (False, "medium"))
        self.assertEqual(list(sites)[0], "nationalrail.co.uk")                    # recommended and high first

    def test_at_most_fifteen_sites_are_kept(self):
        results = [{"url": f"https://site{i}.gov.uk/x", "title": "t"} for i in range(20)]
        assessed = [{"host": f"site{i}.gov.uk", "name": f"S{i}", "authority": "high", "relevance": 0.5,
                     "areas": ["refunds"], "reason": "r", "recommended": True} for i in range(20)]
        sites, *_ = self.discover_with(assessed, results)
        self.assertEqual(len(sites), sources.MAX_SITES)

    def test_the_registry_keeps_the_users_choices_across_discoveries(self):
        sites, *_ = self.discover_with([{"host": "nationalrail.co.uk", "name": "National Rail", "authority": "high",
                               "relevance": 0.8, "areas": ["refunds"], "reason": "Official", "recommended": True}])
        sources.save_discovered(list(sites.values()))
        listed = {s["host"]: s for s in sources.list_sources()}
        self.assertEqual(set(listed), {"nationalrail.co.uk", "orr.gov.uk", "thetrainline.com"})
        sources.set_status(listed["nationalrail.co.uk"]["source_id"], "selected")
        sources.set_status(listed["thetrainline.com"]["source_id"], "removed")
        with self.assertRaises(ValueError):
            sources.set_status(listed["orr.gov.uk"]["source_id"], "approved")
        with self.assertRaises(LookupError):
            sources.set_status(999999, "selected")
        sources.save_discovered([])                       # a new discovery: candidates replaced, choices kept
        kept = {s["host"]: s["status"] for s in sources.list_sources(include_removed=True)}
        self.assertEqual(kept, {"nationalrail.co.uk": "selected", "thetrainline.com": "removed"})
        self.assertEqual([s["host"] for s in sources.list_sources()], ["nationalrail.co.uk"])
        cover = {c["key"]: c["sources"] for c in sources.coverage(RAIL_BLUEPRINT, sources.list_sources())}
        self.assertEqual(cover, {"refunds": ["National Rail"], "accessibility": []})
        knowledge_system.mark_ready("existing")
        self.assertEqual(knowledge_system.reset("RESET", "admin-1")["kb_sources"], 2)


SITES = [{"host": "nationalrail.co.uk", "name": "National Rail", "base_url": "https://www.nationalrail.co.uk",
          "kind": "website", "origin": "search", "authority": "high", "relevance": 0.8, "areas": ["refunds"],
          "reason": "Official", "recommended": True, "evidence": []},
         {"host": "seat61.com", "name": "Seat61", "base_url": "https://www.seat61.com", "kind": "website",
          "origin": "search", "authority": "low", "relevance": 0.3, "areas": ["refunds"], "reason": "A blog",
          "recommended": False, "evidence": []}]


class SourcesStep(SetupDatabase):
    """The Sources step: discovery in the background (inline here), choosing, adding, continuing."""

    def setUp(self):
        super().setUp()
        inline = patch.object(sources_run, "_start_thread", side_effect=lambda work: work())
        inline.start()
        self.addCleanup(inline.stop)
        version = blueprint.start_version()
        blueprint.finish(version, blueprint.DomainBlueprint.model_validate(blueprint._normalise(
            {**RAIL_BLUEPRINT, "flow": {**RAIL_BLUEPRINT["flow"], "brief": "UK trains", "scope": "UK rail only",
                                        "supervisor_instructions": "x"}})), {})
        blueprint.confirm(version, "admin-1")
        knowledge_system.set_blueprint_version(version)
        for a, b in zip(state.STATES, state.STATES[1:5]):
            knowledge_system.set_state(a, b)          # NEW … DISCOVERING_SOURCES
        self.discovered = 0

    def discover(self, bp):
        self.discovered += 1
        if getattr(self, "fail_next", False):
            self.fail_next = False
            raise RuntimeError("Tavily down")
        return [dict(s) for s in SITES], {"queries": ["q"]}

    def found(self):
        sources_run.start(discover=self.discover)
        return {s["host"]: s for s in supervisor.view()["sources"]["sources"]}

    def test_discovery_moves_setup_to_choosing(self):
        listed = self.found()
        view = supervisor.view()
        self.assertEqual((view["state"], view["step"]), (state.AWAITING_SOURCE_SELECTION, "sources"))
        self.assertEqual(set(listed), {"nationalrail.co.uk", "seat61.com"})
        self.assertTrue(all(s["status"] == "candidate" for s in listed.values()))     # suggested, not selected
        self.assertEqual(view["sources"]["run"]["status"], "ready")
        self.assertEqual(view["sources"]["live_areas"], ["Live departures"])
        self.assertEqual([c["key"] for c in view["sources"]["coverage"]], ["refunds", "accessibility"])

    def test_a_failed_discovery_can_be_started_again(self):
        self.fail_next = True
        sources_run.start(discover=self.discover)
        view = supervisor.view()
        self.assertEqual((view["state"], view["sources"]["run"]["status"]), (state.DISCOVERING_SOURCES, "failed"))
        self.assertIn("Tavily down", view["sources"]["run"]["error"])
        self.found()
        self.assertEqual(supervisor.view()["state"], state.AWAITING_SOURCE_SELECTION)

    def test_a_discovery_cut_off_by_a_restart_expires(self):
        with patch.object(sources_run, "_start_thread"):            # the work never runs
            sources_run.start(discover=self.discover)
        with self.assertRaises(RuntimeError):
            sources_run.start(discover=self.discover)
        with sources_run._connect() as connection:
            connection.execute("UPDATE app_source_discoveries SET created_at = now() - interval '1 hour'")
        self.assertEqual(sources_run.latest_run()["error"], "interrupted: please start again")
        self.found()

    def test_choosing_and_discovering_again_keeps_the_choices(self):
        listed = self.found()
        sources_run.choose(listed["nationalrail.co.uk"]["source_id"], "selected")
        sources_run.choose(listed["seat61.com"]["source_id"], "removed")
        with self.assertRaises(ValueError):
            sources_run.choose(listed["seat61.com"]["source_id"], "trusted")
        listed = self.found()                                            # again, from the selection
        self.assertEqual((self.discovered, listed["nationalrail.co.uk"]["status"]), (2, "selected"))
        self.assertNotIn("seat61.com", listed)                           # removed stays removed
        cover = {c["key"]: c["sources"] for c in supervisor.view()["sources"]["coverage"]}
        self.assertEqual(cover, {"refunds": ["National Rail"], "accessibility": []})

    def test_adding_your_own_site(self):
        self.found()
        ok = lambda url: {"url": url, "title": "Homepage | Transport for Wales", "text": "Rail in Wales. " * 50,
                          "error": None}
        allow = lambda *a, **k: guardrails.Verdict(decision="ALLOW", reason="rail", stage="research")
        block = lambda *a, **k: guardrails.Verdict(decision="BLOCK", reason="about cooking", stage="research")
        with self.assertRaises(ValueError):
            sources_run.add_site("not a site", check=allow, fetch=ok)
        with self.assertRaises(ValueError):
            sources_run.add_site("https://down.example", check=allow,
                                 fetch=lambda url: {"url": url, "error": "HTTPError: 503"})
        with self.assertRaises(ValueError) as forbidden:
            sources_run.add_site("https://www.scotrail.co.uk", check=allow, fetch=lambda url: {
                "url": url, "error": "HTTPError: 403 Client Error: Forbidden for url: https://www.scotrail.co.uk/"})
        self.assertIn("refuses automated access", str(forbidden.exception))
        with self.assertRaises(ValueError) as refused:
            sources_run.add_site("https://www.bbcgoodfood.com", check=block, fetch=ok)
        self.assertIn("about cooking", str(refused.exception))
        site = sources_run.add_site("tfw.wales/help", check=allow, fetch=ok)
        self.assertEqual((site["host"], site["origin"], site["status"], site["base_url"]),
                         ("tfw.wales", "user", "selected", "https://tfw.wales"))
        self.assertEqual(site["name"], "Transport for Wales")
        for title, name in (("Homepage | Transport for Wales", "Transport for Wales"),
                            ("Welcome - Office of Rail and Road", "Office of Rail and Road"),
                            ("Home", "tfw.wales"), (None, "tfw.wales")):
            self.assertEqual(sources_run.site_name(title, "tfw.wales"), name)
        again = sources_run.add_site("https://www.seat61.com/uk", check=allow, fetch=ok)   # already listed
        self.assertEqual((again["origin"], again["status"]), ("search", "selected"))

    def test_continuing_needs_a_chosen_source(self):
        listed = self.found()
        with self.assertRaises(ValueError):
            sources_run.continue_("admin-1")
        sources_run.choose(listed["nationalrail.co.uk"]["source_id"], "selected")
        sources_run.continue_("admin-1")
        view = supervisor.view()
        self.assertEqual((view["state"], view["step"]), (state.ANALYSING_SOURCES, "content"))
        self.assertEqual(view["turns"][-1]["text"], sources_run.CONTINUED)
        self.assertEqual(knowledge_system.events()[0]["details"]["sources"], ["nationalrail.co.uk"])
        with self.assertRaises(state.TransitionNotAllowed):              # the choice is made
            sources_run.choose(listed["seat61.com"]["source_id"], "selected")

    def test_confirming_the_blueprint_starts_discovery(self):
        knowledge_system.set_state(state.DISCOVERING_SOURCES, state.CLARIFYING)
        knowledge_system.set_state(state.CLARIFYING, state.DOMAIN_READY)
        version = blueprint.start_version()
        blueprint.finish(version, blueprint.DomainBlueprint.model_validate(blueprint._normalise(
            {**RAIL_BLUEPRINT, "flow": {"brief": "b", "domain": "d", "scope": "s", "supervisor_instructions": "i"}})), {})
        with patch.object(sources, "discover", side_effect=self.discover):
            blueprint_run.confirm("admin-1")
        self.assertEqual((self.discovered, knowledge_system.status()["state"]), (1, state.AWAITING_SOURCE_SELECTION))

    def test_the_sources_api(self):
        import web_api
        api = SetupApi()
        with patch.object(sources, "discover", side_effect=self.discover):
            status, view = api.call(web_api.setup_sources_discover)
        self.assertEqual((status, view["state"]), (200, state.AWAITING_SOURCE_SELECTION))
        rail = next(s for s in view["sources"]["sources"] if s["host"] == "nationalrail.co.uk")

        async def choose(source_id, body):
            request = SetupApi.Request(body)
            request.path_params = {"source_id": source_id}
            return await web_api.setup_source_choose(request)
        self.assertEqual(asyncio.run(choose(rail["source_id"], {"status": "x"})).status_code, 400)
        self.assertEqual(asyncio.run(choose(999999, {"status": "selected"})).status_code, 404)
        self.assertEqual(asyncio.run(choose(rail["source_id"], {"status": "selected"})).status_code, 200)
        self.assertEqual(api.call(web_api.setup_source_add, {"url": ""})[0], 400)
        status, view = api.call(web_api.setup_sources_continue)
        self.assertEqual((status, view["state"]), (200, state.ANALYSING_SOURCES))
        self.assertEqual(api.call(web_api.setup_sources_continue)[0], 409)


class FixtureSite:
    """A site as {url: (status, content type, body)}; records each request."""

    def __init__(self, pages: dict):
        self.pages, self.requested = pages, []

    def __call__(self, url):
        self.requested.append(url)
        status, kind, body = self.pages.get(url, (404, "text/html", ""))
        if status != 200:
            error = RuntimeError(f"{status} Client Error for url: {url}")
            error.response = Mock(status_code=status)
            raise error
        return (body.encode() if isinstance(body, str) else body), kind


SITE = "https://www.rail.example"
HOME = """<html><body><header><nav><a href="/tickets/">Tickets and railcards</a>
<a href="/help/refunds/">Refunds</a><a href="https://other.example/x">Elsewhere</a></nav></header>
<main><a href="/news/today">Today</a><a href="mailto:a@b.c">Mail</a><a href="#top">Top</a></main></body></html>"""
INDEX = f"""<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>{SITE}/sitemap-a.xml</loc></sitemap><sitemap><loc>https://cdn.other.example/s.xml</loc></sitemap>
<sitemap><loc>{SITE}/sitemap-b.xml</loc></sitemap></sitemapindex>"""
URLSET = lambda *urls: ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                        + "".join(f"<url><loc>{u}</loc><lastmod>2026-0{i % 9 + 1}-01</lastmod></url>"
                                  for i, u in enumerate(urls)) + "</urlset>")


class SiteAnalysis(unittest.TestCase):
    def test_links_are_absolute_without_fragments_and_marked_navigation(self):
        links = {l["url"]: l for l in scraping.extract_links(HOME, SITE + "/")}
        self.assertEqual(set(links), {f"{SITE}/tickets/", f"{SITE}/help/refunds/", "https://other.example/x",
                                      f"{SITE}/news/today"})
        self.assertEqual((links[f"{SITE}/tickets/"]["nav"], links[f"{SITE}/tickets/"]["text"]),
                         (True, "Tickets and railcards"))
        self.assertFalse(links[f"{SITE}/news/today"]["nav"])
        self.assertEqual(scraping.extract_links("", SITE), [])

    def test_a_site_with_a_sitemap(self):
        site = FixtureSite({
            f"{SITE}/robots.txt": (200, "text/plain", f"User-agent: *\nDisallow: /private/\nCrawl-delay: 5\n"
                                                     f"Sitemap: {SITE}/sitemap-index.xml\n"),
            f"{SITE}/": (200, "text/html", HOME),
            f"{SITE}/sitemap-index.xml": (200, "application/xml", INDEX),
            f"{SITE}/sitemap-a.xml": (200, "application/xml", URLSET(
                f"{SITE}/tickets/railcards", f"{SITE}/tickets/advance#x", f"{SITE}/private/admin",
                f"{SITE}/help/refunds/delay-repay", f"{SITE}/help/refunds/claim")),
            f"{SITE}/sitemap-b.xml": (200, "application/xml", URLSET(
                f"{SITE}/documents/conditions.pdf", "https://other.example/page", f"{SITE}/")),
        })
        slept = []
        result = site_map.analyse_site(SITE, fetch=site, sleep=slept.append)
        self.assertEqual((result["status"], result["how"]), ("ready", "sitemap"))
        urls = [p["url"] for p in result["pages"]]
        self.assertEqual(urls, [f"{SITE}/tickets/railcards", f"{SITE}/tickets/advance",
                                f"{SITE}/help/refunds/delay-repay", f"{SITE}/help/refunds/claim",
                                f"{SITE}/documents/conditions.pdf"])     # no disallowed, other host or home page
        self.assertNotIn("https://cdn.other.example/s.xml", site.requested)    # another host's sitemap
        self.assertEqual(result["robots"]["crawl_delay"], 5.0)
        self.assertTrue(slept and all(abs(s - (5.0 - site_map.config.FETCH_MAX_DELAY_SECONDS)) < 1e-9 for s in slept))
        sections = {s["key"]: s for s in result["sections"]}
        self.assertEqual(set(sections), {"tickets", "help/refunds", "documents"})
        self.assertEqual((sections["tickets"]["name"], sections["tickets"]["url_count"]), ("Tickets and railcards", 2))
        self.assertEqual(sections["help/refunds"]["name"], "Refunds")              # a container's sub-section
        self.assertEqual((sections["documents"]["name"], sections["documents"]["pdf_count"]), ("Documents", 1))
        self.assertEqual(sections["help/refunds"]["lastmod"], "2026-05-01")

    def test_a_site_without_a_sitemap_is_read_from_its_navigation(self):
        site = FixtureSite({
            f"{SITE}/": (200, "text/html", HOME),
            f"{SITE}/tickets/": (200, "text/html", '<a href="/tickets/season">Season</a><a href="/tickets/advance">A</a>'),
            f"{SITE}/help/refunds/": (200, "text/html", '<a href="/help/refunds/delay-repay">Delay Repay</a>'),
        })
        result = site_map.analyse_site(SITE, fetch=site, sleep=lambda s: None)
        self.assertEqual((result["status"], result["how"], result["robots"]["found"]), ("ready", "navigation", False))
        self.assertEqual({p["url"] for p in result["pages"]},
                         {f"{SITE}/tickets/", f"{SITE}/help/refunds/", f"{SITE}/news/today", f"{SITE}/tickets/season",
                          f"{SITE}/tickets/advance", f"{SITE}/help/refunds/delay-repay"})
        self.assertNotIn("https://other.example/x", site.requested)

    def test_blocked_sites(self):
        refuses = FixtureSite({f"{SITE}/robots.txt": (200, "text/plain", "User-agent: *\nDisallow: /\n")})
        self.assertEqual(site_map.analyse_site(SITE, fetch=refuses)["error"],
                         "its robots.txt does not allow us to read it")
        forbidden = FixtureSite({f"{SITE}/": (403, "text/html", "")})
        result = site_map.analyse_site(SITE, fetch=forbidden, sleep=lambda s: None)
        self.assertEqual((result["status"], result["error"]), ("blocked", "refuses automated access"))

    def test_the_budget_and_the_listing_cap_hold(self):
        many = [f"{SITE}/sitemap-{i}.xml" for i in range(30)]
        index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                 + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in many) + "</sitemapindex>")
        pages = {f"{SITE}/": (200, "text/html", HOME), f"{SITE}/sitemap.xml": (200, "application/xml", index)}
        pages.update({u: (200, "application/xml", URLSET(*[f"{SITE}/p{i}/{j}" for j in range(50)]))
                      for i, u in enumerate(many)})
        site = FixtureSite(pages)
        result = site_map.analyse_site(SITE, fetch=site, max_requests=6, max_urls=120, sleep=lambda s: None)
        self.assertEqual(len(site.requested), 6)
        self.assertEqual(len(result["pages"]), 120)

    def test_one_large_sitemap_does_not_crowd_out_the_others(self):
        robots = "Sitemap: {s}/sitemap-destinations.xml\nSitemap: {s}/sitemap-live-trains.xml\nSitemap: {s}/sitemap-pages.xml\n"
        site = FixtureSite({
            f"{SITE}/robots.txt": (200, "text/plain", robots.format(s=SITE)),
            f"{SITE}/": (200, "text/html", HOME),
            f"{SITE}/sitemap-destinations.xml": (200, "application/xml",
                                                 URLSET(*[f"{SITE}/destinations/{i}" for i in range(500)])),
            f"{SITE}/sitemap-live-trains.xml": (200, "application/xml", URLSET(*[f"{SITE}/live/{i}" for i in range(50)])),
            f"{SITE}/sitemap-pages.xml": (200, "application/xml",
                                          URLSET(*[f"{SITE}/help/refunds/{i}" for i in range(40)])),
        })
        result = site_map.analyse_site(SITE, fetch=site, max_urls=200, sleep=lambda s: None)
        self.assertEqual(site.requested[2:], [f"{SITE}/sitemap-pages.xml", f"{SITE}/sitemap-destinations.xml",
                                              f"{SITE}/sitemap-live-trains.xml"])   # general first, live last
        counts = {s["key"]: s["url_count"] for s in result["sections"]}
        self.assertEqual(counts["help/refunds"], 40)
        self.assertLess(counts["destinations"], 200)                              # a share, not the lot
        self.assertIn("live", counts)

    def test_small_paths_are_gathered_and_generic_link_texts_ignored(self):
        pages = [{"url": f"{SITE}/report-{i}", "lastmod": None} for i in range(5)]
        pages += [{"url": f"{SITE}/consultations/{i}", "lastmod": None} for i in range(4)]
        nav = {f"{SITE}/consultations/": "See all", f"{SITE}/report-1": "www.rail.example"}
        sections = site_map.sections(pages, nav)
        self.assertEqual([(s["key"], s["name"], s["url_count"]) for s in sections],
                         [("consultations", "Consultations", 4), ("other", "Other pages", 5)])
        self.assertIsNone(sections[-1]["path_prefix"])

    def test_section_keys(self):
        for url, key in ((f"{SITE}/", "home"), (f"{SITE}/tickets/advance", "tickets"),
                         (f"{SITE}/travel-information/accessibility/assistance", "travel-information/accessibility"),
                         (f"{SITE}/Help.html", "help"), (f"{SITE}/en/stations/leeds", "en/stations")):
            self.assertEqual(site_map.section_key(url), key, url)


def found_site(*sections):
    """An analyse_site() result: sections as (key, name, page count)."""
    out = []
    for key, name, count in sections:
        urls = [{"url": f"{SITE}/{key}/{i}", "lastmod": None} for i in range(count)]
        out.append({"key": key, "name": name, "path_prefix": f"/{key}/", "url_count": count, "pdf_count": 0,
                    "lastmod": None, "sample": [u["url"] for u in urls[:5]], "urls": urls})
    return {"status": "ready", "error": None, "how": "sitemap", "robots": {"found": True}, "requests": ["r"] * 4,
            "pages": [u for s in out for u in s["urls"]], "nav": {}, "sections": out}


MAPPING = [{"key": "tickets", "areas": ["refunds", "made_up"], "relevance": 0.9, "recommended": True,
            "reason": "Ticket rules; directly serves the refunds knowledge area"},
           {"key": "news", "areas": ["refunds"], "relevance": 0.6, "recommended": True, "flags": ["news"],
            "reason": "Press releases"},
           {"key": "jobs", "areas": [], "relevance": 0.05, "recommended": True, "reason": "Careers"},
           {"key": "help", "areas": ["accessibility"], "relevance": 0.7, "recommended": True, "reason": "Help"}]


class AnalysedSites(SetupDatabase):
    """Setup at ANALYSING_SOURCES with the fixture sites chosen; analysis runs inline."""

    def setUp(self):
        super().setUp()
        inline = patch.object(content, "_start_thread", side_effect=lambda work: work())
        inline.start()
        self.addCleanup(inline.stop)
        version = blueprint.start_version()
        blueprint.finish(version, blueprint.DomainBlueprint.model_validate(blueprint._normalise(
            {**RAIL_BLUEPRINT, "flow": {"brief": "b", "domain": "d", "scope": "s", "supervisor_instructions": "i"}})), {})
        blueprint.confirm(version, "admin-1")
        knowledge_system.set_blueprint_version(version)
        for a, b in zip(state.STATES, state.STATES[1:7]):
            knowledge_system.set_state(a, b)          # NEW … ANALYSING_SOURCES
        sources.save_discovered([dict(s) for s in SITES])
        self.ids = {s["host"]: s["source_id"] for s in sources.list_sources()}
        for host in self.ids:
            sources.set_status(self.ids[host], "selected")
        self.mapped = []

    def analyse(self, base_url):
        if "seat61" in base_url:
            return {"status": "blocked", "error": "refuses automated access", "requests": ["r"], "sections": []}
        return found_site(("tickets", "Tickets", 250), ("news", "News", 30), ("jobs", "Jobs", 3), ("help", "Help", 8))

    def mapper(self, bp, site, found):
        self.mapped.append((site["host"], [s["key"] for s in found]))
        return content.SectionAssessments(sections=[content.SectionAssessment.model_validate(m) for m in MAPPING]), {}

    def analysed(self):
        content.start(analyse=self.analyse, mapper=self.mapper)
        return {s["host"]: s for s in supervisor.view()["content"]["sites"]}


class ContentStep(AnalysedSites):
    """The Content step: analysis in the background (inline here), choosing sections and pages."""

    def test_the_chosen_sites_are_analysed_and_mapped(self):
        sites = self.analysed()
        self.assertEqual(knowledge_system.status()["state"], state.AWAITING_CONTENT_SELECTION)
        self.assertEqual(self.mapped, [("nationalrail.co.uk", ["tickets", "news", "jobs", "help"])])
        rail = {c["key"]: c for c in sites["nationalrail.co.uk"]["sections"]}
        self.assertEqual(list(rail), ["tickets", "help", "news", "jobs"])          # recommended first
        self.assertEqual((rail["tickets"]["recommended"], rail["tickets"]["areas"], rail["tickets"]["large"]),
                         (True, ["refunds"], True))                                 # unknown area dropped; > 200 pages
        self.assertEqual(rail["tickets"]["reason"], "Ticket rules; directly serves the Refunds knowledge area")
        self.assertFalse(rail["news"]["recommended"])                               # flagged
        self.assertFalse(rail["jobs"]["recommended"])                               # too little relevance
        self.assertTrue(all(c["status"] == "candidate" for c in rail.values()))     # suggested, not chosen
        self.assertEqual(len(rail["tickets"]["sample"]), 5)
        self.assertNotIn("urls", rail["tickets"])                                   # page lists on demand
        blocked = sites["seat61.com"]
        self.assertEqual((blocked["analysis"]["status"], blocked["analysis"]["error"], blocked["sections"]),
                         ("blocked", "refuses automated access", []))
        self.assertEqual(sites["nationalrail.co.uk"]["analysis"]["page_count"], 291)

    def test_choosing_sections_and_pages(self):
        rail = {c["key"]: c for c in self.analysed()["nationalrail.co.uk"]["sections"]}
        content.choose(rail["tickets"]["content_id"], "selected")
        content.choose(rail["help"]["content_id"], "selected")
        content.choose(rail["news"]["content_id"], "removed")
        with self.assertRaises(ValueError):
            content.choose(rail["jobs"]["content_id"], "maybe")
        pages = content.get_section(rail["tickets"]["content_id"])["urls"]
        section = content.exclude(rail["tickets"]["content_id"],
                                  [pages[0]["url"], pages[1]["url"], pages[1]["url"], "https://elsewhere.example/x"])
        self.assertEqual(section["excluded_urls"], [pages[0]["url"], pages[1]["url"]])   # own pages only, once
        view = supervisor.view()["content"]
        self.assertEqual(view["pages_chosen"], 248 + 8)
        self.assertNotIn("news", [c["key"] for c in view["sites"][0]["sections"]])        # removed: hidden
        self.assertEqual({c["key"]: c["sections"] for c in view["coverage"]},
                         {"refunds": ["National Rail: Tickets"], "accessibility": ["National Rail: Help"]})

    def test_analysing_again_keeps_the_choices(self):
        rail = {c["key"]: c for c in self.analysed()["nationalrail.co.uk"]["sections"]}
        content.choose(rail["tickets"]["content_id"], "selected")
        content.exclude(rail["tickets"]["content_id"], [f"{SITE}/tickets/0"])
        self.analyse = lambda url: found_site(("tickets", "Tickets", 260), ("help", "Help", 8))
        content.start(analyse=self.analyse, mapper=self.mapper, only=self.ids["nationalrail.co.uk"])
        again = {c["key"]: c for c in supervisor.view()["content"]["sites"][0]["sections"]}
        self.assertEqual(set(again), {"tickets", "help"})                          # news and jobs gone
        self.assertEqual((again["tickets"]["status"], again["tickets"]["url_count"],
                          again["tickets"]["excluded_urls"]), ("selected", 260, [f"{SITE}/tickets/0"]))

    def test_a_failed_analysis_shows_why(self):
        def broken(url):
            raise OSError("network down")
        content.start(analyse=broken, mapper=self.mapper)
        sites = {s["host"]: s for s in supervisor.view()["content"]["sites"]}
        self.assertEqual(sites["nationalrail.co.uk"]["analysis"]["status"], "failed")
        self.assertIn("network down", sites["nationalrail.co.uk"]["analysis"]["error"])
        content.start(analyse=lambda url: found_site(), mapper=self.mapper, only=self.ids["nationalrail.co.uk"])
        self.assertEqual(content.analyses()[self.ids["nationalrail.co.uk"]]["error"], "no pages found on the site")
        self.assertEqual(knowledge_system.status()["state"], state.AWAITING_CONTENT_SELECTION)   # moved on anyway

    def test_an_analysis_cut_off_by_a_restart_expires(self):
        content.set_analysis(self.ids["nationalrail.co.uk"], "analysing")
        with self.assertRaises(RuntimeError):
            content.start(analyse=self.analyse, mapper=self.mapper)
        with content._connect() as connection:
            connection.execute("UPDATE kb_site_analyses SET updated_at = now() - interval '1 hour'")
        self.assertEqual(content.analyses()[self.ids["nationalrail.co.uk"]]["error"], "interrupted: please analyse again")

    def test_choosing_waits_for_the_analysis_and_going_back_to_sources(self):
        with self.assertRaises(state.TransitionNotAllowed):
            content.choose(1, "selected")
        self.analysed()
        content.back_to_sources("admin-1")
        self.assertEqual(knowledge_system.status()["state"], state.AWAITING_SOURCE_SELECTION)

    def test_continuing_from_sources_starts_the_analysis(self):
        knowledge_system.set_state(state.ANALYSING_SOURCES, state.AWAITING_SOURCE_SELECTION)
        started = []
        with patch.object(content, "start", side_effect=lambda **k: started.append(k)):
            sources_run.continue_("admin-1")
        self.assertEqual((knowledge_system.status()["state"], started), (state.ANALYSING_SOURCES, [{}]))

    def test_the_content_api(self):
        import web_api
        api = SetupApi()
        content.start(analyse=self.analyse, mapper=self.mapper)
        tickets = next(c for c in supervisor.view()["content"]["sites"][0]["sections"] if c["key"] == "tickets")

        async def call(handler, content_id, body=None):
            request = SetupApi.Request(body)
            request.path_params = {"content_id": content_id}
            result = handler(request)
            response = await result if hasattr(result, "__await__") else result
            return response.status_code, json.loads(response.body)
        status, section = asyncio.run(call(web_api.setup_content_section, tickets["content_id"]))
        self.assertEqual((status, len(section["urls"])), (200, 250))
        self.assertEqual(asyncio.run(call(web_api.setup_content_section, 999999))[0], 404)
        self.assertEqual(asyncio.run(call(web_api.setup_content_choose, tickets["content_id"], {"status": "x"}))[0], 400)
        self.assertEqual(asyncio.run(call(web_api.setup_content_choose, 999999, {"status": "selected"}))[0], 404)
        self.assertEqual(asyncio.run(call(web_api.setup_content_choose, tickets["content_id"],
                                          {"status": "selected"}))[0], 200)
        self.assertEqual(asyncio.run(call(web_api.setup_content_exclude, tickets["content_id"], {"excluded": "x"}))[0],
                         400)
        status, body = asyncio.run(call(web_api.setup_content_exclude, tickets["content_id"],
                                        {"excluded": [f"{SITE}/tickets/3"]}))
        self.assertEqual((status, body["view"]["content"]["pages_chosen"]), (200, 249))
        self.assertEqual(api.call(web_api.setup_back_to_sources)[0], 200)
        self.assertEqual(api.call(web_api.setup_content_analyse, {})[0], 409)        # not in the content step now


class IngestionPlan(AnalysedSites):
    """The Build step's plan: built from the chosen content, reviewed, approved; nothing fetched."""

    def setUp(self):
        super().setUp()
        storage.ensure_collection(storage.get_client())     # approving queues the build's job
        content.start(analyse=self.analyse, mapper=self.mapper)
        self.sections = {c["key"]: c for c in supervisor.view()["content"]["sites"][0]["sections"]}

    def choose(self, *keys):
        for key in keys:
            content.choose(self.sections[key]["content_id"], "selected")

    def test_the_plan_is_the_chosen_pages(self):
        self.choose("tickets", "help")
        content.exclude(self.sections["tickets"]["content_id"], [f"{SITE}/tickets/0", f"{SITE}/tickets/1"])
        review = plan.review()
        self.assertEqual(review["plan"]["totals"], {"pages": 248 + 8, "pdfs": 0, "sites": 1, "sections": 2,
                                                    "minutes": 11})
        urls = [p["url"] for p in plan.pages(review["plan"]["version"])]
        self.assertNotIn(f"{SITE}/tickets/0", urls)
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual({(g["section"], g["pages"], g["ttl_days"]) for g in review["sections"]},
                         {("Tickets", 248, 90), ("Help", 8, 90)})
        self.assertEqual(review["uncovered"], [])
        self.assertIsNone(review["problem"])
        self.assertIsNone(review["differences"])                            # nothing approved yet
        self.assertEqual(plan.pages(review["plan"]["version"])[0]["status"], "pending")

    def test_versions(self):
        self.choose("help")
        self.assertEqual(plan.review()["uncovered"], ["Refunds"])           # an area no chosen section covers
        first = plan.review()["plan"]["version"]
        self.assertEqual(plan.review()["plan"]["version"], first)          # unchanged: the same draft
        self.choose("tickets")
        second = plan.review()["plan"]
        self.assertEqual((second["version"], second["status"]), (first + 1, "draft"))
        self.assertEqual(plan.pages(first), [])                             # the old draft is gone

    def test_ttl_rules_and_the_users_ttl(self):
        now = datetime(2026, 10, 9, tzinfo=timezone.utc)
        base = {"flags": [], "pdf_count": 0, "url_count": 10, "lastmod": None}
        self.assertEqual(plan.suggested_ttl(base, now), 90)
        self.assertEqual(plan.suggested_ttl({**base, "lastmod": "2026-09-20"}, now), plan.TTL_OFTEN)
        self.assertEqual(plan.suggested_ttl({**base, "flags": ["live"]}, now), plan.TTL_OFTEN)
        self.assertEqual(plan.suggested_ttl({**base, "lastmod": "2024-01-01T00:00:00Z"}, now), plan.TTL_RARE)
        self.assertEqual(plan.suggested_ttl({**base, "pdf_count": 10}, now), plan.TTL_RARE)
        dated = lambda *days: {**base, "urls": [{"url": "u", "lastmod": d} for d in days]}
        self.assertEqual(plan.suggested_ttl(dated("2026-10-01", "2026-06-01", "2026-05-01", None), now), 90)
        self.assertEqual(plan.suggested_ttl(dated("2026-10-01", "2026-09-30", "2026-05-01"), now), plan.TTL_OFTEN)
        self.choose("help")
        help_id = self.sections["help"]["content_id"]
        for bad in (0, 400, "30", 7.5):
            with self.assertRaises(ValueError):
                plan.set_ttl(help_id, bad)
        plan.set_ttl(help_id, 30)
        self.assertEqual(plan.review()["sections"][0]["ttl_days"], 30)
        self.assertEqual(plan.review()["sections"][0]["ttl_suggested"], 90)
        plan.set_ttl(help_id, None)
        self.assertEqual(plan.review()["sections"][0]["ttl_days"], 90)

    def test_approval(self):
        with self.assertRaises(ValueError):
            plan.approve(plan.review()["plan"]["version"], "admin-1")       # nothing chosen
        self.choose("help")
        version = plan.review()["plan"]["version"]
        self.choose("tickets")                                              # changed after the review
        with self.assertRaises(ValueError):
            plan.approve(version, "admin-1")
        with patch.object(plan, "PLAN_PAGE_LIMIT", 100):
            version = plan.review()["plan"]["version"]   # still the 258-page draft, made with the limit of 500
            content.choose(self.sections["jobs"]["content_id"], "selected")
            review = plan.review()
            self.assertIn("more than the limit of 100", review["problem"])
            with self.assertRaises(ValueError):
                plan.approve(review["plan"]["version"], "admin-1")
        content.choose(self.sections["jobs"]["content_id"], "candidate")
        version = plan.review()["plan"]["version"]
        approved = plan.approve(version, "admin-1")
        self.assertEqual((approved["status"], approved["approved_by"]), ("approved", "admin-1"))
        self.assertIsNotNone(approved["approved_at"])
        self.assertEqual(knowledge_system.status()["state"], state.INGESTION_APPROVED)
        event = knowledge_system.events()[0]
        self.assertEqual((event["details"]["plan_version"], event["details"]["pages"]), (version, 258))
        self.assertEqual(supervisor.view()["build"]["progress"], {"pending": 258})
        with self.assertRaises(state.TransitionNotAllowed):
            plan.approve(version, "admin-1")                                 # once only

    def test_nothing_is_fetched_before_approval(self):
        refuse = RuntimeError("fetched before approval")
        with patch.object(scraping, "fetch_html", side_effect=refuse) as html, \
                patch.object(scraping, "fetch_bytes", side_effect=refuse) as data, \
                patch("research.fetch_source", side_effect=refuse) as page:
            self.choose("tickets", "help")
            plan.set_ttl(self.sections["help"]["content_id"], 30)
            plan.approve(plan.review()["plan"]["version"], "admin-1")
            supervisor.view()
        self.assertEqual((html.call_count, data.call_count, page.call_count), (0, 0, 0))

    def test_a_changed_plan_shows_what_it_adds_and_removes(self):
        self.choose("tickets")
        plan.approve(plan.review()["plan"]["version"], "admin-1")
        client = storage.get_client()
        storage.ensure_collection(client)
        storage.upsert_chunks(client, f"{SITE}/tickets/5", ["a", "b"], [[1.0] + [0.0] * 255] * 2, "h", 90)
        plan.back_to_content("admin-1")
        self.assertEqual(knowledge_system.status()["state"], state.AWAITING_CONTENT_SELECTION)
        content.exclude(self.sections["tickets"]["content_id"], [f"{SITE}/tickets/5", f"{SITE}/tickets/6"])
        self.choose("help")
        diff = plan.review()["differences"]
        self.assertEqual({k: diff[k] for k in ("against", "added", "removed", "removed_chunks")},
                         {"against": 1, "added": 8, "removed": 2, "removed_chunks": 2})
        second = plan.review()["plan"]["version"]
        plan.approve(second, "admin-1")
        self.assertEqual([(p["version"], p["status"]) for p in (plan.latest("approved"), plan.latest("superseded"))],
                         [(second, "approved"), (1, "superseded")])

    def test_the_plan_api(self):
        import web_api
        api = SetupApi()
        self.choose("help")
        review = json.loads(web_api.setup_plan_review(SetupApi.Request()).body)
        version = review["plan"]["version"]

        async def ttl(content_id, body):
            request = SetupApi.Request(body)
            request.path_params = {"content_id": content_id}
            response = await web_api.setup_content_ttl(request)
            return response.status_code
        self.assertEqual(asyncio.run(ttl(self.sections["help"]["content_id"], {"ttl_days": 0})), 400)
        self.assertEqual(asyncio.run(ttl(999999, {"ttl_days": 30})), 404)
        self.assertEqual(asyncio.run(ttl(self.sections["help"]["content_id"], {"ttl_days": 30})), 200)
        self.assertEqual(api.call(web_api.setup_plan_approve, {"version": "1"})[0], 400)
        self.assertEqual(api.call(web_api.setup_plan_approve, {"version": version})[0], 409)   # TTL changed it
        version = json.loads(web_api.setup_plan_review(SetupApi.Request()).body)["plan"]["version"]
        status, view = api.call(web_api.setup_plan_approve, {"version": version})
        self.assertEqual((status, view["state"], view["build"]["plan"]["approved_by"]),
                         (200, state.INGESTING, "admin-1"))                # Build RAG queues the build
        self.assertEqual(api.call(web_api.setup_back_to_content)[0], 200)

    def test_a_reset_clears_the_plans(self):
        self.choose("help")
        plan.approve(plan.review()["plan"]["version"], "admin-1")
        deleted = knowledge_system.reset("RESET", "admin-1")
        self.assertEqual((deleted["kb_ingestion_plans"], deleted["kb_ingestion_plan_pages"]), (1, 8))


ROBOTS = b"User-agent: *\nDisallow: /help/7\n"


class Building(AnalysedSites):
    """Build RAG's fixture: the approved plan read through the worker; fetching and embedding faked."""

    def setUp(self):
        super().setUp()
        content.start(analyse=self.analyse, mapper=self.mapper)
        self.sections = {c["key"]: c for c in supervisor.view()["content"]["sites"][0]["sections"]}
        self.client = storage.get_client()
        storage.ensure_collection(self.client)
        self.fetched = []
        self.failing = False
        self.stop = threading.Event()
        self.stop_after = None
        fakes = [patch.object(scraping, "fetch_bytes", side_effect=self.fetch_bytes),
                 patch.object(scraping, "fetch_html", side_effect=self.fetch_html),
                 patch.object(scraping, "extract_text", side_effect=lambda page, url: f"All about {url}, in full."),
                 patch("common.embedding.embed_texts", side_effect=lambda texts: [[1.0] + [0.0] * 255 for _ in texts])]
        for fake in fakes:
            fake.start()
            self.addCleanup(fake.stop)

    def fetch_bytes(self, url):
        if url.endswith("/robots.txt"):
            return ROBOTS, "text/plain"
        raise AssertionError(f"unexpected fetch of {url}")

    def fetch_html(self, url):
        if self.failing:
            raise OSError("connection reset")
        self.fetched.append(url)
        if self.stop_after and len(self.fetched) >= self.stop_after:
            self.stop.set()
        return "<html>page</html>"

    def approve(self, *keys):
        for key in keys:
            content.choose(self.sections[key]["content_id"], "selected")
        return build.build_rag(plan.review()["plan"]["version"], "admin-1")["version"]

    def work(self):
        """What the worker does with the queued job."""
        import ingestion_worker
        job = storage.claim_job(self.client)
        ingestion_worker.process_job(self.client, job)
        return storage.list_jobs(self.client, "ingest_plan", 1)[0]

    def chunks(self):
        with self.client.connection() as connection:
            return connection.execute("SELECT source_url, payload FROM rag_chunks").fetchall()



class Build(Building):
    """Build RAG."""

    def test_a_plan_of_more_than_ten_pages_is_built(self):
        version = self.approve("tickets", "help")
        self.assertEqual(knowledge_system.status()["state"], state.INGESTING)
        self.assertEqual(self.fetched, [])
        job = self.work()
        self.assertEqual(job["status"], "success")
        self.assertEqual(len(self.fetched), 250 + 7)                         # all but the disallowed page
        self.assertNotIn(f"{SITE}/help/7", self.fetched)
        view = supervisor.view()["build"]
        self.assertEqual(view["progress"], {"ingested": 257, "blocked": 1})
        self.assertEqual(view["failures"], [{"url": f"{SITE}/help/7", "section": "Help", "status": "blocked",
                                             "error": "robots.txt does not allow it"}])
        self.assertEqual(knowledge_system.status()["state"], state.EVALUATING)
        self.assertEqual({k: view["plan"]["build"][k] for k in ("pages_with_chunks", "chunks", "missing")},
                         {"pages_with_chunks": 257, "chunks": 257, "missing": []})
        self.assertEqual([e["event"] for e in knowledge_system.events()[:3]],
                         ["state"] * 3)                                      # INGESTING, INDEXING, EVALUATING

    def test_every_chunk_traces_back_to_an_approved_plan_entry(self):
        version = self.approve("tickets", "help")
        self.work()
        stored = self.chunks()
        self.assertEqual(len(stored), 257)
        entries = {p["url"]: p for p in plan.pages(version)}
        for row in stored:
            payload = row["payload"]
            entry = entries[row["source_url"]]
            self.assertEqual((payload["origin"], payload["plan_version"], payload["content_id"], payload["source_id"]),
                             ("setup", version, entry["content_id"], entry["source_id"]))
            self.assertEqual(payload["approved_by"], "admin-1")
            self.assertEqual(payload["ttl_days"], entry["ttl_days"])
        self.assertEqual(plan.latest("approved")["version"], version)

    def test_a_stopped_build_carries_on(self):
        self.approve("tickets", "help")
        self.stop_after = 5
        self.assertFalse(build.run(self.client, plan.latest("approved")["version"], self.stop))
        self.assertEqual(plan.view()["progress"], {"ingested": 5, "pending": 253})
        self.assertEqual(knowledge_system.status()["state"], state.INGESTING)
        self.stop.clear()
        self.stop_after = None
        self.assertTrue(build.run(self.client, plan.latest("approved")["version"], self.stop))
        self.assertEqual(len(self.fetched), 257)                             # none read twice
        self.assertEqual(knowledge_system.status()["state"], state.EVALUATING)

    def test_a_build_where_nothing_could_be_read_waits_for_a_retry(self):
        self.approve("help")
        self.failing = True
        self.work()
        self.assertEqual(knowledge_system.status()["state"], state.INGESTING)
        self.assertEqual(plan.view()["progress"], {"failed": 7, "blocked": 1})
        self.assertEqual(knowledge_system.events()[0]["event"], "build_failed")
        self.assertIn("connection reset", supervisor.view()["build"]["failures"][0]["error"])
        self.failing = False
        build.retry("admin-1")
        self.assertEqual(plan.view()["progress"], {"pending": 7, "blocked": 1})
        self.work()
        self.assertEqual(knowledge_system.status()["state"], state.EVALUATING)
        with self.assertRaises(ValueError):
            build.retry("admin-1")                                           # no page failed

    def test_a_build_stopped_at_indexing_carries_on(self):
        self.approve("help")
        with patch.object(plan, "set_build", side_effect=RuntimeError("database went away")):
            job = self.work()
        self.assertEqual((job["status"], knowledge_system.status()["state"]), ("failed", state.INDEXING))
        self.fetched.clear()
        build.retry("admin-1")                                               # nothing failed: carries on
        self.work()
        self.assertEqual((self.fetched, knowledge_system.status()["state"]), ([], state.EVALUATING))
        self.assertEqual(plan.view()["plan"]["build"]["pages_with_chunks"], 7)

    def test_a_plan_table_from_before_the_build_column_is_brought_up_to_date(self):
        plan.ensure_schema()
        with plan._connect() as connection:
            connection.execute("ALTER TABLE kb_ingestion_plans DROP COLUMN build")
        plan.reset_schema_cache()
        plan.ensure_schema()
        self.approve("help")
        self.work()
        self.assertEqual(knowledge_system.status()["state"], state.EVALUATING)

    def test_a_changed_plan_removes_dropped_pages_and_keeps_the_rest(self):
        first = self.approve("help")
        self.work()
        plan.back_to_content("admin-1")
        content.exclude(self.sections["help"]["content_id"], [f"{SITE}/help/0"])
        second = self.approve()
        self.fetched.clear()
        self.work()
        urls = {r["source_url"]: r["payload"] for r in self.chunks()}
        self.assertNotIn(f"{SITE}/help/0", urls)                              # dropped: its chunks removed
        self.assertEqual(self.fetched, [])                                    # the rest still fresh: not fetched
        self.assertEqual({p["plan_version"] for p in urls.values()}, {second})   # … but now from this plan
        self.assertEqual(plan.view()["progress"], {"unchanged": 6, "blocked": 1})
        self.assertNotEqual(first, second)

    def test_why_is_this_here(self):
        version = self.approve("help")
        self.work()
        why = build.provenance(f"{SITE}/help/2")
        self.assertEqual((why["origin"], why["plan"]["version"], why["plan"]["approved_by"], why["section"]["name"],
                          why["source"]["host"], why["entry"]["status"]),
                         ("setup", version, "admin-1", "Help", "nationalrail.co.uk", "ingested"))
        self.assertEqual(why["section"]["reason"], "Help")
        with self.assertRaises(LookupError):
            build.provenance("https://elsewhere.example/x")
        storage.upsert_chunks(self.client, "https://old.example/a", ["x"], [[1.0] + [0.0] * 255], "h", 90)
        self.assertEqual(build.provenance("https://old.example/a")["origin"], "configured list")

        import web_api
        request = SetupApi.Request()
        request.query_params = {"url": f"{SITE}/help/2"}
        self.assertEqual(web_api.knowledge_provenance(request).status_code, 200)
        request.query_params = {}
        self.assertEqual(web_api.knowledge_provenance(request).status_code, 400)

    def test_the_build_api_and_one_job_at_a_time(self):
        import web_api
        api = SetupApi()
        storage.enqueue_job(self.client, "ingest_urls", [{"url": "fixture://one"}])   # another job is waiting
        content.choose(self.sections["help"]["content_id"], "selected")
        version = plan.review()["plan"]["version"]
        status, body = api.call(web_api.setup_plan_approve, {"version": version})
        self.assertEqual((status, knowledge_system.status()["state"]), (409, state.INGESTION_APPROVED))
        self.assertIn("another ingestion job", body["error"])
        with self.client.connection() as connection:
            connection.execute("DELETE FROM ingestion_jobs")
        status, view = api.call(web_api.setup_build_start, {"version": version})
        self.assertEqual((status, view["state"], view["build"]["job"]["status"]), (200, state.INGESTING, "queued"))
        self.assertEqual(api.call(web_api.setup_build_retry)[0], 409)        # nothing failed

class Evaluation(Building):
    """#16: the candidate flow and the evaluation set, after a build of the Help section
    (the accessibility area); refunds has no pages, live departures is live. Model calls stubbed."""

    def setUp(self):
        super().setUp()
        version = knowledge_system.status()["blueprint_version"]
        bp = blueprint.get(version)["blueprint"]
        areas = {a["key"]: a for a in bp["knowledge_areas"]}
        areas["refunds"]["example_questions"] = ["Can I get a refund on an off-peak ticket?"]
        areas["live_departures"]["example_questions"] = ["When is the next train from Leeds to York?"]
        bp["flow"] = {"brief": "An assistant for passengers on UK railways; every question means the UK.",
                      "domain": "UK train information", "search_country": "united kingdom",
                      "scope": "In scope: UK rail tickets, refunds and accessibility. Out of scope: flights, visas.",
                      "supervisor_instructions": "Send rail questions to knowledge_base; live times to tools."}
        with blueprint._connect() as connection:
            connection.execute("UPDATE app_domain_blueprints SET blueprint = %s WHERE version = %s",
                               (psycopg.types.json.Jsonb(bp), version))
        inline = patch.object(evaluation, "_start_thread", side_effect=lambda work: work())
        inline.start()
        self.addCleanup(inline.stop)
        self.version = self.approve("help")
        self.work()
        self.written = []

    def writer(self, bp, area, pages, count):
        """Two grounded questions, then one whose quote is not in its page and one about a page not given."""
        self.written.append((area["key"], [p["url"] for p in pages], count))
        q = evaluation.DraftQuestion
        return evaluation.DraftQuestions(questions=[
            q(question="Is help available at the station?", expected_answer="Yes.", expected_source=pages[0]["url"],
              supporting_quote=f"All about  {pages[0]['url'].upper()}, in full."),
            q(question="Can I book assistance?", expected_answer="Yes.", expected_source=pages[1]["url"],
              supporting_quote=f"\u201call about {pages[1]['url']}, in full\u201d"),
            q(question="Are there ramps?", expected_answer="Yes.", expected_source=pages[2]["url"],
              supporting_quote="Every station has ramps on every platform."),
            q(question="Is there a lift?", expected_answer="Yes.", expected_source="https://elsewhere.example/lifts",
              supporting_quote="All about https://elsewhere.example/lifts, in full.")])

    @staticmethod
    def scoper(bp, count):
        return ["How do I get a visa for France?", "What flights go from London to Paris?", "A third?"]

    def prepare(self, **kwargs):
        return evaluation.prepare("admin-1", check=lambda spec: None, writer=kwargs.get("writer", self.writer),
                                  scoper=kwargs.get("scoper", self.scoper))

    def test_a_built_knowledge_base_gets_questions_for_every_area(self):
        self.prepare()
        view = supervisor.view()["evaluation"]
        self.assertEqual(view["status"], "ready", view.get("error"))
        kinds = [(q["kind"], q["area"]) for q in view["questions"]]
        self.assertEqual(kinds, [("not_covered", "refunds")] + [("answer", "accessibility")] * 2 +
                         [("live", "live_departures")] + [("out_of_scope", "out_of_scope")] * 2)
        first = view["questions"][1]
        self.assertTrue(first["expected_source"].startswith(f"{SITE}/help/"))
        self.assertEqual((first["expected"], first["expect_blocked"]), ("Yes.", False))
        self.assertTrue(all(q["expect_blocked"] for q in view["questions"][-2:]))
        self.assertEqual(self.written[0][0], "accessibility")
        self.assertEqual((len(self.written[0][1]), self.written[0][2]), (evaluation.PAGES_PER_AREA, 3))
        self.assertEqual([(d["question"], d["reason"]) for d in view["record"]["dropped"]],
                         [("Are there ramps?", "its supporting quote is not in the page"),
                          ("Is there a lift?", "its page is not one of the pages given")])
        self.assertEqual(view["record"]["dropped"][0]["quote"], "Every station has ramps on every platform.")
        self.assertEqual({k: (a["kind"], a["questions"]) for k, a in view["record"]["areas"].items()},
                         {"accessibility": ("answer", 2), "refunds": ("not_covered", 1),
                          "live_departures": ("live", 1), "out_of_scope": ("out_of_scope", 2)})
        stored = flows.evals.get_set(evaluation.SET_ID)                     # editable like any set
        self.assertEqual((stored["count"], stored["builtin"]), (6, False))

    def test_the_candidate_flow_has_the_blueprints_settings_and_is_not_live(self):
        self.prepare()
        view = evaluation.view()
        self.assertEqual((view["flow_id"], view["flow_version"]), ("setup_uk_train_information", 1))
        spec = flows.FlowSpec.model_validate(flows.store.get_version(view["flow_id"], 1, flows.load_flow()))
        supervisor_ = next(n for n in spec.nodes if n.type == "supervisor").config
        self.assertEqual((supervisor_["domain"], supervisor_["search_country"]),
                         ("UK train information", "united kingdom"))
        self.assertTrue(supervisor_["brief"].startswith("An assistant for passengers"))
        self.assertTrue(supervisor_["instructions"].startswith("Send rail questions"))
        scope = next(n for n in spec.nodes if n.type == "input_guardrail").config["scope"]
        self.assertIn("Out of scope: flights", scope)
        self.assertEqual(flows.validate(spec), [])
        self.assertIsNone(flows.store.live_pointer())                        # users still get the built-in flow
        self.prepare()                                                       # prepared again: a new version
        self.assertEqual(evaluation.view()["flow_version"], 2)

    def test_the_candidate_flow_compiles(self):
        bp = content._confirmed_blueprint()
        spec = evaluation.apply_blueprint(flows.load_flow(), bp, "setup_uk_train_information")
        evaluation._check_flow(spec)

    def test_the_grounding_check(self):
        page = [{"url": "https://a.example/x", "text": "Assisted travel:\nbook  it 2 hours ahead \u2013 or turn up."}]
        q = evaluation.DraftQuestion
        check = lambda quote, url="https://a.example/x": evaluation.grounded(
            q(question="?", expected_answer="A", expected_source=url, supporting_quote=quote), page)
        self.assertIsNone(check("BOOK it 2 hours ahead - or turn up"))
        self.assertIsNone(check("\u201cbook it 2 hours ahead \u2013 or turn up.\u201d"))
        self.assertEqual(check("book it"), "its supporting quote is too short")
        self.assertIsNone(check("Assisted travel: book it 2 hours ahead"))         # a line break read as a colon
        self.assertEqual(check("book it 3 hours ahead - or turn up"), "its supporting quote is not in the page")
        self.assertEqual(check("ok it 2 hours ahead or turn up"), "its supporting quote is not in the page")
        self.assertEqual(check("book it 2 hours ahead", "https://a.example/y"), "its page is not one of the pages given")

    def test_prepared_by_itself_once_per_plan(self):
        with patch.object(evaluation, "write_with_llm", side_effect=self.writer), \
                patch.object(evaluation, "scope_with_llm", side_effect=self.scoper), \
                patch.object(evaluation, "_check_flow"):
            evaluation.ensure_prepared()
            evaluation.ensure_prepared()
        with evaluation._connect() as connection:
            rows = connection.execute("SELECT status, plan_version FROM app_setup_evaluations").fetchall()
        self.assertEqual(rows, [{"status": "ready", "plan_version": self.version}])

    def test_not_prepared_before_the_build_has_finished(self):
        knowledge_system.set_state(state.EVALUATING, state.INGESTING)
        evaluation.ensure_prepared()
        self.assertIsNone(evaluation.view())
        with self.assertRaises(state.TransitionNotAllowed):
            self.prepare()

    def test_failures_are_listed_or_fail_the_preparation(self):
        def broken(*args):
            raise llm.NoToolCall("no tool call")
        self.prepare(writer=broken, scoper=broken)
        view = evaluation.view()
        self.assertEqual(view["status"], "ready")
        self.assertEqual([(d["area"], d["reason"]) for d in view["record"]["dropped"]],
                         [("accessibility", "no questions written: NoToolCall"),
                          ("out_of_scope", "none written: NoToolCall")])
        self.assertEqual([q["kind"] for q in view["questions"]], ["not_covered", "live"])

        def uncompilable(spec):
            raise flows.FlowError([{"level": "error", "where": spec.id, "message": "does not compile"}])
        evaluation.prepare("admin-1", check=uncompilable, writer=self.writer, scoper=self.scoper)
        self.assertEqual(evaluation.view()["status"], "failed")
        self.assertIn("FlowError", evaluation.view()["error"])

    def test_a_large_blueprint_stays_within_the_set_limit(self):
        bp = content._confirmed_blueprint()
        many = [{"key": f"area_{i}", "name": f"Area {i}", "description": "d", "knowledge_class": "STATIC_KNOWLEDGE",
                 "example_questions": []} for i in range(30)]
        counts = []
        def own_pages(version, key, size=3):                               # each area its own pages
            return [{"url": f"{SITE}/{key}/{i}", "text": f"All about {key} {i} and more, in full."} for i in range(size)]
        with patch.object(evaluation, "area_pages", side_effect=own_pages):
            def writer(bp, area, pages, count):
                counts.append(count)
                return evaluation.DraftQuestions(questions=[evaluation.DraftQuestion(
                    question=f"{area['key']} {i}?", expected_answer="A", expected_source=pages[i]["url"],
                    supporting_quote=pages[i]["text"]) for i in range(count)])
            built = evaluation.build_set({**bp, "knowledge_areas": many}, self.version, writer, self.scoper)
        self.assertEqual(len(built["questions"]), flows.evals.MAX_QUESTIONS)
        self.assertEqual(counts, [2] * 18 + [1] * 12)                       # 48 answer questions share the room
        self.assertEqual(built["dropped"], [])
        more = [{**a, "key": f"more_{i}"} for i, a in enumerate(many)]
        with patch.object(evaluation, "area_pages", side_effect=lambda version, key: own_pages(version, key, 1)):
            built = evaluation.build_set({**bp, "knowledge_areas": many + more}, self.version, writer, self.scoper)
        self.assertEqual(sum(q["kind"] == "answer" for q in built["questions"]), 48)
        self.assertEqual([d["reason"] for d in built["dropped"]], ["the set is full"] * 12)

    def test_one_passage_makes_one_question(self):
        bp = content._confirmed_blueprint()
        shared = [{"url": f"{SITE}/a", "text": "Delay Repay pays from 15 minutes late, on most trains."}]
        two = [{"key": k, "name": k, "description": "d", "knowledge_class": "STATIC_KNOWLEDGE"}
               for k in ("refunds", "delay_compensation")]

        def writer(bp, area, pages, count):
            return evaluation.DraftQuestions(questions=[evaluation.DraftQuestion(
                question=f"{area['key']}?", expected_answer="A", expected_source=pages[0]["url"],
                supporting_quote="Delay Repay pays from 15 minutes late")])
        with patch.object(evaluation, "area_pages", return_value=shared):
            built = evaluation.build_set({**bp, "knowledge_areas": two}, self.version, writer, self.scoper)
        self.assertEqual([q["question"] for q in built["questions"] if q["kind"] == "answer"], ["refunds?"])
        self.assertEqual([(d["area"], d["reason"]) for d in built["dropped"]],
                         [("delay_compensation", "another question is from the same passage")])

    def test_question_fields_are_kept_and_checked(self):
        cleaned = flows.evals.clean_questions([{"question": "Q", "kind": "answer", "area": "refunds",
                                                "expected_source": "https://a.example/x"}, "Plain"])
        self.assertEqual(cleaned, [{"question": "Q", "expected": "", "expect_blocked": False, "kind": "answer",
                                    "area": "refunds", "expected_source": "https://a.example/x"},
                                   {"question": "Plain", "expected": "", "expect_blocked": False}])
        with self.assertRaises(ValueError):
            flows.evals.clean_questions([{"question": "Q", "kind": "guess"}])

    def test_the_evaluation_api(self):
        import web_api
        api = SetupApi()
        with patch.object(evaluation, "write_with_llm", side_effect=self.writer), \
                patch.object(evaluation, "scope_with_llm", side_effect=self.scoper), \
                patch.object(web_api, "_publish_check"):
            status, view = api.call(web_api.setup_evaluation_prepare)
            self.assertEqual((status, view["status"], len(view["questions"])), (200, "ready", 6))
            view = json.loads(web_api.setup_evaluation_view(api.Request()).body)
            self.assertEqual(view["record"]["questions"], 6)
            with patch.object(evaluation, "_start_thread"):                   # the work never runs
                self.assertEqual(api.call(web_api.setup_evaluation_prepare)[0], 200)
                self.assertEqual(api.call(web_api.setup_evaluation_prepare)[0], 409)   # already preparing


if __name__ == "__main__":
    unittest.main()
