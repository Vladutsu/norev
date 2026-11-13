import json
import unittest
from contextlib import nullcontext
from datetime import datetime

from norev.adapters.domains.loader import DOMAINS, load_domain, optional_completion, optional_decisions
from norev.core.models import Segment, Session, Transcript
from norev.testing.fakes import InMemorySessionStore
from norev.usecases.council_evidence import render_vote, validate_attendance, validate_votes
from norev.usecases.council_transcript import OutputLimit, collect_notes, extract_windows, read_windows, windows
from norev.usecases.session_flow import run_session


class ExperimentTest(unittest.TestCase):
    def test_overlap_carries_vote_question_over_boundary_within_budget(self):
        lines = ['Deschidere', 'Cine se abține?', 'Trei.', 'Următorul proiect']
        template = lambda body: 'Instrucțiuni\n' + body
        limit = len(template('\n'.join(lines[:2])))
        plain = list(windows(lines, template, len, limit))
        overlap = list(windows(lines, template, len, limit, overlap=16))

        self.assertFalse(any('Cine se abține?\nTrei.' in prompt for _, _, prompt in plain))
        self.assertTrue(any('Cine se abține?\nTrei.' in prompt for _, _, prompt in overlap))
        self.assertTrue(all(len(prompt) <= limit for _, _, prompt in overlap))
        self.assertEqual({i for start, end, _ in overlap for i in range(start, end)}, set(range(len(lines))))

    def test_notes_get_source_text_and_time_from_a_segment_in_the_window(self):
        transcript = Transcript((Segment(0, 1, 'Cine se abține?'), Segment(1, 2, 'Trei.')), 'ro')
        answer = {'notes': [
            {'id': 1, 'kind': 'numar_vot'},
            {'id': 1, 'kind': 'numar_vot'},
            {'id': 2, 'kind': 'numar_vot'},
        ]}
        notes = collect_notes(FakeModel(answer), transcript, ['0 Cine se abține?', '1 Trei.'], None, 0)
        self.assertEqual([(note['id'], note['start'], note['quote']) for note in notes], [(1, 1, 'Trei.')])

    def test_merge_keeps_repeat_vote_with_identical_tally(self):
        original = {'time': '00:01:00', 'title': 'Proiectul 1', 'result': 'neclar', 'announced': ['Trei abțineri.']}
        repeated = {**original, 'time': '00:02:00', 'title': 'Reluarea votului: Proiectul 1'}
        llm = FakeModel({'meeting': {}, 'items': [original, original, repeated]})
        extracted = json.loads(read_windows(llm, ['Voturile'], '{{transcript}}', {}, None, 0))
        self.assertEqual(extracted['items'], [original, repeated])

    def test_output_limit_retries_smaller_windows_and_covers_all_input(self):
        lines = ['a', 'b', 'c', 'd']
        llm = ShortResponseModel(None)
        result = list(extract_windows(llm, lines, lambda body: body, {}, None, overlap=1))
        self.assertEqual([(start, end) for start, end, _ in result], [(0, 2), (1, 2), (2, 4)])

    def test_repeated_notes_on_one_line_are_kept_not_retried_forever(self):
        transcript = Transcript((Segment(0, 1, 'Supun la vot. Aprobat.'),), 'ro')
        answer = {'notes': [{'id': 0, 'kind': 'rezultat_vot'}] * 24}
        notes = collect_notes(FakeModel(answer), transcript, ['0 Supun la vot. Aprobat.'], None, 0)
        self.assertEqual([(note['id'], note['kind']) for note in notes], [(0, 'rezultat_vot')])


