"""Workflow executor (#23): runs the Workflow of an approved Proposed Action, one step at a time.

- Only known steps run. A step with a Connector goes through the Central Processor, which checks the
  approved payload and audits the send. A step without one must be in the explicit registry of local
  steps the app gives the executor. Anything else refuses the whole Workflow before any step runs, so
  a Domain or a model answer cannot name code or commands to run.
- The approved payload is #18's approval plan. Before the first step runs, the plan must still match
  the Domain (same digest and Action Type) and every Connector step's payload is checked through the
  Central Processor: a Domain changed after approval, or a plan without a step, runs nothing.
- The action is saved through the store's revision check (#18); a concurrent change to another
  action of the Session is reloaded, never overwritten.
- Each step's state is saved before and after it runs. A step saved as started, with no end, is
  uncertain (the app stopped mid-step): it is never run again automatically, the User reconciles it.
- A step's Connector is the Domain's, or one of the app's `destinations` for that Action Type that
  the approved plan names (the User chose it, e.g. Jira for a work item, #73).
- One run per action at a time: a second Execute while one runs is refused (double click).
- Retry is manual, keeps the approved payload and skips the steps already done (OPEN-QUESTIONS #7).
"""
import copy
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from norev.core.models import (
    ActionStatus, Destinations, Domain, ProposedAction, StaleRevision, StepRecord, StepState, WorkflowStep, allowed_connectors,
)
from norev.core.ports import ExecutionStore, SessionStore
from norev.processor.central import CentralProcessor, ConnectorFailed, ResultNotRecorded
from norev.usecases.approve import domain_digest

MAX_SAVE_ATTEMPTS = 5  # revision conflicts with other writers of the Session before giving up


class ExecutionRefused(ValueError):
    """Nothing ran: the action, its Workflow or its payload does not allow it."""


class AlreadyRunning(ExecutionRefused):
    pass


@dataclass(frozen=True)
class ExecutionOutcome:
    action_id: str
    status: str  # "executed", "failed", or "uncertain": a step must be reconciled by the User first
    steps: dict[str, StepRecord]
    uncertain_step: str | None = None


# A local step: gets a copy of the action, returns its result (or None). Registered by the app, by name.
LocalStep = Callable[[ProposedAction], dict[str, Any] | None]


