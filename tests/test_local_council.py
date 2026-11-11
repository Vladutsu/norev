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
        self.assertIn('[de verificat] Secvența nu a putut fi redactată', result)

    def test_votes_are_read_in_one_request_over_the_whole_meeting(self):
        debate = 'Discutăm despre proiect, argumente pro și contra. ' * 120
        transcript = Transcript((
            Segment(0, 5, 'Proiect de hotărâre numărul 2 privind parcările.', 'S1'), Segment(5, 6, 'Cine este pentru?', 'S1'),
            Segment(6, 300, debate, 'S2'), Segment(300, 301, '5.', 'S3'), Segment(301, 302, 'Proiectul a fost respins.', 'S3'),
        ), 'ro')
        excerpt = vote_excerpt(transcript, {})
        self.assertIn('[S1] Proiect de hotărâre numărul 2', excerpt)
        self.assertIn('[S3] 5.', excerpt)
        self.assertIn('respins', excerpt)
        self.assertNotIn('argumente pro', excerpt)
        empty = json.dumps({'proposed_actions': [], 'added_actions': [], 'suggestions': []})
        parts = len(list(chunks(transcript)))
        items = {'items': [{'time': '00:00:00', 'title': 'Proiectul 2, parcările', 'result': 'respins',
                            'announced': ['5.', 'Proiectul a fost respins.']},
                           {'time': '00:05:00', 'title': 'Proiectul 3', 'result': 'adoptat',
                            'announced': ['6 voturi pentru, proiectul a fost adoptat.']}]}
        llm = FakeLlmClient([empty, '{"output":"Dezbatere."}'] * parts + [json.dumps(items)])
        _, _, result = complete_document(llm, load_domain(FOLDER), transcript, date.today(), optional_completion(FOLDER),
                                         [], 's1', optional_decisions(FOLDER))
        self.assertGreater(parts, 1)
        self.assertIn('## Hotărâri și voturi\n\n00:00:00 — Proiectul 2, parcările\nRezultat anunțat: respins.\n'
                      'Anunțat în ședință: „5.”; „Proiectul a fost respins.”', result)
        # a quote that is not in the Transcript gives no outcome and no numbers
        self.assertIn('00:05:00 — Proiectul 3\nRezultat anunțat: neclar [de verificat].', result)
        self.assertNotIn('6 voturi', result)
        self.assertIn('Cine este pentru?', llm.calls[-1][0])
        self.assertIn('[S3] Proiectul a fost respins', llm.calls[-1][0])
        # the fragments never see an announced result, so they cannot write half a vote
        fragments = ' '.join(call[0] for call in llm.calls[1:-1:2])
        self.assertNotIn('Proiectul a fost respins', fragments)
        self.assertIn('consemnată în secțiunea', fragments)
        self.assertIn('Cine este pentru?', fragments)

    def test_unreadable_decisions_are_flagged_not_invented(self):
        from norev.usecases.document_completion import render_decisions
        transcript = Transcript((Segment(0, 1, 'Proiectul a fost adoptat.'),), 'ro')
        self.assertIn('[de verificat] Voturile nu au putut fi extrase', render_decisions('{"items": [', transcript))
        claimed = json.dumps({'items': [{'time': '1', 'title': 'P1', 'result': 'respins', 'announced': ['Proiectul a fost adoptat.']}]})
        self.assertIn('Rezultat anunțat: neclar [de verificat].', render_decisions(claimed, transcript))

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
