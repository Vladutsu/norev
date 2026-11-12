"""Bounded document generation from the whole spoken Transcript, through the Central Processor."""
import json
import re

from norev.core.models import Segment, Suggestion, Transcript
from norev.usecases.domain_stage import ask_domain
from norev.usecases.others_prompt import fill


# ~1.3k tokens of Romanian per request: prompt, fragment and answer stay inside an 8k context (#council e2e)
LIMIT = 4000


def chunks(transcript: Transcript, limit: int = LIMIT):
    """Keep every character, including an unusually long segment, within a bounded request."""
    batch, size = [], 0
    for segment in transcript.segments:
        overhead = len(f"[{timestamp(segment.start)}–{timestamp(segment.end)}] [{segment.speaker_id or '?'}] \n")
        available = limit - overhead
        if available <= 0:
            raise ValueError("Transcript segment metadata exceeds request limit")
        for offset in range(0, len(segment.text), available):
            text = segment.text[offset:offset + available]
            if batch and size + len(text) + overhead > limit:
                yield Transcript(tuple(batch), transcript.language)
                batch, size = [], 0
            batch.append(Segment(segment.start, segment.end, text, segment.speaker_id))
            size += len(text) + overhead
    if batch:
        yield Transcript(tuple(batch), transcript.language)