class EvidenceTest(unittest.TestCase):
    def test_spoken_project_number_starts_a_new_vote_in_the_same_turn_as_its_title(self):
        segments = turns('Proiect de hotărâre privind regulamentul.', 'Cine este pentru?',
                         'Proiectul a fost respins.', 'Numărul 3, proiect de hotărâre privind bugetul.',
                         'Cine este pentru?', 'Trei voturi pentru. Proiectul a fost adoptat.')
        first, second = validate_votes(segments)
        self.assertEqual(first['interval_end'], second['start'])
        self.assertEqual(first['result'], 'respins')
        self.assertEqual(second['result'], 'adoptat')
        self.assertEqual(second['anchor_segment_id'], 3)
        self.assertEqual([e['segment_id'] for e in second['vote_evidence']], [5, 5])

    def test_threshold_and_old_count_do_not_enter_the_revote(self):
        segments = turns(
            'Proiect de hotărâre privind regulamentul.', '11 voturi pentru înseamnă pragul necesar.',
            'Cine este pentru?', '13 voturi pentru. Proiectul a fost adoptat.',
            'Bine, reluăm votul la proiectul numărul 1.', 'Cine este pentru?',
            'Proiectul numărul 1 a fost votat prin exprimarea unor 13 voturi pentru.',
            'Avem 3 abțineri.', 'Este cineva împotrivă?', '4.', 'Este cineva împotrivă?',
        )
        initial, repeat = validate_votes(segments)
        self.assertEqual([(e['category'], e['value']) for e in initial['vote_evidence']],
                         [('pentru', 13), ('rezultat', 'adoptat')])
        self.assertEqual([(e['category'], e['value'], e['segment_id']) for e in repeat['vote_evidence']],
                         [('abțineri', 3, 7)])
        self.assertEqual(initial['interval_end'], repeat['start'])
        self.assertTrue(repeat['result'].startswith('neclar'))

    def test_bare_count_needs_a_question_in_the_same_vote(self):
        segments = turns(
            'Proiect de hotărâre privind regulamentul.', 'Cine este pentru?', '5.',
            'Vă rog să considerați și votul meu.', 'Deci șase.',
            'Împotrivă?', 'Una.', 'Proiectul a fost respins.',
            'Proiect de hotărâre privind bugetul.', 'Cine este pentru?', 'Proiectul a fost adoptat.',
        )
        first, second = validate_votes(segments)
        against, result = first['vote_evidence']
        self.assertEqual((against['category'], against['value'], against['question_segment_id']), ('împotrivă', 1, 5))
        self.assertEqual(result['value'], 'respins')
        self.assertEqual([e['category'] for e in second['vote_evidence']], ['rezultat'])

    def test_conflicting_tally_is_flagged_and_left_out_of_rendered_counts(self):
        vote, = validate_votes(turns('Proiect de hotărâre privind regulamentul.', 'Cine este pentru?',
                                     'Două voturi împotrivă, trei voturi împotrivă. Proiectul a fost respins.'))
        self.assertEqual(vote['conflicts'], ['împotrivă'])
        output = render_vote(vote)
        self.assertIn('Cifre contradictorii', output)
        self.assertNotIn('Două voturi', output)
        self.assertNotIn('trei voturi', output)

    def test_attendance_uses_its_own_evidence_and_preserves_unresolved_update(self):
        opening = ('Sunt prezenți 20 de consilieri.', 'Nu trebuia doamna Barac online?',
                   'Deci vom fi 20 de consilieri.', 'Șase voturi pentru.')
        meeting = validate_attendance({'attendance': '6', 'attendance_inferred': 5},
                                      turns(*opening, "Pe domnul Abaraf n-am văzut-o deloc."))
        self.assertEqual(meeting['attendance'], '20')
        self.assertIn('attendance_update', meeting)
        self.assertNotIn('attendance_inferred', meeting)
        self.assertEqual([e['segment_id'] for e in meeting['attendance_evidence']], [0, 2, 4])
        self.assertEqual(validate_attendance({}, turns(*opening, 'Au rămas prezenți 19 consilieri.'))['attendance_final'], 19)


class SessionIntegrationTest(unittest.TestCase):
    def test_council_analysis_saves_validated_minutes_and_followups(self):
        folder = DOMAINS / 'local_council'
        domain = load_domain(folder)
        store = InMemorySessionStore()
        transcript = Transcript(turns('Sunt prezenți 20 de consilieri.', 'Supun aprobarea ordinii de zi.',
                                      'Cine este pentru?', 'Ordinea de zi a fost aprobată în unanimitate.',
                                      'Se trimite raportul mâine.'), 'ro')
        ref = store.save_transcript('s1', transcript)
        store.save_session(Session('s1', datetime(2026, 4, 27), domain.name, transcript_ref=ref,
                                   source_transcript_ref=ref))
        llm = CouncilModel()
        run_session('s1', store=store, translator=None, extractor=None, domain=domain, llm=llm,
                    completion=optional_completion(folder), decisions=optional_decisions(folder))
        document = store.load_completion('s1')
        self.assertIn('„20”', document)
        self.assertIn('adoptat în unanimitate', document)
        self.assertIn('Se va transmite raportul.', document)
        self.assertTrue(any(a.title == 'Transmiterea raportului' for a in store.load_actions('s1')))


class CouncilModel:
    limit = 8000
    tokens = staticmethod(len)

    def bounded_llm(self, model):
        return nullcontext(self)

    def generate_json(self, prompt, *, model=None, json_schema=None):
        if 'notes' in json_schema['properties']:
            return '{"notes":[{"kind":"prezenta","id":0},{"kind":"dezbatere","id":4}]}'
        if 'meeting' in json_schema['properties']:
            return '{"meeting":{"attendance":"6"},"items":[]}'
        if 'output' in json_schema['properties']:
            return '{"output":"Se va transmite raportul."}'
        return json.dumps({'added_actions': [{'action_type': 'council_follow_up', 'title': 'Transmiterea raportului',
                                               'transcript_excerpt': 'Se trimite raportul mâine.'}]})


def turns(*lines):
    return tuple(Segment(index * 2, index * 2 + 1, text) for index, text in enumerate(lines))


class FakeModel:
    limit = 1500
    tokens = staticmethod(len)

    def __init__(self, answer):
        self.answer = answer

    def generate_json(self, prompt, *, model=None, json_schema=None):
        return json.dumps(self.answer)


class ShortResponseModel(FakeModel):
    def generate_json(self, prompt, *, model=None, json_schema=None):
        if len(prompt.splitlines()) > 2:
            raise OutputLimit
        return super().generate_json(prompt, model=model, json_schema=json_schema)
