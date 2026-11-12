"""Ground experimental council vote and attendance fields in transcript segments."""

import re

from norev.usecases.document_completion import (
    AGENDA_APPROVAL, AGENDA_TITLE, ATTENDANCE_UPDATE, CHECK, EXPECTED_COUNT, OUTCOME, PUBLIC_HEARING,
    _inferred_attendance, _plain, timestamp,
)


_NUMBERS = {
    'zero': 0, 'un': 1, 'o': 1, 'unu': 1, 'una': 1, 'doi': 2, 'două': 2, 'trei': 3,
    'patru': 4, 'cinci': 5, 'șase': 6, 'șapte': 7, 'opt': 8, 'nouă': 9, 'zece': 10,
    'unsprezece': 11, 'doisprezece': 12, 'douăsprezece': 12, 'treisprezece': 13,
    'paisprezece': 14, 'cincisprezece': 15, 'șaisprezece': 16, 'șaptesprezece': 17,
    'optsprezece': 18, 'nouăsprezece': 19, 'douăzeci': 20,
}
_NUMBER = r'\d+|' + '|'.join(_NUMBERS)
_TALLY = re.compile(rf'\b(?P<number>{_NUMBER})\s+(?P<kind>vot(?:uri)?\s+(?:pentru|împotrivă)|abținer[ei])\b')
_BARE_COUNT = re.compile(rf'(?:{_NUMBER})')
_ATTENDANCE = re.compile(rf'\b(?:sunt|erau|rămân|au rămas)\s+prezenți\s+({_NUMBER})\b|'
                         rf'\b({_NUMBER})\s+(?:de\s+)?consilieri\s+prezenți\b')
_FINAL_ATTENDANCE = re.compile(r'\b(?:rămân|au rămas|în final|în urma)\b')
_PROJECT = re.compile(r'^(?:(?:numărul\s+)?\d+\s*[.,:\-–]\s*)?proiect(?:ul)? de hotărâre\b')
_REVOTE = re.compile(r'\b(?:reluăm|reluarea votului|se reia votul|repetăm votul)\b.*?'
                     r'\b(proiectul(?:\s+(?:numărul|nr\.?))?\s+\d+)\b')
_AGENDA_MOTION = re.compile(r'\b(?:supun|aprobării|aprobarea)\b')
_VOTE_CALL = re.compile(r'\b(?:supun(?:em)?\b.{0,35}\bla vot|cine\s+(?:este\s+pentru|se abține)|'
                        r'(?:este\s+)?cineva\s+împotrivă|se abține cineva|împotrivă\?)')
_RECOUNT = re.compile(r'\b(?:câte voturi|considerați și votul)\b')
_RECAP = re.compile(r'\b(?:a fost votat(?: anterior| prin)|votul anterior|pragul|sunt necesare|'
                    r'înseamnă|poate fi adoptat)\b')
_QUESTION = {
    'pentru': re.compile(r'\bcine este pentru\b'),
    'împotrivă': re.compile(r'\bîmpotrivă\b'),
    'abțineri': re.compile(r'\b(?:se abține|abținer[ei])\b'),
}


def _value(number):
    return int(number) if number.isdecimal() else _NUMBERS[number]


def _source(segments, index, **claim):
    segment = segments[index]
    return {**claim, 'segment_id': index, 'start': segment.start, 'end': segment.end, 'quote': segment.text}


def validate_attendance(meeting, segments):
    announcements = []
    updates = []
    for index, segment in enumerate(segments):
        line = _plain(segment.text)
        match = _ATTENDANCE.search(line) or EXPECTED_COUNT.search(line)
        if match and '?' not in segment.text:
            value = _value(next(part for part in match.groups() if part is not None))
            status = 'finală' if _FINAL_ATTENDANCE.search(line) else 'anunțată'
            announcements.append(_source(segments, index, value=value, status=status))
        if ATTENDANCE_UPDATE.search(line):
            updates.append(_source(segments, index))

    meeting = {key: value for key, value in meeting.items() if not key.startswith('attendance')}
    meeting['attendance_evidence'] = sorted(announcements + updates, key=lambda entry: entry['segment_id'])
    if not announcements:
        return meeting
    first, last = announcements[0], announcements[-1]
    meeting['attendance'] = str(first['value'])
    later = [source for source in updates if source['segment_id'] > last['segment_id']]
    if last['status'] == 'finală' and not later:
        meeting['attendance'] = str(last['value'])
        meeting['attendance_final'] = last['value']
    elif later:
        meeting['attendance_update'] = later[-1]['quote']
        opening = ' '.join(_plain(segment.text) for segment in segments[:last['segment_id'] + 1])
        estimate = _inferred_attendance(opening, [source['quote'] for source in later], str(last['value']))
        if estimate is not None:
            meeting['attendance_inferred'] = estimate
    return meeting