class WorkflowExecutor:
    def __init__(
        self,
        processor: CentralProcessor,
        sessions: SessionStore,
        steps: ExecutionStore,
        domain: Domain,
        local_steps: Mapping[str, LocalStep] | None = None,
        destinations: Destinations | None = None,
    ) -> None:
        self._processor = processor
        self._sessions = sessions
        self._steps = steps
        self._domain = domain
        self._local = dict(local_steps or {})
        self._destinations = dict(destinations or {})
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def execute(self, session_id: str, action_id: str) -> ExecutionOutcome:
        with self._one_run(action_id):
            action = self._action(session_id, action_id)
            if action.status is not ActionStatus.APPROVED:
                raise ExecutionRefused(f"action {action_id} is {action.status.value}, not approved")
            workflow = self._workflow(action)
            payloads = self._approved_payloads(action, workflow)  # before any step runs

            records = self._steps.load_steps(action_id)
            uncertain = [s.name for s in workflow if s.name in records and records[s.name].state is StepState.STARTED]
            if uncertain:
                return ExecutionOutcome(action_id, "uncertain", records, uncertain[0])

            for step in workflow:
                if step.name in records and records[step.name].state is StepState.DONE:
                    continue  # done in an earlier run: a Retry does not repeat it
                self._save_step(action_id, records, StepRecord(step.name, StepState.STARTED))
                error_type = None
                try:
                    if step.connector:
                        connector, payload = payloads[step.name]
                        result = self._processor.send(action, connector, payload, step=step.name)
                    else:
                        result = self._local[step.name](copy.deepcopy(action))
                except ResultNotRecorded:  # the send may have happened: left started, so uncertain
                    return ExecutionOutcome(action_id, "uncertain", records, step.name)
                except ConnectorFailed as exc:
                    error_type = type(exc.cause).__name__
                except Exception as exc:  # any step error fails the step; its message may quote Session data
                    error_type = type(exc).__name__
                if error_type is None:
                    self._save_step(action_id, records, StepRecord(step.name, StepState.DONE, result=result))
                    continue
                self._save_step(action_id, records, StepRecord(step.name, StepState.FAILED, error_type=error_type))
                label = step.label or step.name
                if step.connector and payloads[step.name][0] != step.connector:
                    label = f"{step.name} ({payloads[step.name][0]})"  # the Domain's label names its own Connector
                action.mark_failed(f"Step {label} failed ({error_type}).")
                self._save_action(session_id, action, ActionStatus.APPROVED)
                return ExecutionOutcome(action_id, "failed", records)

            action.mark_executed()
            self._save_action(session_id, action, ActionStatus.APPROVED)
            return ExecutionOutcome(action_id, "executed", records)

    def retry(self, session_id: str, action_id: str) -> None:
        """Manual Retry of a Failed action: back to Approved with the same approved payload."""
        with self._one_run(action_id):
            action = self._action(session_id, action_id)
            action.retry()
            self._save_action(session_id, action, ActionStatus.FAILED)

    def reconcile(self, session_id: str, action_id: str, step: str, happened: bool) -> None:
        """The User's answer for an uncertain step: it did happen (done, not run again) or it did not
        (it runs on the next Execute)."""
        with self._one_run(action_id):
            self._action(session_id, action_id)
            records = self._steps.load_steps(action_id)
            if step not in records or records[step].state is not StepState.STARTED:
                raise ExecutionRefused(f"step {step} of action {action_id} is not uncertain")
            if happened:
                record = StepRecord(step, StepState.DONE, result={"reconciled": True})
            else:
                record = StepRecord(step, StepState.FAILED, error_type="NotSent")
            self._steps.save_step(action_id, record)

    # ── helpers ─────────────────────────────────────────────────────────────

    @contextmanager
    def _one_run(self, action_id: str) -> Iterator[None]:
        # In process: after a restart nothing is running, and a step left started is uncertain instead
        with self._lock:
            if action_id in self._running:
                raise AlreadyRunning(f"action {action_id} is already running")
            self._running.add(action_id)
        try:
            yield
        finally:
            with self._lock:
                self._running.discard(action_id)

    def _action(self, session_id: str, action_id: str) -> ProposedAction:
        # an unknown Session raises LookupError; another Domain's plan fails the plan check before any step
        action = next((a for a in self._sessions.load_actions_snapshot(session_id).actions if a.id == action_id), None)
        if action is None:
            raise LookupError(f"no Proposed Action {action_id!r} in Session {session_id!r}")
        return action

    def _workflow(self, action: ProposedAction) -> tuple[WorkflowStep, ...]:
        action_type = self._domain.action_type(action.action_type) if action.action_type else None
        if action_type is None:
            raise ExecutionRefused(f"action {action.id} has no Action Type of Domain {self._domain.name}")
        for step in action_type.workflow.steps:
            if step.connector:
                allowed = allowed_connectors(self._destinations, self._domain.name, action.action_type, step)
                if not any(self._processor.has_connector(c) for c in allowed):
                    raise ExecutionRefused(f"step {step.name}: unknown Connector {step.connector!r}")
            elif step.name not in self._local:
                raise ExecutionRefused(f"step {step.name}: no known local implementation")
        return action_type.workflow.steps

    def _approved_payloads(self, action: ProposedAction, workflow: tuple[WorkflowStep, ...]) -> dict[str, tuple[str, dict]]:
        """Each Connector step's Connector and payload from the approval plan, checked as send() will check it."""
        plan = action.approved_payload
        if (not isinstance(plan, dict) or plan.get("version") != 1 or plan.get("domain") != self._domain.name
                or plan.get("domain_digest") != domain_digest(self._domain) or plan.get("action_type") != action.action_type):
            raise ExecutionRefused(f"the approved plan of action {action.id} does not match the Domain; approve it again")
        steps = plan.get("steps") if isinstance(plan.get("steps"), list) else []
        by_name = {s.get("step"): s for s in steps if isinstance(s, dict)}
        payloads = {}
        for step in workflow:
            if step.connector:
                planned = by_name.get(step.name, {})
                connector = planned.get("connector")
                if connector not in allowed_connectors(self._destinations, self._domain.name, action.action_type, step):
                    connector = step.connector  # check_send refuses the plan: its step names another Connector
                payload = planned.get("payload") if isinstance(planned.get("payload"), dict) else {}
                payloads[step.name] = (connector, self._processor.check_send(action, connector, payload, step=step.name))
        return payloads

    def _save_step(self, action_id: str, records: dict[str, StepRecord], record: StepRecord) -> None:
        self._steps.save_step(action_id, record)
        records[record.step] = record

    def _save_action(self, session_id: str, action: ProposedAction, was: ActionStatus) -> None:
        """Save this action's new status with the store's revision check. Other actions of the Session
        may change meanwhile (the User reviews them): reload and apply again. If this action itself
        changed status, someone else decided it: StaleRevision, nothing saved."""
        for _ in range(MAX_SAVE_ATTEMPTS):
            snapshot = self._sessions.load_actions_snapshot(session_id)
            current = next((a for a in snapshot.actions if a.id == action.id), None)
            if current is None or current.status is not was:
                raise StaleRevision(f"Proposed Action {action.id} changed while it ran")
            actions = [action if a.id == action.id else a for a in snapshot.actions]
            try:
                self._sessions.save_actions(session_id, actions, expected_revision=snapshot.revision)
                return
            except StaleRevision:
                continue
        raise StaleRevision(f"Session {session_id!r} kept changing; Proposed Action {action.id} not saved")
