"""#19 end to end with fakes: recorded audio -> Transcript with Speakers -> translation -> extraction ->
Domain stage -> review (#18 preview/approve) -> Workflow executor (#23) -> local document, audited.

Medical's Workflows need Google Calendar (next stage), so it stops at preview with nothing sent.
"""
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.core.models import ActionStatus, UnsupportedWorkflow
from norev.processor.central import CentralProcessor
from norev.processor.identity import LocalUser
from norev.processor.payloads import LocalDocumentPayloadBuilder
from norev.processor.workflow import WorkflowExecutor
from norev.testing.fakes import FakeLlmClient, FixedClock
from norev.usecases.approve import approve_action, preview_action
from test_processor_send import RecordingAuditLog
from test_processor_workflow import InMemoryExecutionStore
from test_usecases_session_flow import ANSWERS, CORPORATE, MEDICAL, OTHERS, TRANSLATION, Flow

AT = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
USER = "local:ana"


class SessionToLocalDocumentTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.export_dir = Path(tmp.name)
        self.audit = RecordingAuditLog([])
        self.processor = CentralProcessor(FakeLlmClient([]), self.audit, identity=LocalUser(USER), clock=FixedClock(AT),
                                          connectors=[LocalDocumentConnector(self.export_dir)])
        self.local_steps_run: list[str] = []

    def _review(self, flow: Flow, domain):
        """The User previews and approves the first Proposed Action through #18's use cases."""
        revision = flow.store.load_actions_snapshot("s1").revision
        snapshot = preview_action("s1", "s1-1", {}, expected_revision=revision, store=flow.store, domain=domain,
                                  builder=LocalDocumentPayloadBuilder(self.export_dir))
        approve_action("s1", "s1-1", expected_revision=snapshot.revision, store=flow.store, domain=domain,
                       clock=FixedClock(AT), approved_by=USER)

    def _execute(self, flow: Flow, domain):
        # shortcut: the app's local steps (build_document, save_result) are not implemented yet (#23); stand-ins here
        local = {name: (lambda action, name=name: self.local_steps_run.append(name))
                 for name in ("build_document", "save_result")}
        return WorkflowExecutor(self.processor, flow.store, InMemoryExecutionStore(), domain, local).execute("s1", "s1-1")

    def test_corporate_and_others_end_in_an_approved_local_document(self):
        for domain, expected in ((CORPORATE, "deliverable: report"), (OTHERS, "deliverable: report")):
            with self.subTest(domain=domain.name):
                self.audit.events.clear()
                for f in os.listdir(self.export_dir):
                    os.unlink(self.export_dir / f)
                flow = Flow(domain, [TRANSLATION, ANSWERS[domain.name]])
                flow.run()
                self._review(flow, domain)

                outcome = self._execute(flow, domain)

                self.assertEqual(outcome.status, "executed")
                action = flow.store.load_actions("s1")[0]
                self.assertEqual(action.status, ActionStatus.EXECUTED)
                documents = os.listdir(self.export_dir)
                self.assertEqual(len(documents), 1)
                text = (self.export_dir / documents[0]).read_text(encoding="utf-8")
                self.assertIn("title: Send the report", text)
                self.assertIn(expected, text)
                # exactly the approved payload was sent, and the send is in the audit log
                [event] = self.audit.events
                self.assertEqual((event.action_id, event.connector, event.approved_by), ("s1-1", "local_document", USER))
                self.assertEqual(event.payload, action.approved_payload["steps"][0]["payload"])

    def test_medical_stops_at_review_until_google_calendar(self):
        complete = {"proposed_actions": [{"id": "s1-1", "action_type": "schedule_visit", "fields": {
            "reason": "Report", "visit_date": "2026-11-13", "visit_time": "09:00", "visit_end_time": "09:30",
            "location": "Clinic"}}]}
        flow = Flow(MEDICAL, [TRANSLATION, complete])
        flow.run()
        revision = flow.store.load_actions_snapshot("s1").revision
        with self.assertRaises(UnsupportedWorkflow):
            preview_action("s1", "s1-1", {}, expected_revision=revision, store=flow.store, domain=MEDICAL,
                           builder=LocalDocumentPayloadBuilder(self.export_dir))
        self.assertEqual(flow.store.load_actions("s1")[0].status, ActionStatus.PROPOSED)
        self.assertEqual((os.listdir(self.export_dir), self.audit.events), ([], []))

    def test_rerun_after_execution_is_refused_and_keeps_the_executed_action(self):
        flow = Flow(CORPORATE, [TRANSLATION, ANSWERS["corporate"], ANSWERS["corporate"]])
        flow.run()
        self._review(flow, CORPORATE)
        self._execute(flow, CORPORATE)
        with self.assertRaises(ValueError):
            flow.run()
        self.assertEqual([a.status for a in flow.store.load_actions("s1")], [ActionStatus.EXECUTED])
        self.assertEqual(len(os.listdir(self.export_dir)), 1)


if __name__ == "__main__":
    unittest.main()
