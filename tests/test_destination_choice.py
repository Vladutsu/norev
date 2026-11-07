"""The destination of a work item, chosen per action at preview (#72): Domain format, approval,
payload routing, the Workflow executor, and the desktop UI with Trello on the real encrypted store."""
import copy
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.adapters.connectors.trello import TrelloAccount, TrelloConnector, TrelloSettings
from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.adapters.store.encrypted import KEY_BYTES, EncryptedStore
from norev.core.models import ActionStatus, ProposedAction, Segment, Session, Transcript, ValidationError
from norev.processor.central import CentralProcessor
from norev.processor.compose import LOCAL_STEPS, build_trello, desktop_payload_builder
from norev.processor.identity import LocalUser
from norev.processor.payloads import ConnectorPayloadBuilder, LocalDocumentPayloadBuilder, TrelloPayloadBuilder
from norev.processor.trello_account import TrelloLink
from norev.processor.workflow import ExecutionRefused, WorkflowExecutor
from norev.testing.fakes import FakeLlmClient, FixedClock, InMemorySessionStore
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge
from norev.ui.session_backend import SessionBackend
from norev.usecases.approve import approve_action, domain_digest, preview_action
from test_connectors_google_auth import MemorySecrets
from test_connectors_trello import GOOD, KEY, LIST_ID, FakeTrello
from test_processor_send import FakeConnector
from test_processor_workflow import InMemoryExecutionStore

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
USER = "local:ana"
CORPORATE = load_domain(DOMAINS / "corporate")
CHOSEN = {"id": LIST_ID, "name": "Echipa / De făcut"}


def work_item() -> ProposedAction:
    return ProposedAction("s1-1", "Send the Q4 report", "Send the draft to the client.",
                          "can you send the Q4 report by Friday?", owner="Elena", due_date=date(2026, 11, 13),
                          action_type="work_item", fields={"deliverable": "the Q4 report"})


