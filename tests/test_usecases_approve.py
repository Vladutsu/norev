"""Approval tests use no model, Connector, filesystem output or network."""
import copy
import dataclasses
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone

from norev.core.models import (
    ActionStatus, ActionType, Domain, DomainField, InvalidTransition,
    ProposedAction, Role, Session, StaleRevision, Suggestion, Transcript, ValidationError,
    UnsupportedWorkflow, Workflow, WorkflowStep,
)
from norev.testing.fakes import FakeLlmClient, FakePayloadBuilder, FixedClock, InMemorySessionStore
from norev.usecases.domain_stage import run_domain_stage
from norev.usecases.approve import (
    approve_action, domain_digest, edit_action, preview_action, reject_action,
)
from norev.usecases.extract import extract_session_actions


NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
DOMAIN = Domain(
    'corporate', 'Corporate', 'local.gguf', 4096, (Role('participant'),),
    (ActionType('work_item', (
        DomainField('deliverable', 'string', False),
        DomainField('meeting_date', 'date', False),
        DomainField('meeting_time', 'time', False),
    ), Workflow((WorkflowStep('prepare'), WorkflowStep(
        'export_document', 'local_document', ('title', 'description', 'owner', 'due_date')
    ), WorkflowStep('save_result')))),),
    '{{transcript}} {{proposed_actions}}', {},
)


def action():
    return ProposedAction('a1', 'Send offer', 'Send the offer', 'I will send it',
                          action_type='work_item')


