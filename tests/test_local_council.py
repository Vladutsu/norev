import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from norev.adapters.connectors.local_document import ExportConflict, LocalDocumentConnector
from norev.adapters.connectors.word_document import render_docx
from norev.adapters.domains.loader import load_domain, optional_completion, optional_decisions
from norev.core.models import Segment, Transcript
from norev.processor.payloads import LocalDocumentPayloadBuilder
from norev.testing.fakes import FakeLlmClient
from norev.usecases.document_completion import LIMIT, _copied, chunks, complete_document, vote_excerpt
from norev.usecases.session_flow import select_domain
from test_ui_session_backend import Case

FOLDER = Path(__file__).resolve().parents[1] / 'domains/local_council'


class DocumentTest(unittest.TestCase):
    def test_every_character_survives_chunking_and_requests_are_bounded(self):
        source = Transcript((Segment(0, 50, 'Ședință. ' * 1500), Segment(50, 60, 'Închidem.')), 'ro')
        parts = list(chunks(source))
        self.assertGreater(len(parts), 1)
        self.assertEqual(''.join(s.text for p in parts for s in p.segments), ''.join(s.text for s in source.segments))
        self.assertTrue(all(sum(len(s.text) for s in p.segments) <= LIMIT for p in parts))

    def test_completion_keeps_all_parts_and_rejects_invalid_partial_result(self):
        transcript = Transcript((Segment(0, 120, 'a' * 6500),), 'ro')
        empty = json.dumps({'proposed_actions': [], 'added_actions': [], 'suggestions': []})
        llm = FakeLlmClient([empty, '{"output":"Prima parte"}', empty, '{"output":"Ultima parte"}'])
        domain = load_domain(FOLDER)
        actions, hints, result = complete_document(llm, domain, transcript, date.today(), optional_completion(FOLDER), [], 's1')
        self.assertIn('Prima parte', result)
        self.assertIn('Ultima parte', result)
        self.assertEqual((actions, hints), ([], []))
        self.assertEqual(select_domain('local_council'), 'local_council')
        with self.assertRaises(ValueError):
            complete_document(FakeLlmClient([empty, '{"output":""}']), domain, transcript,
                              date.today(), optional_completion(FOLDER), [], 's1')

    def test_a_broken_follow_up_answer_keeps_the_minutes_and_names_roles(self):
        from norev.core.models import Speaker
        transcript = Transcript((Segment(0, 5, 'Declar ședința deschisă.', 'S1'),), 'ro')
        llm = FakeLlmClient(['{"proposed_actions": [', '{"output":"Deschiderea ședinței"}'])
        domain = load_domain(FOLDER)
        actions, hints, result = complete_document(llm, domain, transcript, date.today(), optional_completion(FOLDER),
                                                   [Speaker('S1', 'chair', True), Speaker('S2', 'mayor')], 's1')
        self.assertIn('Deschiderea ședinței', result)
        self.assertEqual(actions, [])
        self.assertEqual(len(hints), 1)
        self.assertIn('[S1 · Președinte de ședință] Declar', llm.calls[-1][0])

    def test_unconfirmed_roles_stay_out_and_a_copied_transcript_is_retried(self):
        from norev.core.models import Speaker
        transcript = Transcript((Segment(0, 5, 'Supun la vot.', 'S2'),), 'ro')
        empty = json.dumps({'proposed_actions': [], 'added_actions': [], 'suggestions': []})
        copy = json.dumps({'output': '[00:00:00–00:00:05] [S2] Supun la vot.\n[00:00:05] a\n[00:00:06] b'})
        llm = FakeLlmClient([empty, copy, '{"output":"Proiectul a fost supus la vot (00:00:00)."}'])
        _, _, result = complete_document(llm, load_domain(FOLDER), transcript, date.today(),
                                         optional_completion(FOLDER), [Speaker('S2', 'mayor')], 's1')
        self.assertIn('[S2] Supun', llm.calls[1][0])
        self.assertIn('nu copia replicile', llm.calls[2][0])
        self.assertIn('Proiectul a fost supus la vot', result)
        twice = FakeLlmClient([empty, copy, copy])
        _, _, result = complete_document(twice, load_domain(FOLDER), transcript, date.today(),
                                         optional_completion(FOLDER), [], 's1')
        self.assertIn('[de verificat] Fragmentul nu a putut fi redactat', result)

    def test_minutes_follow_the_agenda_with_each_vote_under_its_item(self):
        debate = 'Discutăm despre proiect, argumente pro și contra. ' * 40
        transcript = Transcript((
            Segment(0, 5, 'Pentru astăzi a fost convocat Consiliul Local Sibiu în ședință extraordinară.', 'S1'),
            Segment(5, 8, 'Din 21 de consilieri sunt prezenți 20 de consilieri.', 'S1'),
            Segment(10, 300, debate, 'S2'),
            Segment(300, 305, 'Proiect de hotărâre numărul 2 privind parcările.', 'S1'), Segment(305, 306, 'Cine este pentru?', 'S1'),
            Segment(306, 307, '5.', 'S3'), Segment(307, 308, 'Proiectul a fost respins.', 'S3'),
            Segment(310, 600, debate, 'S2'),
            Segment(600, 602, 'Proiectul 3 privind spațiile verzi.', 'S1'), Segment(602, 603, 'Cine este pentru?', 'S1'),
            Segment(603, 604, 'Declar ședința închisă.', 'S1'),
        ), 'ro')
        excerpt = vote_excerpt(transcript, {})
        self.assertIn('[S3] 5.', excerpt)
        self.assertNotIn('argumente pro', excerpt)
        answer = {'meeting': {'locality': 'Sibiu', 'type': 'extraordinară', 'chair': 'Domnul X conduce ședința.',
                              'attendance': 'Din 21 de consilieri sunt prezenți 20 de consilieri.'},
                  'items': [{'time': '00:05:00', 'title': 'Proiectul 2, parcările', 'result': 'respins',
                             'announced': ['5.', 'Proiectul a fost respins.']},
                            {'time': '10:00', 'title': 'Proiectul 3', 'result': 'adoptat',
                             'announced': ['6 voturi pentru, proiectul a fost adoptat.']}]}
        empty = json.dumps({'proposed_actions': [], 'added_actions': [], 'suggestions': []})
        llm = FakeLlmClient([json.dumps(answer)] + [r for i in range(4) for r in (empty, json.dumps({'output': f'Text {i}.'}))])
        _, _, result = complete_document(llm, load_domain(FOLDER), transcript, date.today(), optional_completion(FOLDER),
                                         [], 's1', optional_decisions(FOLDER))
        self.assertIn('**Ședința:** extraordinară a Consiliului Local Sibiu', result)
        self.assertIn('**Prezență:** „Din 21 de consilieri sunt prezenți 20 de consilieri.”', result)
        self.assertIn('**Conducerea ședinței:** [de completat]', result)  # a chair nobody said is not written
        headings = [line for line in result.splitlines() if line.startswith('## ')]
        self.assertEqual(headings, ['## Deschiderea ședinței', '## 1. Proiectul 2, parcările', '## Dezbateri', '## 2. Proiectul 3'])
        item = result[result.index('## 1. Proiectul 2'):result.index('## Dezbateri')]
        self.assertIn('Text 1.', item)
        self.assertIn('**Rezultatul votului:** respins.\n**Anunțat în ședință:** „Proiectul a fost respins.”', item)
        self.assertNotIn('„5.”', item)
        last = result[result.index('## 2. Proiectul 3'):]
        self.assertIn('**Rezultatul votului:** neclar [de verificat].', last)  # its only quote was never said
        self.assertNotIn('6 voturi', result)
        self.assertIn('[S3] Proiectul a fost respins', llm.calls[0][0])
        fragments = ' '.join(call[0] for call in llm.calls[2::2])
        self.assertNotIn('Proiectul a fost respins', fragments)
        self.assertIn('consemnat separat', fragments)

    def test_a_repeat_vote_cannot_borrow_the_first_announcement(self):
        from norev.usecases.document_completion import parse_decisions
        transcript = Transcript((
            Segment(0, 2, 'Proiectul 1.'), Segment(2, 4, '13 voturi pentru, proiectul a fost adoptat.'),
            Segment(60, 62, 'Reluăm votul la proiectul 1.'), Segment(62, 64, 'Avem 3 abțineri.'),
        ), 'ro')
        answer = json.dumps({'meeting': {}, 'items': [
            {'time': '00:00', 'title': 'Proiectul 1', 'result': 'adoptat', 'announced': ['13 voturi pentru, proiectul a fost adoptat.']},
            {'time': '01:00', 'title': 'Reluarea votului: Proiectul 1', 'result': 'adoptat',
             'announced': ['13 voturi pentru, proiectul a fost adoptat.', 'Avem 3 abțineri.']}]})
        first, repeat = parse_decisions(answer, transcript)[1]
        self.assertEqual((first['result'], first['found']), ('adoptat', ['13 voturi pentru, proiectul a fost adoptat.']))
        self.assertEqual((repeat['result'], repeat['found']), ('neclar [de verificat]', ['Avem 3 abțineri.']))
        self.assertTrue(repeat['unchecked'])

    def test_unreadable_decisions_are_flagged_not_invented(self):
        from norev.usecases.document_completion import parse_decisions
        transcript = Transcript((Segment(0, 1, 'Proiectul a fost adoptat.'),), 'ro')
        self.assertEqual(parse_decisions('{"items": [', transcript), ({}, []))
        claimed = json.dumps({'meeting': {}, 'items': [{'time': '00:00', 'title': 'P1', 'result': 'respins',
                                                        'announced': ['Proiectul a fost adoptat.']}]})
        self.assertEqual(parse_decisions(claimed, transcript)[1][0]['result'], 'neclar [de verificat]')
        empty = json.dumps({'proposed_actions': [], 'added_actions': [], 'suggestions': []})
        llm = FakeLlmClient(['not json', empty, '{"output":"Text."}'])
        _, _, result = complete_document(llm, load_domain(FOLDER), transcript, date.today(), optional_completion(FOLDER),
                                         [], 's1', optional_decisions(FOLDER))
        self.assertIn('## Secvența 1', result)
        self.assertIn('[de verificat] Voturile nu au putut fi extrase', result)

    def test_procedural_votes_and_late_attendance_note_survive_an_incomplete_extraction(self):
        from norev.usecases.document_completion import parse_decisions
        initial = 'Sunt prezenți 20 de consilieri.'
        update = 'Doamna consilier nu s-a mai conectat.'
        transcript = Transcript((
            Segment(0, 2, initial),
            Segment(20, 22, 'Ordinea de zi a fost aprobată în unanimitate.'),
            Segment(90, 96, 'Persoanele înscrise la cuvânt să fie ascultate înainte de vot.'),
            Segment(100, 102, 'Trei abțineri.'),
            Segment(180, 182, update),
        ), 'ro')
        answer = {'meeting': {'attendance': initial}, 'items': [
            {'time': '00:20', 'title': 'Aprobarea ordinii de zi', 'result': 'neclar', 'announced': []},
        ]}
        meeting, votes = parse_decisions(json.dumps(answer), transcript)
        self.assertEqual(meeting['attendance_update'], update)
        self.assertEqual([(vote['result'], vote['found']) for vote in votes], [
            ('adoptat', ['Ordinea de zi a fost aprobată în unanimitate.']),
            ('neclar [de verificat]', ['Trei abțineri.']),
        ])

    def test_a_quote_cannot_match_only_the_last_digit_of_a_vote_count(self):
        from norev.usecases.document_completion import parse_decisions
        transcript = Transcript((Segment(0, 1, '15 voturi pentru.'),), 'ro')
        answer = json.dumps({'items': [{'time': '00:00', 'title': 'Proiect', 'result': 'neclar',
                                        'announced': ['5 voturi pentru.']}]})
        self.assertEqual(parse_decisions(answer, transcript)[1][0]['found'], [])

    def test_presence_estimate_requires_the_same_person_and_expected_count(self):
        from norev.usecases.document_completion import _inferred_attendance
        opening = 'Doamna Ionescu online? Va intra, deci vom fi 20 de consilieri.'
        self.assertEqual(_inferred_attendance(opening, ['Doamna Ionescu n-a intrat.'], '20'), 19)
        self.assertIsNone(_inferred_attendance(opening, ['Doamna Popescu n-a intrat.'], '20'))
        self.assertIsNone(_inferred_attendance('Sunt 20 de consilieri.', ['Doamna Ionescu n-a intrat.'], '20'))

    def test_the_spoken_title_recovers_a_vote_whose_start_was_set_too_late(self):
        from norev.usecases.document_completion import parse_decisions
        title = 'Proiect de hotărâre privind parcările'
        transcript = Transcript((
            Segment(0, 3, f'Numărul trei, {title}.'),
            Segment(4, 6, 'Proiectul a fost respins.'),
            Segment(10, 11, 'Îmi cer scuze.'),
        ), 'ro')
        answer = json.dumps({'items': [{'time': '00:10', 'title': title, 'result': 'respins',
                                        'announced': ['Proiectul a fost respins.']}]})
        item = parse_decisions(answer, transcript)[1][0]
        self.assertEqual((item['start'], item['result']), (0, 'respins'))

    def test_speaker_codes_never_reach_the_minutes(self):
        from norev.usecases.document_completion import _without_codes
        self.assertEqual(_without_codes('S8 a subliniat. Consilierul local S11 a spus că S3 e de acord [S2].'),
                         'Un vorbitor a subliniat. Consilierul local a spus că un vorbitor e de acord.')

    def test_line_by_line_retelling_counts_as_a_copy(self):
        retold = ' '.join(f'La 00:3{i}:00, S10 a spus ceva.' for i in range(6))
        self.assertTrue(_copied(retold))
        self.assertFalse(_copied('La 00:31:00 a început dezbaterea, apoi la 00:35:00 s-a votat.'))

    def test_many_short_segments_include_timestamp_overhead_in_request_limit(self):
        source = Transcript(tuple(Segment(i, i + 1, 'Da.', 'S1') for i in range(500)), 'ro')
        parts = list(chunks(source))
        self.assertGreater(len(parts), 1)
        for part in parts:
            from norev.usecases.document_completion import timestamp
            rendered = '\n'.join(f'[{timestamp(s.start)}–{timestamp(s.end)}] [{s.speaker_id}] {s.text}' for s in part.segments)
            self.assertLessEqual(len(rendered), LIMIT)

    def test_word_is_deterministic_valid_xml_and_escapes_text(self):
        text = '# Proces verbal\nȘedință & vot < 12\n## Hotărâri\nText editabil'
        data = render_docx(text)
        self.assertEqual(data, render_docx(text))
        with ZipFile(io.BytesIO(data)) as z:
            for name in z.namelist():
                ET.fromstring(z.read(name))
            root = ET.fromstring(z.read('word/document.xml'))
            self.assertIn('Ședință & vot < 12', ''.join(root.itertext()))

    def test_exports_never_overwrite_or_escape_the_chosen_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            connector = LocalDocumentConnector(root)
            for format in ('docx', 'txt', 'json'):
                payload = LocalDocumentPayloadBuilder(root).document('Ședință', format)
                self.assertTrue(connector.send(payload)['created'])
                self.assertFalse(connector.send(payload)['created'])
                Path(payload['destination']).write_bytes(b'User edited this')
                with self.assertRaises(ExportConflict):
                    connector.send(payload)
                with self.assertRaises(ValueError):
                    connector.send({**payload, 'destination': str(root.parent/'outside.docx')})
            with self.assertRaises(ValueError):
                LocalDocumentPayloadBuilder(root).document('text', '../docx')


