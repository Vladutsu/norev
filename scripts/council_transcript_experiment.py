"""Run a reproducible council benchmark on an exported, corrected transcript."""

import argparse
from dataclasses import replace
from datetime import date
import hashlib
import json
from pathlib import Path
import time

from LLM.AI_Model.budgeted import BudgetedLlama
from norev.adapters.domains.loader import DOMAINS, load_domain, optional_completion, optional_decisions
from norev.core.models import Segment, Transcript
from norev.usecases.council_transcript import experiment
from norev.usecases.document_completion import complete_document, parse_decisions


MODES = ('baseline', 'sliding', 'notes', 'sliding-notes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('transcript', type=Path, help='JSON transcript exported by Tasca')
    parser.add_argument('output', type=Path)
    parser.add_argument('--url', default='http://127.0.0.1:18083')
    parser.add_argument('--model', help='Name of the model loaded in llama-server')
    parser.add_argument('--date', required=True, type=date.fromisoformat)
    parser.add_argument('--modes', nargs='+', choices=MODES, default=MODES)
    parser.add_argument('--validate-evidence', action='store_true', help='Ground sliding-notes votes and attendance in the transcript')
    args = parser.parse_args()
    if args.validate_evidence and args.modes != ['sliding-notes']:
        parser.error('--validate-evidence requires --modes sliding-notes')
    source = args.transcript.read_bytes()
    data = json.loads(source)
    transcript = Transcript(tuple(Segment(**s) for s in data['segments']), data['language'])
    args.output.mkdir(parents=True, exist_ok=True)
    folder = DOMAINS / 'local_council'
    domain, completion, decisions = load_domain(folder), optional_completion(folder), optional_decisions(folder)
    if args.model:
        domain = replace(domain, model=args.model)
    for mode in args.modes:
        print(f'Running {mode} on {len(transcript.segments)} segments', flush=True)
        started = time.monotonic()
        def report(call):
            print(f"  {call['prompt_tokens']} in, {call['completion_tokens']} out, {call['finish_reason']}", flush=True)

        llm = BudgetedLlama(args.url, report=report)
        notes = []
        if mode == 'baseline':
            _, _, document = complete_document(llm, domain, transcript, args.date, completion, [], mode, decisions)
            meeting, items = parse_decisions(llm.first_answer, transcript)
        else:
            document, meeting, items, notes = experiment(llm, domain, transcript, args.date, completion, decisions, mode,
                                                        validate_evidence=args.validate_evidence)
        (args.output / f'{mode}.md').write_text(document + '\n', encoding='utf-8')
        result = {'mode': mode, 'model': domain.model, 'source_sha256': hashlib.sha256(source).hexdigest(),
                  'segments': len(transcript.segments), 'context': llm.context, 'input_limit': llm.limit,
                  'validated_evidence': args.validate_evidence,
                  'seconds': round(time.monotonic() - started, 1), 'calls': llm.calls,
                  'meeting': meeting, 'items': items, 'notes': notes}
        (args.output / f'{mode}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n',
                                                encoding='utf-8')
        print(f'Finished {mode} in {result["seconds"]}s', flush=True)


if __name__ == '__main__':
    main()
