import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.core.models import (
    ActionStatus,
    ActionType,
    Domain,
    DomainField,
    InvalidTransition,
    ProposedAction,
    Workflow,
    WorkflowStep,
)
from norev.processor.central import CentralProcessor, SendRefused
from norev.processor.identity import LocalUser
from norev.processor.workflow import (
    AlreadyRunning,
    ExecutionRefused,
    ExecutionStore,
    StepRecord,
    StepState,
    WorkflowExecutor,
    step_payload,
)
from norev.testing.fakes import FakeLlmClient, FixedClock, InMemorySessionStore
from test_processor_send import RecordingAuditLog

AT = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
SECRET = "Ion Popescu, CNP 1800101123456"
WAIT = 5

EXPORT = WorkflowStep("export", connector="local_document", sends=("title", "owner", "topic"), label="Exportă")
DOMAIN = Domain(
    name="test",
    label="Test",
    model="m.gguf",
    context=4096,
    roles=(),
    action_types=(
        ActionType(
            name="minutes",
            fields=(DomainField("topic", "string", True),),
            workflow=Workflow((WorkflowStep("prepare", label="Pregătește"), EXPORT, WorkflowStep("notify", label="Anunță"))),
        ),
        ActionType(
            name="invite",
            fields=(DomainField("topic", "string", True),),
            workflow=Workflow((WorkflowStep("send", connector="google_calendar", sends=("topic",)),)),
        ),
        ActionType(
            name="script",
            fields=(),
            workflow=Workflow((WorkflowStep("run_shell", label="Run"),)),  # no known implementation
        ),
    ),
    prompt_template="",
    answer_schema={},
)


class InMemoryExecutionStore:
    def __init__(self) -> None:
        self.steps: dict[str, dict[str, StepRecord]] = {}

    def load_steps(self, action_id: str) -> dict[str, StepRecord]:
        return dict(self.steps.get(action_id, {}))

    def save_step(self, action_id: str, record: StepRecord) -> None:
        self.steps.setdefault(action_id, {})[record.step] = record


class WorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.export_dir = Path(tmp.name)
        self.timeline: list[str] = []
        self.audit = RecordingAuditLog(self.timeline)
        self.processor = CentralProcessor(
            FakeLlmClient([]), self.audit, identity=LocalUser("local:ana"), clock=FixedClock(AT),
            connectors=[LocalDocumentConnector(self.export_dir)],
        )
        self.sessions = InMemorySessionStore()
        self.steps = InMemoryExecutionStore()
        self.calls: list[str] = []
        self.notify_error: BaseException | None = None
        self._save(self._approved())

    def _executor(self) -> WorkflowExecutor:
        def prepare(action: ProposedAction) -> None:
            self.calls.append("prepare")

        def notify(action: ProposedAction) -> dict:
            self.calls.append("notify")
            if self.notify_error is not None:
                raise self.notify_error
            return {"notified": True}

        return WorkflowExecutor(self.processor, self.sessions, self.steps, DOMAIN, {"prepare": prepare, "notify": notify})

    def _approved(self, action_type: str = "minutes") -> ProposedAction:
        action = ProposedAction(
            id="s1-1", title="Send the Q4 budget", description="d", transcript_excerpt="e",
            action_type=action_type, fields={"topic": "Budget"},
        )
        step = DOMAIN.action_type(action_type).workflow.steps
        payload = {}
        for s in step:
            if s.connector:
                payload.update(step_payload(action, s))
        action.approve(payload)
        return action

    def _save(self, action: ProposedAction) -> None:
        self.sessions.save_actions("s1", [action])

    def _action(self) -> ProposedAction:
        [action] = self.sessions.load_actions("s1")
        return action

    def _documents(self) -> list[str]:
        return sorted(os.listdir(self.export_dir))

    def _assert_nothing_ran(self) -> None:
        self.assertEqual((self.calls, self.timeline, self._documents(), self.steps.steps), ([], [], [], {}))

    def test_fake_is_an_execution_store(self) -> None:
        self.assertIsInstance(self.steps, ExecutionStore)

    def test_success_exports_what_the_preview_showed(self) -> None:
        executor = self._executor()
        [preview] = executor.preview("s1", "s1-1")
        self.assertEqual(preview["payload"], {"title": "Send the Q4 budget", "owner": None, "topic": "Budget"})
        self.assertEqual(Path(preview["destination"]).parent, self.export_dir)

        outcome = executor.execute("s1", "s1-1")

        self.assertEqual(outcome.status, "executed")
        self.assertEqual(self._action().status, ActionStatus.EXECUTED)
        self.assertEqual(self.calls, ["prepare", "notify"])
        self.assertEqual(self.timeline, ["audit", "ok"])
        self.assertEqual({s: r.state for s, r in self.steps.steps["s1-1"].items()}, dict.fromkeys(["prepare", "export", "notify"], StepState.DONE))
        written = Path(self.steps.steps["s1-1"]["export"].result["path"])
        self.assertEqual(str(written), preview["destination"])
        self.assertEqual(written.read_text(encoding="utf-8"), preview["content"])

    def test_unknown_local_step_is_refused_before_anything_runs(self) -> None:
        self._save(self._approved("script"))
        with self.assertRaises(ExecutionRefused):
            self._executor().execute("s1", "s1-1")
        self._assert_nothing_ran()
        self.assertEqual(self._action().status, ActionStatus.APPROVED)

    def test_unknown_connector_is_refused(self) -> None:
        self._save(self._approved("invite"))  # google_calendar is not registered
        with self.assertRaises(ExecutionRefused):
            self._executor().execute("s1", "s1-1")
        self._assert_nothing_ran()

    def test_action_that_is_not_approved_is_refused(self) -> None:
        self._save(ProposedAction(id="s1-1", title="t", description="d", transcript_excerpt="e", action_type="minutes"))
        with self.assertRaises(ExecutionRefused):
            self._executor().execute("s1", "s1-1")
        self._assert_nothing_ran()

    def test_payload_changed_after_approval_runs_nothing(self) -> None:
        action = self._approved()
        action.fields["topic"] = "Budget, edited after approval"
        self._save(action)
        with self.assertRaises(SendRefused):
            self._executor().execute("s1", "s1-1")
        self._assert_nothing_ran()  # not even the local step before the export
        self.assertEqual(self._action().status, ActionStatus.APPROVED)

    def test_failed_step_marks_failed_without_its_text(self) -> None:
        self.notify_error = RuntimeError(f"could not notify {SECRET}")
        outcome = self._executor().execute("s1", "s1-1")

        self.assertEqual(outcome.status, "failed")
        action = self._action()
        self.assertEqual(action.status, ActionStatus.FAILED)
        self.assertEqual(action.error, "Step Anunță failed (RuntimeError).")
        states = {s: r.state for s, r in self.steps.steps["s1-1"].items()}
        self.assertEqual(states, {"prepare": StepState.DONE, "export": StepState.DONE, "notify": StepState.FAILED})
        self.assertNotIn(SECRET, repr(outcome) + repr(action) + repr(self.steps.steps))

    def test_retry_is_manual_and_skips_the_steps_already_done(self) -> None:
        self.notify_error = RuntimeError("down")
        executor = self._executor()
        executor.execute("s1", "s1-1")
        with self.assertRaises(ExecutionRefused):  # no automatic retry: Failed must be retried first
            executor.execute("s1", "s1-1")

        self.notify_error = None
        executor.retry("s1", "s1-1")
        self.assertEqual(self._action().approved_payload, self._approved().approved_payload)
        outcome = executor.execute("s1", "s1-1")

        self.assertEqual(outcome.status, "executed")
        self.assertEqual(self.calls, ["prepare", "notify", "notify"])  # prepare and export not repeated
        self.assertEqual(self.timeline, ["audit", "ok"])
        self.assertEqual(len(self._documents()), 1)

    def test_retry_needs_a_failed_action(self) -> None:
        with self.assertRaises(InvalidTransition):
            self._executor().retry("s1", "s1-1")

    def test_second_execute_while_running_is_refused(self) -> None:
        inside, finish = threading.Event(), threading.Event()
        executor = self._executor()
        executor._local["prepare"] = lambda action: (inside.set(), finish.wait(WAIT), None)[-1]
        first = threading.Thread(target=executor.execute, args=("s1", "s1-1"))
        first.start()
        self.assertTrue(inside.wait(WAIT))
        with self.assertRaises(AlreadyRunning):  # the double click
            executor.execute("s1", "s1-1")
        finish.set()
        first.join(WAIT)
        self.assertEqual(self._action().status, ActionStatus.EXECUTED)
        with self.assertRaises(ExecutionRefused):  # and once done, it is done
            executor.execute("s1", "s1-1")
        self.assertEqual(self.timeline, ["audit", "ok"])

    def test_after_a_restart_an_uncertain_step_is_not_run_again(self) -> None:
        self.notify_error = KeyboardInterrupt()  # the app stops in the middle of a step
        with self.assertRaises(KeyboardInterrupt):
            self._executor().execute("s1", "s1-1")
        self.notify_error = None

        restarted = self._executor()  # new process: same stores, nothing running
        outcome = restarted.execute("s1", "s1-1")
        self.assertEqual((outcome.status, outcome.uncertain_step), ("uncertain", "notify"))
        self.assertEqual(self.calls, ["prepare", "notify"], "the uncertain step ran again")
        self.assertEqual(self._action().status, ActionStatus.APPROVED)

        restarted.reconcile("s1", "s1-1", "notify", happened=True)  # the User checked: it was done
        self.assertEqual(restarted.execute("s1", "s1-1").status, "executed")
        self.assertEqual(self.calls, ["prepare", "notify"])
        self.assertEqual(self.timeline, ["audit", "ok"])

    def test_uncertain_step_the_user_says_did_not_happen_runs_again(self) -> None:
        self.steps.save_step("s1-1", StepRecord("prepare", StepState.DONE))
        self.steps.save_step("s1-1", StepRecord("export", StepState.STARTED))
        executor = self._executor()
        self.assertEqual(executor.execute("s1", "s1-1").status, "uncertain")
        self.assertEqual(self.timeline, [])

        executor.reconcile("s1", "s1-1", "export", happened=False)
        self.assertEqual(executor.execute("s1", "s1-1").status, "executed")
        self.assertEqual(self.calls, ["notify"])
        self.assertEqual(len(self._documents()), 1)

    def test_only_an_uncertain_step_can_be_reconciled(self) -> None:
        with self.assertRaises(ExecutionRefused):
            self._executor().reconcile("s1", "s1-1", "export", happened=True)


if __name__ == "__main__":
    unittest.main()
