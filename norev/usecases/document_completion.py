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


def complete_document(llm, domain, transcript, session_date, completion, speakers, prefix, decisions=None):
    template, schema = completion
    labels = {r.name: r.label or r.name for r in domain.roles}
    # only Roles the User confirmed: a guessed Role written into the minutes reads as a fact
    who = {s.id: f"{s.id} · {labels[s.role]}" for s in speakers if s.role_confirmed and s.role in labels}
    actions, suggestions, sections = [], [], []
    for index, part in enumerate(chunks(transcript), 1):
        try:
            found, hints = ask_domain(domain, llm, part, [], session_date, speakers,
                                      id_prefix=f"{prefix}-part{index}")
        except ValueError:  # the follow-ups of one fragment are lost, the minutes are not
            found, hints = [], [Suggestion(f"Măsurile din secvența {index} nu au putut fi extrase; "
                                           "verifică-le în procesul-verbal.")]
        actions.extend(found)
        suggestions.extend(hints)
        source = "\n".join(f"[{timestamp(s.start)}–{timestamp(s.end)}] "
                           f"[{who.get(s.speaker_id, s.speaker_id) if s.speaker_id else '?'}] "
                           f"{MASKED if decisions and _announces_result(s.text) else s.text}"
                           for s in part.segments)
        prompt = fill(template, {"transcript": source})
        output = _output(llm.generate_json(prompt, model=domain.model, json_schema=schema))
        if _copied(output):  # the model repeated the transcript instead of writing the document
            output = _output(llm.generate_json(prompt + RETRY, model=domain.model, json_schema=schema))
            if _copied(output):
                output = ("[de verificat] Secvența nu a putut fi redactată automat; "
                          "consultați transcrierea pentru acest interval.")
        sections.append(f"## Secvența {index} · {timestamp(part.segments[0].start)}–{timestamp(part.segments[-1].end)}\n\n{output.strip()}")
    if not sections:
        raise ValueError("empty Transcript")
    if decisions:
        # one request over every vote line, so a vote is never cut at a fragment boundary
        excerpt = vote_excerpt(transcript, who)
        template, decisions_schema = decisions
        raw = llm.generate_json(fill(template, {"transcript": excerpt or "(niciun vot în transcriere)"}),
                                model=domain.model, json_schema=decisions_schema)
        sections.append(f"## Hotărâri și voturi\n\n{render_decisions(raw, transcript)}")
    return actions, suggestions, "\n\n".join(sections)


RETRY = ("\n\nATENȚIE: nu copia replicile și nu le cita una câte una. Rezumă în propoziții proprii, "
         "cel mult 250 de cuvinte, cu reperele temporale ale subiectelor.")
_LINE = re.compile(r"^\s*(\[\d{2}:\d{2}:\d{2}|La \d{2}:\d{2}:\d{2})", re.M)
_INLINE = re.compile(r"\b[Ll]a \d{2}:\d{2}(:\d{2})?\b")
VOTE = re.compile(r"proiect(ul)? de hotărâre|supun|adoptat|respins|unanim|cine este pentru|se abține|împotrivă\?|abțineri|"
                  r"voturi|ordinea de zi|prezenți|legal constituit|ședința (închisă|deschisă)|declar ședința|reluăm|"
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
MASKED = "[anunțarea rezultatului: consemnată în secțiunea „Hotărâri și voturi”]"
CHECK = "[de verificat]"


def _announces_result(text: str) -> bool:
    """A line that announces counts or the outcome: kept out of the fragments, so the votes are
    written once, from the whole meeting, and never from half a vote."""
    text = text.strip()
    return bool(COUNT.match(text) or (RESULT.search(text) and len(text) <= 200))


def _plain(text: str) -> str:
    return " ".join(text.lower().split()).strip(" .,!?;:")


def render_decisions(raw: str, transcript: Transcript) -> str:
    """The model's items, with every number and outcome taken only from announcements quoted
    exactly from the Transcript; a quote that is not there, or an outcome no quote states, is flagged."""
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:
        answer = None
    items = answer.get("items") if isinstance(answer, dict) else None
    if not isinstance(items, list) or not items:
        return f"{CHECK} Voturile nu au putut fi extrase automat; consultați transcrierea."
    spoken = [_plain(s.text) for s in transcript.segments]
    paragraphs = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = " ".join(str(item.get("title") or "").split()) or CHECK
        time = " ".join(str(item.get("time") or "").split()) or CHECK
        quotes = [q for q in item.get("announced") or [] if isinstance(q, str) and q.strip()]
        found = [q.strip() for q in quotes if any(_plain(q) and _plain(q) in line for line in spoken)]
        result = item.get("result")
        backed = {"adoptat": ("adoptat", "aprobat", "unanim"), "respins": ("respins",)}.get(result, ())
        if not any(word in _plain(q) for q in found for word in backed):
            result = f"neclar {CHECK}"
        text = f"{time} — {title}\nRezultat anunțat: {result}."
        if found:
            text += "\nAnunțat în ședință: " + "; ".join(f"„{q}”" for q in found)
        if len(found) < len(quotes) or not found:
            text += f"\n{CHECK} Cifrele votului trebuie verificate pe înregistrare."
        paragraphs.append(text)
    return "\n\n".join(paragraphs) or f"{CHECK} Voturile nu au putut fi extrase automat; consultați transcrierea."