def _anchors(segments):
    for index, segment in enumerate(segments):
        line = _plain(segment.text)
        repeat = _REVOTE.search(line)
        if repeat:
            yield index, f'Reluarea votului: {repeat[1]}', False
        elif _PROJECT.match(line):
            yield index, segment.text.strip(), False
        elif PUBLIC_HEARING.search(line):
            yield index, 'Audierea persoanelor înscrise la cuvânt', False
        elif AGENDA_TITLE.search(line) and _AGENDA_MOTION.search(line) and not AGENDA_APPROVAL.search(line):
            yield index, 'Aprobarea ordinii de zi', True
        elif AGENDA_APPROVAL.search(line):
            yield index, 'Aprobarea ordinii de zi', True


def _vote_evidence(segments, start, stop):
    last_recount = max((i for i in range(start, stop) if _RECOUNT.search(_plain(segments[i].text))), default=-1)
    counts = {}
    outcomes = {}
    last_call = None
    for index in range(start, stop):
        segment = segments[index]
        line = _plain(segment.text)
        if _VOTE_CALL.search(segment.text.lower()):
            last_call = segment.end
        if AGENDA_APPROVAL.search(line):
            outcomes['adoptat'] = _source(segments, index, category='rezultat', value='adoptat')
        if last_call is None or segment.start - last_call > 35 or '?' in segment.text or _RECAP.search(line):
            continue
        for match in _TALLY.finditer(line):
            kind = match['kind'].split()[-1] if match['kind'].startswith('vot') else 'abțineri'
            number = _value(match['number'])
            counts.setdefault(kind, {})[number] = _source(segments, index, category=kind, value=number)
        outcome = OUTCOME.search(line)
        if outcome:
            result = 'respins' if 'respins' in outcome[0] else 'adoptat'
            outcomes[result] = _source(segments, index, category='rezultat', value=result)
        if index <= max(start, last_recount) or not _BARE_COUNT.fullmatch(line):
            continue
        previous = segments[index - 1]
        question = _plain(previous.text)
        kinds = [kind for kind, pattern in _QUESTION.items() if pattern.search(question)]
        following = segments[index + 1].text if index + 1 < stop else ''
        if len(kinds) != 1 or '?' not in previous.text or segment.start - previous.end > 8:
            continue
        kind = kinds[0]
        if '?' in following and _QUESTION[kind].search(_plain(following)):
            continue
        number = _value(line)
        counts.setdefault(kind, {})[number] = _source(segments, index, category=kind, value=number,
                                                      question_segment_id=index - 1)
    result = next(iter(outcomes)) if len(outcomes) == 1 else f'neclar {CHECK}'
    evidence = [next(iter(values.values())) for values in counts.values() if len(values) == 1]
    if len(outcomes) == 1:
        evidence.extend(outcomes.values())
    conflicts = [kind for kind, values in counts.items() if len(values) > 1]
    return result, sorted(evidence, key=lambda entry: entry['segment_id']), conflicts


def validate_votes(segments):
    anchors = list(_anchors(segments))
    items = []
    merged_approval = None
    for position, (start, title, agenda) in enumerate(anchors):
        if start == merged_approval:
            continue
        boundary = anchors[position + 1][0] if position + 1 < len(anchors) else len(segments)
        # An agenda motion and its approval describe the same vote.
        if (agenda and not AGENDA_APPROVAL.search(_plain(segments[start].text)) and boundary < len(segments)
                and AGENDA_APPROVAL.search(_plain(segments[boundary].text))):
            if segments[boundary].start - segments[start].start <= 90:
                merged_approval = boundary
                boundary = anchors[position + 2][0] if position + 2 < len(anchors) else len(segments)
        stop = next((i for i in range(start + 1, boundary) if segments[i].start - segments[start].start > 150), boundary)
        if not any(_VOTE_CALL.search(s.text.lower()) or AGENDA_APPROVAL.search(_plain(s.text))
                   for s in segments[start:stop] if s.start - segments[start].start <= 60):
            continue
        result, evidence, conflicts = _vote_evidence(segments, start, stop)
        end = max((claim['end'] for claim in evidence), default=segments[start].end)
        interval_end = segments[stop].start if stop < len(segments) else segments[-1].end
        items.append({'title': title, 'start': segments[start].start, 'end': end,
                      'interval_end': interval_end, 'anchor_segment_id': start, 'result': result,
                      'vote_evidence': evidence, 'conflicts': conflicts})
    return items


def render_vote(item):
    lines = [f"**Rezultatul votului:** {item['result']}."]
    for evidence in item['vote_evidence']:
        time = timestamp(evidence['start'])
        kind, value = evidence['category'], evidence['value']
        if kind == 'rezultat':
            unanimity = ' în unanimitate' if re.search(r'\bunanim\w*', _plain(evidence['quote'])) else ''
            lines.append(f'**Anunțat în ședință:** {value}{unanimity} [{time}]')
        else:
            lines.append(f'**{kind.capitalize()}:** {value} [{time}]')
    if item['conflicts']:
        lines.append(f'{CHECK} Cifre contradictorii pentru: {", ".join(item["conflicts"])}.')
    return '\n\n'.join(lines)


def procedural_claim(text):
    line = _plain(text)
    return bool(_TALLY.search(line) or _ATTENDANCE.search(line) or EXPECTED_COUNT.search(line) or OUTCOME.search(line))