class CoreTest(unittest.TestCase):
    """Approval and execution with fakes: only the default or an alternative, and what was approved."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.export = Path(tmp.name)
        self.sessions = InMemorySessionStore()
        self.sessions.save_session(Session(id="s1", started_at=NOW, domain="corporate"))
        self.revision = self.sessions.save_actions("s1", [work_item()], expected_revision=0)
        self.builder = ConnectorPayloadBuilder({
            "local_document": LocalDocumentPayloadBuilder(self.export),
            "trello": TrelloPayloadBuilder(lambda: CHOSEN, "Europe/Bucharest", new_id=lambda: "a" * 32)})

    def preview(self, options: dict):
        return preview_action("s1", "s1-1", options, expected_revision=self.revision, store=self.sessions,
                              domain=CORPORATE, builder=self.builder)

    def approve(self, options: dict) -> dict:
        snapshot = self.preview(options)
        approve_action("s1", "s1-1", expected_revision=snapshot.revision, store=self.sessions, domain=CORPORATE,
                       clock=FixedClock(NOW), approved_by=USER)
        return self.sessions.load_actions("s1")[0].approved_payload

    def test_the_domain_lists_trello_as_an_alternative(self) -> None:
        [step] = [s for s in CORPORATE.action_type("work_item").workflow.steps if s.connector]
        self.assertEqual(step.connectors(), ("local_document", "trello", "jira"))
        self.assertIn("build_document", LOCAL_STEPS)

    def test_default_and_chosen_destination(self) -> None:
        snapshot = self.preview({})
        self.assertEqual(snapshot.actions[0].prepared_payload["steps"][0]["connector"], "local_document")
        self.revision = snapshot.revision
        plan = self.approve({"destination": "trello"})
        self.assertEqual(plan["domain_digest"], domain_digest(CORPORATE))  # checked against the real Domain
        [step] = plan["steps"]
        self.assertEqual((step["step"], step["connector"]), ("save_document", "trello"))
        self.assertEqual(step["payload"]["list"], CHOSEN)

    def test_only_the_steps_connectors_can_be_chosen(self) -> None:
        for destination in ("google_calendar", "email_draft", "slack", "", ["trello"]):
            with self.subTest(destination=destination), self.assertRaises(ValidationError) as caught:
                self.preview({"destination": destination})
            self.assertEqual(caught.exception.code, "unknown_destination")

    def test_a_plan_changed_to_another_connector_is_not_approved(self) -> None:
        snapshot = self.preview({"destination": "trello"})
        action = snapshot.actions[0]
        action.prepared_payload["steps"][0]["connector"] = "email_draft"
        revision = self.sessions.save_actions("s1", [action], expected_revision=snapshot.revision)
        with self.assertRaises(ValidationError) as caught:
            approve_action("s1", "s1-1", expected_revision=revision, store=self.sessions, domain=CORPORATE,
                           clock=FixedClock(NOW), approved_by=USER)
        self.assertEqual(caught.exception.code, "invalid_step")

    def executor(self, *connectors) -> tuple[WorkflowExecutor, list]:
        timeline: list = []
        from test_processor_send import RecordingAuditLog

        processor = CentralProcessor(FakeLlmClient([]), RecordingAuditLog(timeline), identity=LocalUser("local:p"),
                                     clock=FixedClock(NOW), connectors=connectors)
        return WorkflowExecutor(processor, self.sessions, InMemoryExecutionStore(), CORPORATE, LOCAL_STEPS), timeline

    def test_the_executor_sends_with_the_approved_destination(self) -> None:
        plan = self.approve({"destination": "trello"})
        timeline: list = []
        local, trello = FakeConnector(timeline, "local_document"), FakeConnector(timeline, "trello")
        executor, _ = self.executor(local, trello)
        self.assertEqual(executor.execute("s1", "s1-1").status, "executed")
        self.assertEqual(local.sent, [])
        [sent] = trello.sent
        self.assertEqual((sent["list"], sent["card"]), (plan["steps"][0]["payload"]["list"], plan["steps"][0]["payload"]["card"]))

    def test_a_destination_not_set_up_here_runs_nothing(self) -> None:
        self.approve({"destination": "trello"})
        timeline: list = []
        executor, audit = self.executor(FakeConnector(timeline, "local_document"))
        with self.assertRaises(ExecutionRefused):
            executor.execute("s1", "s1-1")
        self.assertEqual((timeline, audit), ([], []))

    def test_an_approved_plan_changed_to_another_connector_runs_nothing(self) -> None:
        self.approve({"destination": "trello"})
        action = self.sessions.load_actions_snapshot("s1").actions[0]
        changed = copy.deepcopy(action.approved_payload)
        changed["steps"][0]["connector"] = "email_draft"
        action.approved_payload = changed
        self.sessions.save_actions("s1", [action], expected_revision=self.sessions.load_actions_snapshot("s1").revision)
        timeline: list = []
        executor, audit = self.executor(FakeConnector(timeline, "email_draft"), FakeConnector(timeline, "trello"))
        with self.assertRaises(ExecutionRefused):
            executor.execute("s1", "s1-1")
        self.assertEqual((timeline, audit), ([], []))


class CompositionTest(unittest.TestCase):
    def test_no_trello_without_an_encrypted_store_and_not_available_without_a_key(self) -> None:
        link, connector = build_trello(InMemorySessionStore(), TrelloSettings(KEY))
        self.assertIsNone(connector)
        self.assertEqual(link.status()["key_settable"], False)
        link, connector = build_trello(MemorySecrets(), TrelloSettings(KEY))
        self.assertIsInstance(connector, TrelloConnector)
        self.assertEqual(link.status(), {"available": True, "connected": False, "list": None, "key_settable": True,
                                         "key_in_app": False})

    def test_the_team_leader_enters_the_key_in_the_app(self) -> None:  # ADR 0006
        secrets = MemorySecrets()
        link, connector = build_trello(secrets, TrelloSettings(None))
        self.assertEqual(link.status(), {"available": False, "connected": False, "list": None, "key_settable": True,
                                         "key_in_app": False})
        for bad in ("", "x" * 32, "a" * 31, None, "a" * 65, "a" * 32 + " b"):
            self.assertEqual(link.set_key(bad)["failed"], "invalid_key")
        self.assertEqual(secrets.values, {})
        status = link.set_key(" " + "Ab12" * 8 + " ")
        self.assertEqual((status["failed"], status["available"]), (None, True))
        self.assertEqual(secrets.values, {"trello.api_key": "Ab12" * 8})
        self.assertTrue(status["key_in_app"])
        self.assertEqual(link.set_key("cd34" * 8)["failed"], None)  # changed while not connected
        secrets.values["trello.token"] = "t" * 40
        self.assertEqual(link.set_key("ef56" * 8)["failed"], "disconnect_first")
        self.assertEqual(secrets.values["trello.api_key"], "cd34" * 8)


class DesktopTrelloTest(unittest.TestCase):
    """From the UI bridge: connect Trello with a pasted token, choose the list, pick Trello for a work
    item, preview, approve and Execute, on the encrypted store, to the fake Trello."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.export = root / "export"
        self.store = EncryptedStore(root / "data" / "norev.db", os.urandom(KEY_BYTES))
        self.addCleanup(self.store.close)
        self.trello = FakeTrello()
        self.addCleanup(self.trello.close)
        account = TrelloAccount(TrelloSettings(KEY), self.store, api=self.trello.api, timeout=5)
        self.opened: list[str] = []
        self.link = TrelloLink(account, open_browser=lambda url: self.opened.append(url) or True)
        processor = CentralProcessor(FakeLlmClient([]), self.store, identity=LocalUser(USER), clock=FixedClock(NOW),
                                     connectors=[LocalDocumentConnector(self.export), TrelloConnector(account)])

        @contextmanager
        def no_llm():
            yield None

        self.bridge = Bridge(SessionBackend(
            self.store, approved_by=USER, processor=processor, export_dir=self.export, llm_session=no_llm,
            record_source=lambda: iter(()), clock=FixedClock(NOW),
            builder=desktop_payload_builder(self.export, calendar=False, trello=self.link), trello=self.link))
        transcript = self.store.save_transcript("s1", Transcript((Segment(0, 1, "Send the Q4 report.", "S1"),), "en"))
        self.store.save_session(Session(id="s1", started_at=NOW, domain="corporate", transcript_ref=transcript))
        self.store.save_actions("s1", [work_item()], expected_revision=0)

    def ok(self, result: dict):
        self.assertTrue(result["ok"], result)
        return result["data"]

    def code(self, result: dict) -> str:
        self.assertFalse(result["ok"], result)
        return result["error"]["code"]

    def destinations(self) -> list:
        return self.ok(self.bridge.review("s1"))["actions"][0]["destinations"]

    def connect(self) -> None:
        self.ok(self.bridge.trello_authorize())
        self.assertTrue(self.opened[0].startswith("https://trello.com/1/authorize?"))
        self.assertIsNone(self.ok(self.bridge.trello_connect(GOOD))["failed"])
        boards = self.ok(self.bridge.trello_boards())["boards"]
        self.assertEqual(boards[0]["lists"][0]["id"], LIST_ID)
        status = self.ok(self.bridge.trello_choose_list(LIST_ID))
        self.assertEqual(status["list"], CHOSEN)

    def test_trello_becomes_a_ready_destination_once_connected_with_a_list(self) -> None:
        self.assertEqual(self.ok(self.bridge.trello_status()), {"available": True, "connected": False, "list": None,
                                                               "key_settable": True, "key_in_app": False})
        self.assertEqual(self.destinations(), [
            {"connector": "local_document", "label": "Document local", "ready": True},
            {"connector": "trello", "label": "Trello", "ready": False},
            {"connector": "jira", "label": "Jira", "ready": False}])
        revision = self.ok(self.bridge.review("s1"))["revision"]
        self.assertEqual(self.code(self.bridge.preview("s1", "s1-1", revision, "trello")), "unknown_destination")
        self.connect()
        self.assertTrue(self.destinations()[1]["ready"])
        self.assertEqual(json.loads(self.store.load_secret("trello.list")), CHOSEN)  # kept encrypted

    def test_a_wrong_token_is_a_fixed_code_and_saves_nothing(self) -> None:
        self.assertEqual(self.ok(self.bridge.trello_connect("x" * 64))["failed"], "invalid_token")
        self.assertIsNone(self.store.load_secret("trello.token"))

    def test_work_item_sent_to_trello(self) -> None:
        self.connect()
        revision = self.ok(self.bridge.review("s1"))["revision"]
        review = self.ok(self.bridge.preview("s1", "s1-1", revision, "trello"))
        [step] = review["actions"][0]["plan"]
        self.assertEqual((step["connector"], step["connector_label"]), ("trello", "Trello"))
        self.assertEqual(step["payload"]["list"], CHOSEN)
        review = self.ok(self.bridge.approve("s1", "s1-1", review["revision"]))
        review = self.ok(self.bridge.execute("s1", "s1-1"))
        self.assertEqual(review["actions"][0]["status"], "executed")
        [card] = self.trello.cards
        self.assertEqual((card["idList"], card["name"]), (LIST_ID, "Send the Q4 report"))
        self.assertEqual(card["desc"], step["payload"]["card"]["desc"])
        self.assertEqual([(r.connector, r.ok) for r in self.store.load_results()], [("trello", True)])
        self.assertEqual(list(self.export.glob("*")) if self.export.exists() else [], [])  # no local document

    def test_disconnected_after_approval_fails_and_retry_after_reconnecting_sends_once(self) -> None:
        self.connect()
        revision = self.ok(self.bridge.review("s1"))["revision"]
        review = self.ok(self.bridge.preview("s1", "s1-1", revision, "trello"))
        self.ok(self.bridge.approve("s1", "s1-1", review["revision"]))
        self.ok(self.bridge.trello_disconnect())
        review = self.ok(self.bridge.execute("s1", "s1-1"))
        self.assertEqual(review["actions"][0]["status"], "failed")
        self.assertIn("NotConnected", review["actions"][0]["error"])
        self.connect()
        review = self.ok(self.bridge.retry("s1", "s1-1"))
        self.assertEqual(review["actions"][0]["status"], "executed")
        self.assertEqual(len(self.trello.cards), 1)
        self.assertEqual(self.store.load_actions("s1")[0].status, ActionStatus.EXECUTED)

    def test_the_demo_has_no_trello_and_no_other_destination(self) -> None:
        store = InMemorySessionStore()
        demo = Bridge(DemoBackend(store, approved_by=USER))
        self.assertEqual(self.ok(demo.trello_status()), {"available": False, "connected": False, "list": None})
        for call in (demo.trello_authorize(), demo.trello_connect(GOOD), demo.trello_boards(),
                     demo.trello_choose_list(LIST_ID), demo.trello_disconnect()):
            self.assertEqual(self.code(call), "mode_unavailable")


if __name__ == "__main__":
    unittest.main()