def timestamp(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def confirmed_speaker_labels(domain, speakers):
    labels = {r.name: r.label or r.name for r in domain.roles}
    return {s.id: f"{s.id} · {labels[s.role]}" for s in speakers if s.role_confirmed and s.role in labels}


def complete_document(llm, domain, transcript, session_date, completion, speakers, prefix, decisions=None):
    """The document as Markdown. With `decisions` (local council), it follows the agenda: a header and,
    for each item voted, its debate followed by its vote, both read from the whole meeting first.
    Without them, numbered time sequences."""
    who = confirmed_speaker_labels(domain, speakers)
    if not transcript.segments:
        raise ValueError("empty Transcript")
    writer = _Writer(llm, domain, session_date, completion, speakers, prefix, who, masked=decisions is not None)
    if decisions:
        # one request over every vote line, so a vote is never cut at a fragment boundary
        template, decisions_schema = decisions
        excerpt = vote_excerpt(transcript, who)
        raw = llm.generate_json(fill(template, {"transcript": excerpt or "(niciun vot în transcriere)"}),
                                model=domain.model, json_schema=decisions_schema)
        meeting, items = parse_decisions(raw, transcript)
        if items:
            return writer.actions, writer.suggestions, _agenda_document(writer, transcript, meeting, items)
    sections = ["Reperele de timp sunt timpi din înregistrare. Localitatea, data, participanții și cvorumul "
                "se completează din sursa originală."] if decisions else []
    for part in chunks(transcript):
        sections.append(f"## Secvența {writer.index + 1} · {timestamp(part.segments[0].start)}–"
                        f"{timestamp(part.segments[-1].end)}\n\n{writer.write(part)}")
    if decisions:
        sections.append(f"## Hotărâri și voturi\n\n{CHECK} Voturile nu au putut fi extrase automat; consultați transcrierea.")
    return writer.actions, writer.suggestions, "\n\n".join(sections)


class _Writer:
    """One fragment at a time: its follow-ups (Domain stage) and its prose (completion prompt)."""

    def __init__(self, llm, domain, session_date, completion, speakers, prefix, who, masked):
        self.llm, self.domain, self.session_date, self.speakers = llm, domain, session_date, speakers
        self.template, self.schema = completion
        self.prefix, self.who, self.masked = prefix, who, masked
        self.index, self.actions, self.suggestions = 0, [], []

    def write(self, part: Transcript) -> str:
        self.index += 1
        self.collect_followups(part, self.index)
        source = "\n".join(f"[{timestamp(s.start)}–{timestamp(s.end)}] "
                           f"[{self.who.get(s.speaker_id, s.speaker_id) if s.speaker_id else '?'}] "
                           f"{MASKED if self.masked and _announces_result(s.text) else s.text}"
                           for s in part.segments)
        prompt = fill(self.template, {"transcript": source})
        output = _output(self.llm.generate_json(prompt, model=self.domain.model, json_schema=self.schema))
        if _copied(output):  # the model repeated the transcript instead of writing the document
            output = _output(self.llm.generate_json(prompt + RETRY, model=self.domain.model, json_schema=self.schema))
            if _copied(output):
                output = (f"{CHECK} Fragmentul nu a putut fi redactat automat; "
                          "consultați transcrierea pentru acest interval.")
        return _without_codes(output.strip())

    def collect_followups(self, part: Transcript, index: int) -> None:
        try:
            found, hints = ask_domain(self.domain, self.llm, part, [], self.session_date, self.speakers,
                                      id_prefix=f"{self.prefix}-part{index}")
        except ValueError:  # the follow-ups of one fragment are lost, the minutes are not
            found, hints = [], [Suggestion(f"Măsurile din fragmentul {index} nu au putut fi extrase; "
                                           "verifică-le în procesul-verbal.")]
        self.actions.extend(found)
        self.suggestions.extend(hints)


GAP = 120  # seconds of talk between one vote and the next item: a debate of its own, not part of the vote


def _agenda_document(writer: _Writer, transcript: Transcript, meeting: dict, items: list[dict], *, render_vote=None) -> str:
    segments = transcript.segments
    end = segments[-1].end + 1
    plan = [("Deschiderea ședinței", 0.0, items[0]["start"], None)]
    for number, item in enumerate(items, 1):
        following = items[number]["start"] if number < len(items) else end
        heading = f"{number}. {item['title']}"
        done = min(following, item["end"] + 1)
        if following - done > GAP:
            plan.append((heading, item["start"], done, item))
            plan.append(("Închiderea ședinței" if number == len(items) else "Dezbateri", done, following, None))
        else:
            plan.append((heading, item["start"], following, item))
    out = [_header(meeting)]
    for heading, start, stop, item in plan:
        part = Transcript(tuple(s for s in segments if start <= s.start < stop), transcript.language)
        fragments = list(chunks(part)) if part.segments else []
        if item is None and heading == "Deschiderea ședinței" and stop - start > OPENING:
            # a long stretch before the first vote is the debate: only its first fragment opens the meeting
            out.append(f"## {heading}\n\n{writer_paragraph(writer, fragments[0])}")
            heading, fragments = "Dezbateri", fragments[1:]
        body = [writer_paragraph(writer, fragment) for fragment in fragments]
        if item is not None:
            body.append((render_vote or _vote)(item))
        if body:
            out.append(f"## {heading}\n\n" + "\n\n".join(body))
    return "\n\n".join(out)


OPENING = 600  # seconds; more than this before the first item voted is a debate, not just the opening


def writer_paragraph(writer: _Writer, fragment: Transcript) -> str:
    return f"[{timestamp(fragment.segments[0].start)}–{timestamp(fragment.segments[-1].end)}] {writer.write(fragment)}"


def _header(meeting: dict) -> str:
    kind = meeting.get("type") or CHECK
    place = meeting.get("locality") or CHECK
    lines = [f"**Ședința:** {kind} a Consiliului Local {place}",
             "**Data ședinței:** [de completat]",
             f"**Prezență anunțată:** „{meeting['attendance']}”" if meeting.get("attendance_update")
             else f"**Prezență:** „{meeting['attendance']}”" if meeting.get("attendance")
             else "**Prezență:** [de verificat în lista de prezență]",
             f"**Conducerea ședinței:** „{meeting['chair']}”" if meeting.get("chair")
             else "**Conducerea ședinței:** [de completat]",
             "Reperele de timp [hh:mm:ss] sunt timpi din înregistrare."]
    if meeting.get("attendance_inferred") is not None:
        lines.insert(3, f"**Prezență estimată din transcriere:** {meeting['attendance_inferred']} de consilieri "
                        "(cifra anticipată includea o persoană care ulterior nu s-a conectat).")
    elif meeting.get("attendance_update"):
        lines.insert(3, f"{CHECK} Prezența anunțată inițial trebuie confruntată cu lista finală. "
                        f"Precizare ulterioară: „{meeting['attendance_update']}”")
    return "\n".join(lines)


def _vote(item: dict) -> str:
    lines = [f"**Rezultatul votului:** {item['result']}."]
    if item["found"]:
        lines.append("**Anunțat în ședință:** " + "; ".join(f"„{q}”" for q in item["found"]))
    if item["unchecked"]:
        lines.append(f"{CHECK} Cifrele votului trebuie verificate pe înregistrare.")
    return "\n".join(lines)


RETRY = ("\n\nATENȚIE: nu copia replicile și nu le cita una câte una. Rezumă în propoziții proprii, "
         "cel mult 250 de cuvinte, cu reperele temporale ale subiectelor.")
_LINE = re.compile(r"^\s*(\[\d{2}:\d{2}:\d{2}|La \d{2}:\d{2}:\d{2})", re.M)
_INLINE = re.compile(r"\b[Ll]a \d{2}:\d{2}(:\d{2})?\b")
VOTE = re.compile(r"proiect(ul)? de hotărâre|convocat|conducerea|supun|adoptat|respins|unanim|cine este pentru|se abține|împotrivă\?|abțineri|"
                  r"voturi|ordinea de zi|prezenți|legal constituit|ședința (închisă|deschisă)|declar ședința|reluăm|"
                  r"înscri[șs][ei] la cuvânt|ascultat[eă]|abținer[ei]|nu s-a mai conectat|n-am văzut-o|"
                  r"proiectul (numărul|nr)", re.I)
COUNT = re.compile(r"^\W*(\d+|unu|una|doi|două|trei|patru|cinci|șase|șapte|opt|nouă|zece)\W*$", re.I)
VOTES_LIMIT = 8000  # with the prompt and the answer, inside an 8k context


def _output(raw: str) -> str:
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("invalid document completion") from None
    output = answer.get("output") if isinstance(answer, dict) else None
    if not isinstance(output, str) or not output.strip() or len(output) > 16000:
        raise ValueError("invalid document completion")
    return output


def _copied(output: str) -> bool:
    """Minutes are prose: three lines opening with a timestamp, or six "La 00:31:46, S10 a spus..."
    in a row of text, are the transcript repeated line by line."""
    return len(_LINE.findall(output)) >= 3 or len(_INLINE.findall(output)) >= 6


def vote_excerpt(transcript: Transcript, who: dict[str, str]) -> str:
    """The lines about the agenda, attendance and votes, with the short replies around them
    ("Cine este pentru?" / "5."), in order; bounded so it fits one request."""
    segments = transcript.segments
    keep: set[int] = set()
    for i, segment in enumerate(segments):
        text = segment.text.strip()
        if (VOTE.search(text) and len(text) <= 400) or COUNT.match(text):
            keep.add(i)
            keep.update(j for j in (i - 1, i + 1) if 0 <= j < len(segments) and len(segments[j].text) <= 200)
    lines, size = [], 0
    for i in sorted(keep):
        s = segments[i]
        line = (f"[{timestamp(s.start)}] [{who.get(s.speaker_id, s.speaker_id) if s.speaker_id else '?'}] "
                f"{' '.join(s.text.split())[:400]}")
        if size + len(line) > VOTES_LIMIT:
            lines.append("[extras trunchiat: verificați restul voturilor în transcriere]")
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


RESULT = re.compile(r"adoptat|respins|aprobat|unanim|voturi|abțin|împotrivă(?!\?)", re.I)
OUTCOME = re.compile(r"\b(?:a(?:u)? fost|este|s-a)\s+(?:adoptat[ăe]?|aprobat[ăe]?|respins[ăe]?)\b|"
                     r"\b(?:adoptat|aprobat|respins)[.!]?$", re.I)
TALLY = re.compile(r"\b(?:\d+|un|unu|una|doi|două|trei|patru|cinci|șase|șapte|opt|nouă|zece)\s+"
                   r"(?:voturi? (?:pentru|împotrivă)|abținer[ei])\b", re.I)
ATTENDANCE_UPDATE = re.compile(r"\b(?:nu s-a mai conectat|nu s-a conectat|n-am văzut-o deloc)\b", re.I)
EXPECTED_ONLINE = re.compile(r"\bdoamna\s+(\w+)\s+online\b", re.I)
EXPECTED_COUNT = re.compile(r"\b(?:vom fi|vom avea)\s+(\d+)\s+de consilieri\b", re.I)


def _inferred_attendance(opening: str, updates: list[str], attendance: str) -> int | None:
    """Subtract one only when the opening explicitly anticipates her in its count."""
    expected = EXPECTED_ONLINE.findall(opening)
    count = EXPECTED_COUNT.search(opening)
    if len(expected) != 1 or not count or int(count[1]) < 2 or not _said_in(count[1], attendance):
        return None
    missing = re.compile(rf"\bdoamna\s+{re.escape(expected[0])}\b[^.!?]{{0,120}}"
                         r"\b(?:n-a intrat|nu s-a(?: mai)? conectat)\b", re.I)
    return int(count[1]) - 1 if any(missing.search(update) for update in updates) else None
AGENDA_APPROVAL = re.compile(r"\bordinea de zi\b.*\b(?:a|au) fost aprobat[ăe] în unanimitate\b", re.I)
AGENDA_TITLE = re.compile(r"\bordin(?:ea|ei|ii) de zi\b", re.I)
PUBLIC_HEARING = re.compile(r"\b(?:persoanele|cei) înscri[șs]i?e? la cuvânt\b.*\b(?:înainte|ascultat)", re.I)
MASKED = "[rezultatul votului: consemnat separat, sub punctul votat]"
CHECK = "[de verificat]"
_BRACKETED = re.compile(r"\s*\[S\d+(?: · [^\]]*)?\]")
_NAMED = re.compile(r"\b(consilierul(?: local)?|cetățeanul|vorbitorul|primarul|președintele(?: de ședință)?|participantul) S\d+\b", re.I)
_BARE = re.compile(r"(^|[.!?]\s+|\s)S\d+\b")


def _without_codes(text: str) -> str:
    """Speaker ids only tell voices apart in the Transcript; the minutes never show them."""
    text = _NAMED.sub(r"\1", _BRACKETED.sub("", text))
    return _BARE.sub(lambda m: m.group(1) + ("Un vorbitor" if m.group(1) == "" or m.group(1).strip() in (".", "!", "?")
                                             else "un vorbitor"), text)


def _announces_result(text: str) -> bool:
    """A line that announces counts or the outcome: kept out of the fragments, so the votes are
    written once, from the whole meeting, and never from half a vote."""
    text = text.strip()
    return bool(COUNT.match(text) or (RESULT.search(text) and len(text) <= 200))


def _plain(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .,!?;:")


def _seconds(text: str) -> float | None:
    match = re.fullmatch(r"\s*\[?(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\]?\s*", text or "")
    if not match:
        return None
    hours, minutes, seconds = (int(v) if v else 0 for v in match.groups())
    return float(hours * 3600 + minutes * 60 + seconds)


def parse_decisions(raw: str, transcript: Transcript) -> tuple[dict, list[dict]]:
    """The header and the voted items, keeping only what exact quotes from the Transcript support:
    a quote that is not there is dropped, an outcome no kept quote states becomes unclear, an item
    with no usable time is dropped. Items come back in time order, each with where its vote ends."""
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:
        return {}, []
    if not isinstance(answer, dict):
        return {}, []
    segments = transcript.segments
    spoken = [_plain(s.text) for s in segments]
    whole = " ".join(spoken)

    def said(quote) -> bool:
        return isinstance(quote, str) and _said_in(quote, whole)

    raw_meeting = answer.get("meeting") if isinstance(answer.get("meeting"), dict) else {}
    meeting = {key: " ".join(str(raw_meeting.get(key) or "").split()) for key in ("locality", "type", "attendance", "chair")}
    meeting = {key: value if said(value) else "" for key, value in meeting.items()}
    if meeting["attendance"]:
        initial = next((i for i, line in enumerate(spoken) if _said_in(meeting["attendance"], line)), len(spoken))
        updates = [s.text for s in segments[initial + 1:] if ATTENDANCE_UPDATE.search(s.text)]
        if updates:
            meeting["attendance_update"] = updates[-1]
            opening = " ".join(spoken[:initial + 4])
            inferred = _inferred_attendance(opening, updates, meeting["attendance"])
            if inferred is not None:
                meeting["attendance_inferred"] = inferred
    items = []
    for item in answer.get("items") or []:
        if not isinstance(item, dict):
            continue
        start = _seconds(str(item.get("time") or ""))
        if start is None:
            continue
        title = " ".join(str(item.get("title") or "").split()) or CHECK
        if _plain(title).startswith("proiect"):
            read_at = [s.start for s, line in zip(segments, spoken)
                       if 0 <= start - s.start <= 60 and _said_in(title, line)]
            if len(read_at) == 1:
                start = read_at[0]
        start = next((s.start for s in segments if s.start <= start < s.end), start)
        quotes = [q.strip() for q in item.get("announced") or [] if isinstance(q, str) and q.strip()]
        items.append({"title": title, "start": start,
                      "claimed": item.get("result"), "quotes": quotes})
    for i, segment in enumerate(segments):
        line = spoken[i]
        if AGENDA_APPROVAL.search(line):
            existing = next((item for item in items if 0 <= segment.start - item["start"] <= 90
                             and AGENDA_TITLE.search(item["title"])), None)
            if existing:
                existing["quotes"].append(segment.text)
                existing["claimed"] = "adoptat"
            else:
                items.append({"title": "Aprobarea ordinii de zi", "start": segment.start,
                              "claimed": "adoptat", "quotes": [segment.text]})
        if PUBLIC_HEARING.search(line):
            if any(abs(item["start"] - segment.start) <= 30 for item in items):
                continue
            replies = [s.text for s in segments[i + 1:] if s.start <= segment.end + 15
                       and (TALLY.search(s.text) or OUTCOME.search(s.text))]
            if replies:
                items.append({"title": "Audierea persoanelor înscrise la cuvânt", "start": segment.start,
                              "claimed": "neclar", "quotes": replies})
    items.sort(key=lambda i: i["start"])
    for number, item in enumerate(items):
        # an announcement counts only where this item was voted: before the next item is read
        until = items[number + 1]["start"] if number + 1 < len(items) else float("inf")
        spans = [(s, line) for s, line in zip(segments, spoken) if item["start"] - 5 <= s.start < until]
        found = [q for q in item["quotes"]
                 if (OUTCOME.search(q) or TALLY.search(q)) and not q.rstrip().endswith("?")
                 and any(_said_in(q, line) for _, line in spans)]
        ends = [s.end for q in found for s, line in spans if _said_in(q, line)]
        backed = {"adoptat": ("adoptat", "aprobat", "unanim"), "respins": ("respins",)}.get(item["claimed"], ())
        result = item["claimed"] if any(OUTCOME.search(q) and w in _plain(q)
                                       for q in found for w in backed) else f"neclar {CHECK}"
        item.update(end=max(ends, default=item["start"]), result=result, found=list(dict.fromkeys(found)),
                    unchecked=len(found) < len(item["quotes"]) or result.endswith(CHECK))
    return meeting, items


def _said_in(quote: str, line: str) -> bool:
    normalized = _plain(quote)
    return bool(normalized and re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", line))
