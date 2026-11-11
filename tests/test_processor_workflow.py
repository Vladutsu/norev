import copy
import dataclasses
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
    Session,
    StaleRevision,
    Workflow,
    WorkflowStep,
)
from norev.processor.central import CentralProcessor, SendRefused
from norev.processor.identity import LocalUser
from norev.processor.payloads import LocalDocumentPayloadBuilder
from norev.processor.workflow import (
    AlreadyRunning,
    ExecutionRefused,
    ExecutionStore,
    StepRecord,
    StepState,
    WorkflowExecutor,
)
from norev.testing.fakes import FakeLlmClient, FixedClock, InMemorySessionStore
from norev.usecases.approve import approve_action, preview_action
from test_processor_send import RecordingAuditLog

AT = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
SECRET = "Ion Popescu, CNP 1800101123456"
WAIT = 5

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
            workflow=Workflow((
                WorkflowStep("prepare", label="Pregătește"),
                WorkflowStep("export", connector="local_document", sends=("title", "owner", "topic"), label="Exportă"),
                WorkflowStep("notify", label="Anunță"),
            )),
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
        self.processor = self._processor(LocalDocumentConnector(self.export_dir))
        self.sessions = InMemorySessionStore()
        self.steps = InMemoryExecutionStore()
        self.calls: list[str] = []
        self.notify_error: BaseException | None = None
        self.plan = self._approve()
        self.timeline.clear()  # approving sends nothing; start each test from here

    def _processor(self, *connectors) -> CentralProcessor:
        return CentralProcessor(FakeLlmClient([]), self.audit, identity=LocalUser("local:processor"),
                                clock=FixedClock(AT), connectors=connectors)

    def _approve(self, extra: dict | None = None) -> dict:
        """Through #18's use cases: save, preview with the local_document builder, approve."""
        self.sessions.save_session(Session(id="s1", started_at=AT, domain=DOMAIN.name))
        action = ProposedAction(id="s1-1", title="Send the Q4 budget", description="d", transcript_excerpt="e",
                                action_type="minutes", fields={"topic": "Budget"})
        others = [ProposedAction(id="s1-2", title="Other", description="d", transcript_excerpt="e", action_type="minutes",
                                 fields={"topic": "Other"})]
        revision = self.sessions.save_actions("s1", [action, *others], expected_revision=0)
        builder = LocalDocumentPayloadBuilder(self.export_dir)
        snapshot = preview_action("s1", "s1-1", extra or {}, expected_revision=revision, store=self.sessions,
                                  domain=DOMAIN, builder=builder)
        approve_action("s1", "s1-1", expected_revision=snapshot.revision, store=self.sessions, domain=DOMAIN,
                       clock=FixedClock(AT), approved_by="local:ana")
        return self._action().approved_payload

    def _executor(self, processor: CentralProcessor | None = None, domain: Domain = DOMAIN,
                  local: dict | None = None) -> WorkflowExecutor:
        def prepare(action: ProposedAction) -> None:
            self.calls.append("prepare")

        def notify(action: ProposedAction) -> dict:
            self.calls.append("notify")
            if self.notify_error is not None:
                raise self.notify_error
            return {"notified": True}

        steps = {"prepare": prepare, "notify": notify} if local is None else local
        return WorkflowExecutor(processor or self.processor, self.sessions, self.steps, domain, steps)

    def _action(self, action_id: str = "s1-1") -> ProposedAction:
        return next(a for a in self.sessions.load_actions("s1") if a.id == action_id)

    def _set_action(self, action: ProposedAction) -> None:
        snapshot = self.sessions.load_actions_snapshot("s1")
        actions = [action if a.id == action.id else a for a in snapshot.actions]
        self.sessions.save_actions("s1", actions, expected_revision=snapshot.revision)

    def _documents(self) -> list[str]:
        return sorted(os.listdir(self.export_dir))

    def _assert_nothing_ran(self) -> None:
        self.assertEqual((self.calls, self.timeline, self._documents(), self.steps.steps), ([], [], [], {}))
        self.assertEqual(self._action().status, ActionStatus.APPROVED)

    def test_fake_is_an_execution_store(self) -> None:
        self.assertIsInstance(self.steps, ExecutionStore)

    def test_success_writes_exactly_the_approved_document(self) -> None:
        [step] = self.plan["steps"]
        self.assertEqual(step["step"], "export")
        approved = step["payload"]
        self.assertEqual(Path(approved["destination"]).parent, self.export_dir)
        self.assertIn("topic: Budget", approved["content"])
        self.assertEqual(self._documents(), [], "preview or approval wrote the document")

        outcome = self._executor().execute("s1", "s1-1")

        self.assertEqual(outcome.status, "executed")
        self.assertEqual(self._action().status, ActionStatus.EXECUTED)
        self.assertEqual(self._action("s1-2").status, ActionStatus.PROPOSED)  # the other action is untouched
        self.assertEqual(self.calls, ["prepare", "notify"])
        self.assertEqual(self.timeline, ["audit", "ok"])
        [event] = self.audit.events
        self.assertEqual((event.payload, event.approved_by), (approved, "local:ana"))
        self.assertEqual({s: r.state for s, r in self.steps.steps["s1-1"].items()},
                         dict.fromkeys(["prepare", "export", "notify"], StepState.DONE))
        written = Path(self.steps.steps["s1-1"]["export"].result["path"])
        self.assertEqual(str(written), approved["destination"])
        self.assertEqual(written.read_text(encoding="utf-8"), approved["content"])

    def test_the_users_edited_document_is_what_gets_written(self) -> None:
        self.sessions = InMemorySessionStore()
        plan = self._approve({"export": {"content": "Edited by the User.\n"}})
        self._executor().execute("s1", "s1-1")
        [name] = self._documents()
        self.assertEqual((self.export_dir / name).read_text(encoding="utf-8"), "Edited by the User.\n")
        self.assertEqual(str(self.export_dir / name), plan["steps"][0]["payload"]["destination"])

    def test_unknown_local_step_is_refused_before_anything_runs(self) -> None:
        with self.assertRaises(ExecutionRefused):
            self._executor(local={"notify": lambda action: None}).execute("s1", "s1-1")  # no "prepare"
        self._assert_nothing_ran()

    def test_unregistered_connector_is_refused(self) -> None:
        with self.assertRaises(ExecutionRefused):
            self._executor(processor=self._processor()).execute("s1", "s1-1")
        self._assert_nothing_ran()

    def test_action_that_is_not_approved_is_refused(self) -> None:
        with self.assertRaises(ExecutionRefused):
            self._executor().execute("s1", "s1-2")
        self._assert_nothing_ran()

    def test_domain_changed_after_approval_runs_nothing(self) -> None:
        changed = dataclasses.replace(DOMAIN, label="Test, edited")  # another digest
        with self.assertRaises(ExecutionRefused):
            self._executor(domain=changed).execute("s1", "s1-1")
        self._assert_nothing_ran()

    def test_plan_without_the_step_runs_nothing(self) -> None:
        action = self._action()
        action.approved_payload = {**copy.deepcopy(action.approved_payload), "steps": []}
        self._set_action(action)
        with self.assertRaises(SendRefused):
            self._executor().execute("s1", "s1-1")
        self._assert_nothing_ran()  # not even the local step before the export

    def test_a_result_that_cannot_be_recorded_leaves_the_step_uncertain(self) -> None:
        def no_result(result) -> None:
            raise OSError("disk full")

        self.audit.record_result = no_result
        executor = self._executor()
        outcome = executor.execute("s1", "s1-1")

        self.assertEqual((outcome.status, outcome.uncertain_step), ("uncertain", "export"))
        self.assertEqual(self.steps.load_steps("s1-1")["export"].state, StepState.STARTED)
        self.assertEqual(self._action().status, ActionStatus.APPROVED)
        self.assertEqual(len(self._documents()), 1)  # the send did happen
        self.assertEqual(executor.execute("s1", "s1-1").status, "uncertain")  # and is not sent again
        self.assertEqual(len(self._documents()), 1)
        self.assertEqual(self.calls, ["prepare"])

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
        self.assertEqual(self._action().approved_payload, self.plan)
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

    def test_other_actions_changing_meanwhile_are_kept(self) -> None:
        executor = self._executor()

        def prepare(action: ProposedAction) -> None:  # the User rejects another action while this one runs
            other = self._action("s1-2")
            other.reject()
            self._set_action(other)

        executor._local["prepare"] = prepare
        self.assertEqual(executor.execute("s1", "s1-1").status, "executed")
        self.assertEqual(self._action().status, ActionStatus.EXECUTED)
        self.assertEqual(self._action("s1-2").status, ActionStatus.REJECTED)

    def test_action_decided_elsewhere_while_running_is_not_overwritten(self) -> None:
        executor = self._executor()

        def prepare(action: ProposedAction) -> None:  # another process finished the same action meanwhile
            same = self._action()
            same.mark_executed()
            self._set_action(same)

        executor._local["prepare"] = prepare
        with self.assertRaises(StaleRevision):
            executor.execute("s1", "s1-1")
        self.assertEqual(self._action().status, ActionStatus.EXECUTED)

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

    def test_session_of_another_domain_is_refused(self) -> None:
        other = dataclasses.replace(DOMAIN, name="corporate")
        with self.assertRaises(ExecutionRefused):
            self._executor(domain=other).execute("s1", "s1-1")
        self._assert_nothing_ran()


if __name__ == "__main__":
    unittest.main()