class ExportReviewTest(Case):
    def test_document_flow_uses_spoken_text_and_preserves_previous_result_on_failure(self):
        from dataclasses import replace
        from norev.usecases.session_flow import run_session
        session = self.store.get_session(self.recorded())
        sid = 'council-flow'
        self.store.save_session(replace(session, id=sid, domain='local_council'))
        self.store.save_completion(sid, 'Rezultatul anterior')
        empty = json.dumps({'proposed_actions': [], 'added_actions': [], 'suggestions': []})
        kwargs = dict(store=self.store, translator=None, extractor=None, domain=load_domain(FOLDER),
                      completion=optional_completion(FOLDER))
        with self.assertRaises(ValueError):
            run_session(sid, llm=FakeLlmClient([empty, '{"output":""}']), **kwargs)
        self.assertEqual(self.store.load_completion(sid), 'Rezultatul anterior')
        llm = FakeLlmClient([empty, '{"output":"Document nou"}'])
        run_session(sid, llm=llm, **kwargs)
        self.assertIn('Document nou', self.store.load_completion(sid))
        spoken = self.store.load_transcript(session.source_transcript_ref)
        self.assertIn(spoken.segments[0].text, llm.calls[1][0])

    def test_the_transcript_is_corrected_once_when_the_meeting_is_over(self):
        from dataclasses import replace
        from norev.adapters.domains.loader import optional_correction
        from norev.usecases.session_flow import run_session
        session = self.store.get_session(self.recorded())
        sid = 'council-correction'
        ref = self.store.save_transcript(sid, Transcript((Segment(0, 3, 'Vai intra domnul Zenopint.', 'S1'),), 'ro'))
        self.store.save_session(replace(session, id=sid, domain='local_council', transcript_ref=ref, source_transcript_ref=None))
        empty = json.dumps({'proposed_actions': [], 'added_actions': [], 'suggestions': []})
        fix = json.dumps({'fixes': [{'i': 0, 'text': 'Va intra domnul Zeno Pinter.'}]})
        kwargs = dict(store=self.store, translator=None, extractor=None, domain=load_domain(FOLDER),
                      completion=optional_completion(FOLDER), correction=optional_correction(FOLDER))
        llm = FakeLlmClient([fix, empty, '{"output":"Document"}'])
        result = run_session(sid, llm=llm, **kwargs)
        corrected = self.store.get_session(sid)
        self.assertEqual(self.store.load_transcript(corrected.transcript_ref).segments[0].text, 'Vai intra domnul Zeno Pinter.')
        self.assertIn('Vai intra domnul Zeno Pinter.', llm.calls[2][0])  # the minutes read the corrected text
        self.assertIn('corectată automat în 1 replici', result.suggestions[0].text)
        again = FakeLlmClient([empty, '{"output":"Document"}'])  # a new analysis does not correct again
        run_session(sid, llm=again, regenerate=True, **kwargs)
        self.assertEqual(len(again.calls), 2)

    def test_transcript_exports_original_language_and_refuses_stale_preview(self):
        sid = self.recorded()
        preview = self.ok(self.bridge.transcript_preview(sid, 'json'))['payload']
        content = json.loads(preview['content'])
        self.assertEqual(content['language'], 'ro')
        self.assertTrue(content['segments'])
        saved = self.ok(self.bridge.transcript_save(sid, preview))['saved']
        self.assertTrue(Path(saved['path']).is_file())
        self.ok(self.bridge.edit_transcript(sid, 0, 'Text corectat de utilizator.'))
        self.assertEqual(self.error(self.bridge.transcript_save(sid, preview)), 'not_executable')

    def test_user_names_a_speaker_before_the_minutes_are_regenerated(self):
        from dataclasses import replace
        session = self.store.get_session(self.recorded())
        sid = 'council-names'
        ref = self.store.save_transcript(sid, Transcript((
            Segment(0, 5, 'Bună ziua, am o întrebare.', 'S1', speaker_name='Mureșan Maria-Diana'),
            Segment(5, 6, 'Da.', 'S1'),
            Segment(6, 12, 'Sunt cetățean și vreau să spun ceva.', 'S2'),
        ), 'ro'))
        self.store.save_session(replace(session, id=sid, domain='local_council', transcript_ref=ref, source_transcript_ref=None))
        from norev.core.speakers import speakers_in
        self.store.save_speakers(sid, speakers_in(self.store.load_transcript(ref)))
        speakers = {s['id']: s for s in self.ok(self.bridge.review(sid))['speakers']}
        self.assertEqual((speakers['S1']['name'], speakers['S1']['named'], speakers['S1']['segments']),
                         ('Mureșan Maria-Diana', 1, 2))
        self.assertIsNone(speakers['S2']['name'])
        review = self.ok(self.bridge.name_speaker(sid, 'S2', '  Ion   Popescu '))
        self.assertEqual([s.get('speaker_name') for s in review['transcript']['spoken']],
                         ['Mureșan Maria-Diana', None, 'Ion Popescu'])
        self.ok(self.bridge.name_speaker(sid, 'S1', ''))  # the User clears a wrong recognition
        spoken = self.store.load_transcript(self.store.get_session(sid).transcript_ref)
        self.assertEqual([s.speaker_name for s in spoken.segments], [None, None, 'Ion Popescu'])
        self.assertEqual(self.error(self.bridge.name_speaker(sid, 'S2', '[S1] Ion')), 'invalid_name')
        self.assertEqual(self.error(self.bridge.name_speaker(sid, 'S9', 'Ion')), 'invalid_speaker')

    def test_council_minutes_export_as_word(self):
        from dataclasses import replace
        session = self.store.get_session(self.recorded())
        sid = 'council-test'
        self.store.save_session(replace(session, id=sid, domain='local_council'))
        self.store.save_completion(sid, '## Dezbateri\nS-a discutat propunerea. Rezultatul votului: [de verificat].')
        payload = self.ok(self.bridge.completion_preview(sid))['payload']
        self.assertEqual(payload['format'], 'docx')
        self.assertIn('PROIECT', payload['content'])
        saved = self.ok(self.bridge.completion_save(sid, payload))['saved']
        self.assertTrue(ZipFile(saved['path']).testzip() is None)
