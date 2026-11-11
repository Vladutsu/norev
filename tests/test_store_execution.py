"""End to end (#51): WorkflowExecutor + CentralProcessor + EncryptedStore + local_document.

The step states and send results live in the encrypted store, so a step left started by a crash is
still uncertain after the store is reopened: execute() reports it and does not send again (#23).
"""
import os
import tempfile
import unittest
from pathlib import Path

from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.adapters.store.encrypted import StoreSettings, create_key_file, open_store
from norev.core.models import ActionStatus, ProposedAction, SendResult, Session, StepState
from norev.processor.central import CentralProcessor
from norev.processor.identity import LocalUser
from norev.processor.payloads import LocalDocumentPayloadBuilder
from norev.processor.workflow import WorkflowExecutor
from norev.testing.fakes import FakeLlmClient, FixedClock
from norev.usecases.approve import approve_action, preview_action
from test_processor_workflow import AT, DOMAIN


class Crash(BaseException):
    """The app stops mid-send: not an Exception, so nothing catches it and no result is written."""


class CrashingConnector(LocalDocumentConnector):
    def send(self, payload):
        raise Crash()


class StoreExecutionTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.export_dir = root / "exports"
        self.export_dir.mkdir()
        self.settings = StoreSettings(path=root / "data" / "norev.db", key_file=root / "keys" / "norev.key")
        create_key_file(self.settings.key_file)
        self.store = self.open()
        self.calls: list[str] = []
        self.approve()

    def open(self):
        store = open_store(self.settings)
        self.addCleanup(store.close)
        return store

    def approve(self) -> None:
        self.store.save_session(Session(id="s1", started_at=AT, domain=DOMAIN.name))
        action = ProposedAction(id="s1-1", title="Send the Q4 budget", description="d", transcript_excerpt="e",
                                action_type="minutes", fields={"topic": "Budget"})
        revision = self.store.save_actions("s1", [action], expected_revision=0)
        snapshot = preview_action("s1", "s1-1", {}, expected_revision=revision, store=self.store, domain=DOMAIN,
                                  builder=LocalDocumentPayloadBuilder(self.export_dir))
        approve_action("s1", "s1-1", expected_revision=snapshot.revision, store=self.store, domain=DOMAIN,
                       clock=FixedClock(AT), approved_by="local:ana")

    def executor(self, store, connector) -> WorkflowExecutor:
        processor = CentralProcessor(FakeLlmClient([]), store, identity=LocalUser("local:processor"),
                                     clock=FixedClock(AT), connectors=[connector])
        local = {"prepare": lambda a: self.calls.append("prepare"), "notify": lambda a: self.calls.append("notify")}
        return WorkflowExecutor(processor, store, store, DOMAIN, local)

    def test_a_step_started_before_a_restart_is_uncertain_and_not_sent_again(self) -> None:
        with self.assertRaises(Crash):
            self.executor(self.store, CrashingConnector(self.export_dir)).execute("s1", "s1-1")
        self.store.close()

        again = self.open()
        self.assertEqual(again.load_steps("s1-1")["export"].state, StepState.STARTED)
        self.assertEqual(len(again.load_events()), 1)  # the attempt is audited...
        self.assertEqual(again.load_results(), [])  # ...with no result: an uncertain send

        outcome = self.executor(again, LocalDocumentConnector(self.export_dir)).execute("s1", "s1-1")

        self.assertEqual((outcome.status, outcome.uncertain_step), ("uncertain", "export"))
        self.assertEqual(os.listdir(self.export_dir), [], "the uncertain step was sent again")
        self.assertEqual(len(again.load_events()), 1)
        self.assertEqual(self.calls, ["prepare"])  # only from the first run
        self.assertEqual(again.load_actions("s1")[0].status, ActionStatus.APPROVED)

    def test_a_successful_run_is_kept_after_reopening(self) -> None:
        outcome = self.executor(self.store, LocalDocumentConnector(self.export_dir)).execute("s1", "s1-1")
        self.assertEqual(outcome.status, "executed")
        self.store.close()

        again = self.open()
        steps = again.load_steps("s1-1")
        self.assertEqual({name: r.state for name, r in steps.items()},
                         {"prepare": StepState.DONE, "export": StepState.DONE, "notify": StepState.DONE})
        self.assertEqual(steps["export"].result, outcome.steps["export"].result)
        self.assertEqual(again.load_results(), [SendResult(AT, "s1-1", "local_document", ok=True)])
        self.assertEqual(again.load_actions("s1")[0].status, ActionStatus.EXECUTED)
        self.assertEqual(len(os.listdir(self.export_dir)), 1)


if __name__ == "__main__":
    unittest.main()