def plan():
    return {
        'version': 1, 'domain': 'corporate', 'domain_digest': domain_digest(DOMAIN),
        'action_type': 'work_item', 'steps': [{
            'step': 'export_document', 'connector': 'local_document',
            'payload': {'version': 1, 'path': 'C:\\Exports\\offer.txt',
                        'content': 'Send offer\n', 'encoding': 'utf-8', 'overwrite': False},
        }],
    }


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemorySessionStore()
        self.store.save_session(Session('s1', NOW, 'corporate'))
        self.store.save_actions('s1', [action()], expected_revision=0)
        self.builder = FakePayloadBuilder(plan())

    def snapshot(self):
        return self.store.load_actions_snapshot('s1')

    def preview(self, **kwargs):
        return preview_action('s1', 'a1', {}, expected_revision=self.snapshot().revision,
                              store=self.store, domain=kwargs.get('domain', DOMAIN),
                              builder=kwargs.get('builder', self.builder))

    def approve(self, revision=None, **kwargs):
        return approve_action('s1', 'a1', expected_revision=(
            self.snapshot().revision if revision is None else revision), store=self.store,
            domain=kwargs.get('domain', DOMAIN), clock=FixedClock(kwargs.get('now', NOW)),
            approved_by=kwargs.get('actor', 'local-user-1'))

    def edit(self, changes, **kwargs):
        return edit_action('s1', 'a1', changes, expected_revision=self.snapshot().revision,
                           store=self.store, domain=kwargs.get('domain', DOMAIN))

    def test_preview_then_approval_uses_exact_saved_payload_and_actor(self):
        shown = self.preview()
        saved_plan = copy.deepcopy(shown.actions[0].prepared_payload)
        shown.actions[0].prepared_payload['steps'][0]['payload']['content'] = 'tampered'
        result = self.approve(shown.revision)
        approved = result.actions[0]
        self.assertEqual(approved.approved_payload, saved_plan)
        self.assertEqual(approved.status, ActionStatus.APPROVED)
        self.assertEqual((approved.approved_by, approved.approved_at), ('local-user-1', NOW))
        self.assertIsNone(approved.prepared_payload)
        self.assertEqual(len(self.builder.calls), 1)  # approval never rebuilds
        self.assertIsNone(approved.owner)
        self.assertIsNone(approved.due_date)
        approved.approved_payload['steps'].clear()
        self.assertEqual(self.snapshot().actions[0].approved_payload, saved_plan)

    def test_approval_requires_preview_and_valid_actor_clock(self):
        with self.assertRaises(ValidationError):
            self.approve()
        self.preview()
        before = self.snapshot()
        for kwargs in ({'actor': ''}, {'actor': 3}, {'now': NOW.replace(tzinfo=None)}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValidationError):
                self.approve(**kwargs)
        self.assertEqual(self.snapshot(), before)

    def test_edit_invalidates_preview_and_preserves_source(self):
        shown = self.preview()
        edited = self.edit({'title': 'New title', 'due_date': '2026-11-09',
                            'fields': {'deliverable': 'PDF'}})
        self.assertIsNone(edited.actions[0].prepared_payload)
        self.assertEqual(edited.actions[0].due_date, date(2026, 11, 9))
        self.assertEqual(edited.actions[0].transcript_excerpt, 'I will send it')
        with self.assertRaises(StaleRevision):
            self.approve(shown.revision)
        with self.assertRaises(ValidationError):
            self.approve()

    def test_second_preview_invalidates_first_even_if_identical(self):
        old = self.preview()
        self.preview()
        with self.assertRaises(StaleRevision):
            self.approve(old.revision)

    def test_partial_edit_allowed_but_required_title_blocks_preview(self):
        self.edit({'title': '  '})
        with self.assertRaises(ValidationError):
            self.preview()
        self.assertEqual(self.builder.calls, [])

    def test_required_domain_field_blocks_preview(self):
        required = dataclasses.replace(DOMAIN.action_types[0], fields=(
            DomainField('deliverable', 'string', True),))
        domain = dataclasses.replace(DOMAIN, action_types=(required,))
        for value in (None, '', '  '):
            self.edit({'fields': {'deliverable': value}}, domain=domain)
            with self.assertRaises(ValidationError):
                self.preview(domain=domain)

    def test_invalid_edits_are_rejected_without_writes(self):
        invalid = [
            {'id': 'other'}, {'status': 'approved'}, {'approved_payload': {}},
            {'prepared_payload': {}}, {'action_type': 'other'}, {'transcript_excerpt': 'fake'},
            {'title': True}, {'description': {}}, {'owner': 12},
            {'due_date': '2026-02-30'}, {'due_date': '20261109'}, {'due_date': True},
            {'fields': {'unknown': 'x'}}, {'fields': {'deliverable': []}},
            {'fields': {'meeting_date': '2026-02-30'}},
            {'fields': {'meeting_time': '24:00'}}, {'fields': {'meeting_time': '9:00'}},
            {'fields': {'meeting_time': 900}}, {'fields': []},
        ]
        before = self.snapshot()
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.edit(changes)
        self.assertEqual(self.snapshot(), before)

    def test_domain_mismatch_unknown_action_type_and_schema_change(self):
        with self.assertRaises(ValidationError):
            self.preview(domain=dataclasses.replace(DOMAIN, name='medical'))
        with self.assertRaises(ValidationError):
            self.preview(domain=dataclasses.replace(DOMAIN, action_types=()))
        self.preview()
        with self.assertRaises(ValidationError):
            self.approve(domain=dataclasses.replace(DOMAIN, prompt_template='Changed'))

    def test_malformed_builder_plan_cannot_be_approved(self):
        bad_plans = []
        for key, value in [('version', True), ('domain', 'other'), ('domain_digest', 'bad'),
                           ('action_type', 'other'), ('steps', []), ('extra', 'no')]:
            candidate = plan()
            candidate[key] = value
            bad_plans.append(candidate)
        for key, value in [('connector', 'email'), ('step', 'unknown'), ('payload', []),
                           ('extra', 'no')]:
            candidate = plan()
            candidate['steps'][0][key] = value
            bad_plans.append(candidate)
        for value in (float('nan'), (1, 2), {3: 'x'}):
            candidate = plan()
            candidate['steps'][0]['payload']['invalid'] = value
            bad_plans.append(candidate)
        for candidate in bad_plans:
            with self.subTest(candidate=candidate), self.assertRaises(ValidationError):
                self.preview(builder=FakePayloadBuilder(candidate))
        self.assertIsNone(self.snapshot().actions[0].prepared_payload)

    def test_reject_needs_no_preview_or_complete_fields_and_is_final(self):
        self.edit({'title': ''})
        result = reject_action('s1', 'a1', expected_revision=self.snapshot().revision,
                               store=self.store)
        self.assertEqual(result.actions[0].status, ActionStatus.REJECTED)
        for call in (lambda: self.edit({'title': 'new'}), self.preview, self.approve):
            with self.assertRaises(InvalidTransition):
                call()
        self.assertEqual(self.builder.calls, [])

    def test_unavailable_connector_builder_error_preserves_previous_preview(self):
        self.preview()
        before = self.snapshot()

        class UnavailableBuilder:
            def build(self, action, domain, options):
                raise UnsupportedWorkflow('Connector unavailable')

        with self.assertRaises(UnsupportedWorkflow):
            self.preview(builder=UnavailableBuilder())
        self.assertEqual(self.snapshot(), before)

    def test_each_workflow_step_payload_and_order_are_preserved(self):
        action_type = dataclasses.replace(DOMAIN.action_types[0], workflow=Workflow((
            WorkflowStep('first', 'local_document'), WorkflowStep('second', 'local_document'),
        )))
        domain = dataclasses.replace(DOMAIN, action_types=(action_type,))
        payload = plan()
        payload['domain_digest'] = domain_digest(domain)
        payload['steps'] = [
            {'step': name, 'connector': 'local_document', 'payload': {'content': name}}
            for name in ('first', 'second')
        ]
        reversed_payload = copy.deepcopy(payload)
        reversed_payload['steps'].reverse()
        with self.assertRaises(ValidationError):
            self.preview(domain=domain, builder=FakePayloadBuilder(reversed_payload))
        self.preview(domain=domain, builder=FakePayloadBuilder(payload))
        self.assertEqual(self.approve(domain=domain).actions[0].approved_payload, payload)

    def test_approved_action_cannot_be_edited_or_approved_twice(self):
        self.preview()
        self.approve()
        for call in (lambda: self.edit({'title': 'new'}), self.preview, self.approve):
            with self.assertRaises(InvalidTransition):
                call()

    def test_payload_and_errors_do_not_expose_content(self):
        secret = 'CONTENT_SECRET'
        self.edit({'description': secret})
        self.preview()
        self.assertNotIn(secret, repr(self.snapshot()))
        with self.assertRaises(ValidationError) as caught:
            self.edit({'fields': {'deliverable': {secret: secret}}})
        self.assertNotIn(secret, str(caught.exception))

    def test_two_concurrent_decisions_only_one_wins(self):
        for second in ('approve', 'reject'):
            with self.subTest(second=second):
                self.setUp()
                shown = self.preview()
                barrier = threading.Barrier(2)
                original = self.store.save_actions

                def synchronized(*args, **kwargs):
                    barrier.wait(timeout=5)
                    return original(*args, **kwargs)

                self.store.save_actions = synchronized

                def decide(kind):
                    try:
                        if kind == 'approve':
                            self.approve(shown.revision)
                        else:
                            reject_action('s1', 'a1', expected_revision=shown.revision, store=self.store)
                        return 'saved'
                    except StaleRevision:
                        return 'stale'

                with ThreadPoolExecutor(2) as pool:
                    results = list(pool.map(decide, ['approve', second]))
                self.assertCountEqual(results, ['saved', 'stale'])

    def test_extraction_commit_cannot_overwrite_concurrent_approval(self):
        # Pause exactly at the old check-then-save race window, after model output exists.
        session = self.store.get_session('s1')
        # Session input updates are forbidden after actions exist; initialize a fresh store.
        self.store = InMemorySessionStore()
        session.transcript_ref = self.store.save_transcript('s1', Transcript(()))
        self.store.save_session(session)
        self.store.save_actions('s1', [action()], expected_revision=0)
        shown = self.preview()
        entered, release = threading.Event(), threading.Event()
        original = self.store.save_actions

        def delayed(*args, **kwargs):
            if kwargs.get('suggestions') == []:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('approval did not finish')
            return original(*args, **kwargs)

        self.store.save_actions = delayed
        from norev.testing.fakes import FakeActionExtractor
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(extract_session_actions, 's1', self.store,
                                 FakeActionExtractor({'proposed_actions': [{'title': 'replacement'}]}))
            try:
                self.assertTrue(entered.wait(5))
                self.approve(shown.revision)
            finally:
                release.set()
            with self.assertRaises(StaleRevision):
                future.result(timeout=5)
        self.assertEqual(self.snapshot().actions[0].status, ActionStatus.APPROVED)

    def test_domain_commit_conflict_preserves_actions_and_suggestions_together(self):
        generic = action()
        generic.action_type = None
        revision = self.store.save_actions('s1', [generic], expected_revision=self.snapshot().revision,
                                           suggestions=[Suggestion('existing')])
        entered, release = threading.Event(), threading.Event()
        original = self.store.save_actions

        def delayed(*args, **kwargs):
            if kwargs.get('suggestions') is not None:
                entered.set()
                if not release.wait(5):
                    raise AssertionError('rejection did not finish')
            return original(*args, **kwargs)

        self.store.save_actions = delayed
        answer = {'proposed_actions': [{'id': 'a1', 'action_type': 'work_item', 'fields': {}}],
                  'suggestions': [{'text': 'replacement'}]}
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(run_domain_stage, 's1', self.store, DOMAIN,
                                 FakeLlmClient([json.dumps(answer)]), Transcript(()))
            try:
                self.assertTrue(entered.wait(5))
                reject_action('s1', 'a1', expected_revision=revision, store=self.store)
            finally:
                release.set()
            with self.assertRaises(StaleRevision):
                future.result(timeout=5)
        self.assertEqual(self.snapshot().actions[0].status, ActionStatus.REJECTED)
        self.assertEqual(self.store.load_suggestions('s1'), [Suggestion('existing')])


class StoreRevisionTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemorySessionStore()
        self.store.save_session(Session('s1', NOW, 'corporate'))

    def test_cas_is_atomic_with_suggestions_and_returns_copies(self):
        snap = self.store.load_actions_snapshot('s1')
        self.assertEqual((snap.revision, snap.actions), (0, ()))
        revision = self.store.save_actions('s1', [action()], expected_revision=0,
                                           suggestions=[Suggestion('old')])
        with self.assertRaises(StaleRevision):
            self.store.save_actions('s1', [], expected_revision=0, suggestions=[Suggestion('new')])
        self.assertEqual(self.store.load_suggestions('s1'), [Suggestion('old')])
        self.assertEqual(self.store.load_actions_snapshot('s1').revision, revision)
        snap = self.store.load_actions_snapshot('s1')
        snap.actions[0].title = 'changed outside store'
        self.assertEqual(self.store.load_actions('s1')[0].title, 'Send offer')

    def test_suggestions_increment_same_revision_and_conflict_is_noop(self):
        self.store.save_suggestions('s1', [Suggestion('old')], expected_revision=0)
        with self.assertRaises(StaleRevision):
            self.store.save_actions('s1', [action()], expected_revision=0)
        with self.assertRaises(StaleRevision):
            self.store.save_suggestions('s1', [], expected_revision=0)
        self.assertEqual(self.store.load_suggestions('s1'), [Suggestion('old')])

    def test_duplicate_ids_missing_session_and_invalid_revision(self):
        with self.assertRaises(ValueError):
            self.store.save_actions('s1', [action(), action()], expected_revision=0)
        with self.assertRaises(LookupError):
            self.store.load_actions_snapshot('missing')
        with self.assertRaises(LookupError):
            self.store.save_actions('missing', [], expected_revision=0)
        for revision in (True, -1, '0'):
            with self.assertRaises(ValueError):
                self.store.save_actions('s1', [], expected_revision=revision)

    def test_session_input_cannot_change_behind_preview(self):
        self.store.save_actions('s1', [action()], expected_revision=0)
        session = self.store.get_session('s1')
        session.domain = 'medical'
        with self.assertRaises(ValueError):
            self.store.save_session(session)
        self.assertEqual(self.store.get_session('s1').domain, 'corporate')

    def test_session_inputs_can_change_before_review_and_invalidate_inflight_work(self):
        session = self.store.get_session('s1')
        session.audio_ref = 'audio-1'
        self.store.save_session(session)
        revision = self.store.load_actions_snapshot('s1').revision
        session.transcript_ref = 'transcript-1'
        self.store.save_session(session)
        self.assertGreater(self.store.load_actions_snapshot('s1').revision, revision)
        with self.assertRaises(StaleRevision):
            self.store.save_actions('s1', [], expected_revision=revision)


if __name__ == '__main__':
    unittest.main()
