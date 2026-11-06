"""Prepare, edit and decide Proposed Actions without executing a Connector.

The UI approves a persisted preview revision, never a payload supplied by the UI.
PayloadBuilder implementations own Connector-specific validation and rendering;
this module validates the Domain, plan envelope and optimistic store revision.
"""
import copy
import dataclasses
import hashlib
import json
import math
import re
from datetime import date, datetime, timezone
from typing import Any

from norev.core.models import (
    ActionStatus, ActionsSnapshot, Domain, InvalidTransition, ProposedAction,
    StaleRevision, UnsupportedWorkflow, ValidationError,
)
from norev.core.ports import Clock, PayloadBuilder, SessionStore


def _json_value(value: Any) -> bool:
    """JSON values only: no implicit tuple/key conversions or non-finite numbers."""
    if value is None or type(value) in (str, bool, int):
        return True
    if type(value) is float:
        return math.isfinite(value)
    if type(value) is list:
        return all(_json_value(v) for v in value)
    if type(value) is dict:
        return all(type(k) is str and _json_value(v) for k, v in value.items())
    return False


def domain_digest(domain: Domain) -> str:
    """Versioned canonical fingerprint; not a signature or an authorization token."""
    encoded = json.dumps({'version': 1, 'domain': dataclasses.asdict(domain)},
                         sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _date(value: Any, field: str) -> date | None:
    if value is None or value == '':
        return None
    if type(value) is date:
        return value
    valid = isinstance(value, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', value)
    result = None
    if valid:
        try:
            result = date.fromisoformat(value)
        except ValueError:
            pass
    if result is None:
        raise ValidationError('invalid_date', field)
    return result


def _validate_action(action: ProposedAction, domain: Domain, *, complete: bool) -> None:
    action_type = domain.action_type(action.action_type)
    if action_type is None:
        raise ValidationError('unknown_action_type', 'action_type')
    for name in ('title', 'description', 'transcript_excerpt'):
        if not isinstance(getattr(action, name), str):
            raise ValidationError('invalid_string', name)
    if complete and not action.title.strip():
        raise ValidationError('required', 'title')
    if action.owner is not None and not isinstance(action.owner, str):
        raise ValidationError('invalid_string', 'owner')
    if action.due_date is not None and type(action.due_date) is not date:
        raise ValidationError('invalid_date', 'due_date')
    fields = action.fields
    known = {field.name for field in action_type.fields}
    if not isinstance(fields, dict) or set(fields) - known:
        raise ValidationError('unknown_fields', 'fields')
    for field in action_type.fields:
        value = fields.get(field.name)
        if value is not None and not isinstance(value, str):
            raise ValidationError('invalid_string', field.name)
        if value is None or not value.strip():
            if complete and field.required:
                raise ValidationError('required', field.name)
            continue
        if field.type == 'date':
            _date(value, field.name)
        if field.type == 'time' and not re.fullmatch(r'([01][0-9]|2[0-3]):[0-5][0-9]', value):
            raise ValidationError('invalid_time', field.name)


def _validate_plan(plan: Any, action: ProposedAction, domain: Domain) -> None:
    try:
        valid_json = _json_value(plan)
    except RecursionError:
        valid_json = False
    keys = {'version', 'domain', 'domain_digest', 'action_type', 'steps'}
    if not valid_json or not isinstance(plan, dict) or set(plan) != keys:
        raise ValidationError('invalid_plan', 'prepared_payload')
    if (type(plan['version']) is not int or plan['version'] != 1
            or plan['domain'] != domain.name or plan['domain_digest'] != domain_digest(domain)
            or plan['action_type'] != action.action_type):
        raise ValidationError('preview_changed', 'prepared_payload')
    action_type = domain.action_type(action.action_type)
    expected = [(step.name, step.connector) for step in action_type.workflow.steps if step.connector]
    if not expected or len({name for name, _ in expected}) != len(expected):
        raise UnsupportedWorkflow('Workflow needs distinct Connector steps')
    steps = plan['steps']
    if not isinstance(steps, list) or len(steps) != len(expected):
        raise ValidationError('invalid_steps', 'prepared_payload')
    for step, (name, connector) in zip(steps, expected):
        if (not isinstance(step, dict) or set(step) != {'step', 'connector', 'payload'}
                or step['step'] != name or step['connector'] != connector
                or not isinstance(step['payload'], dict)):
            raise ValidationError('invalid_step', 'prepared_payload')


def _load(session_id: str, action_id: str, revision: int, store: SessionStore,
          domain: Domain | None = None) -> tuple[ActionsSnapshot, ProposedAction]:
    snapshot = store.load_actions_snapshot(session_id)
    if type(revision) is not int or revision < 0:
        raise ValidationError('invalid_revision', 'revision')
    if snapshot.revision != revision:
        raise StaleRevision('Session actions changed; reload before deciding')
    if domain is not None and store.get_session(session_id).domain != domain.name:
        raise ValidationError('domain_mismatch', 'domain')
    action = next((a for a in snapshot.actions if a.id == action_id), None)
    if action is None:
        raise LookupError('Proposed Action does not exist')
    if action.status is not ActionStatus.PROPOSED:
        raise InvalidTransition('Only Proposed actions can be edited or decided')
    return snapshot, action


def _save(session_id: str, snapshot: ActionsSnapshot, store: SessionStore) -> ActionsSnapshot:
    revision = store.save_actions(session_id, list(snapshot.actions), expected_revision=snapshot.revision)
    # Return exactly this committed snapshot, not a later concurrent writer's result.
    return ActionsSnapshot(revision, copy.deepcopy(snapshot.actions))


def edit_action(session_id: str, action_id: str, changes: dict[str, Any], *,
                expected_revision: int, store: SessionStore, domain: Domain) -> ActionsSnapshot:
    snapshot, action = _load(session_id, action_id, expected_revision, store, domain)
    if not isinstance(changes, dict) or set(changes) - {'title', 'description', 'owner', 'due_date', 'fields'}:
        raise ValidationError('unknown_changes', 'changes')
    for name, value in changes.items():
        if name == 'due_date':
            value = _date(value, name)
        if name == 'fields':
            if not isinstance(value, dict):
                raise ValidationError('invalid_fields', 'fields')
            action.fields.update(copy.deepcopy(value))
        else:
            setattr(action, name, value)
    _validate_action(action, domain, complete=False)
    action.prepared_payload = None
    return _save(session_id, snapshot, store)


def preview_action(session_id: str, action_id: str, options: dict[str, Any], *,
                   expected_revision: int, store: SessionStore, domain: Domain,
                   builder: PayloadBuilder) -> ActionsSnapshot:
    snapshot, action = _load(session_id, action_id, expected_revision, store, domain)
    _validate_action(action, domain, complete=True)
    if not isinstance(options, dict):
        raise ValidationError('invalid_options', 'options')
    plan = builder.build(copy.deepcopy(action), copy.deepcopy(domain), copy.deepcopy(options))
    _validate_plan(plan, action, domain)
    action.prepared_payload = copy.deepcopy(plan)
    return _save(session_id, snapshot, store)


def approve_action(session_id: str, action_id: str, *, expected_revision: int,
                   store: SessionStore, domain: Domain, clock: Clock,
                   approved_by: str) -> ActionsSnapshot:
    snapshot, action = _load(session_id, action_id, expected_revision, store, domain)
    _validate_action(action, domain, complete=True)
    if action.prepared_payload is None:
        raise ValidationError('preview_required', 'prepared_payload')
    _validate_plan(action.prepared_payload, action, domain)
    if not isinstance(approved_by, str) or not approved_by.strip():
        raise ValidationError('actor_required', 'approved_by')
    at = clock.now()
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValidationError('aware_time_required', 'approved_at')
    action.approve(action.prepared_payload)
    action.approved_by = approved_by
    action.approved_at = at.astimezone(timezone.utc)
    action.prepared_payload = None
    return _save(session_id, snapshot, store)


def reject_action(session_id: str, action_id: str, *, expected_revision: int,
                  store: SessionStore) -> ActionsSnapshot:
    snapshot, action = _load(session_id, action_id, expected_revision, store)
    action.reject()
    action.prepared_payload = None
    return _save(session_id, snapshot, store)
