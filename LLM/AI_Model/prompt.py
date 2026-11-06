import json
import os
from datetime import date, timedelta


PROPOSED_ACTION_PROMPT = """
You are a senior Project Manager with a rigorous, execution-focused mindset. You receive (part of) the
Transcript of a Session (one recorded conversation) and must extract every Proposed Action: a concrete
action someone has to carry out. The Transcript comes from speech recognition and has no speaker labels.

What is a Proposed Action:
- Any commitment or request to do something: "X, can you ...?" followed by agreement, "please send ...",
  "I'll do it", "I'll ask IT today".
- Include small items too: giving someone access, sending a draft, checking figures, writing rules,
  calling someone, submitting something for approval.
- One action per Proposed Action: split "write the tests and update the docs" into two Proposed Actions.
- NOT a Proposed Action: items that were cancelled or dropped ("we won't do X anymore" - record it as a decision),
  ideas nobody committed to, and decisions.

Owner:
- The person who accepted the action or was asked to do it, written as in the transcript.
- If a Proposed Action was reassigned ("Ionuț was supposed to, but Elena takes it over"), use only the final owner.
- If the speaker commits themself ("I'll do it", "I'll send it") and their name is not stated, owner=null.
  Never pick a person who is only mentioned in the sentence.
- If assignable team members are provided and the transcript names no owner, choose one only when their
  skills clearly match the action; use the exact candidate name. Otherwise owner=null.

Due date:
- Resolve "today", "tomorrow", weekday names ("by Monday", "on Thursday") and "next week" with the
  calendar above. "By Monday" means that Monday.
- If no deadline is stated, due_date=null. The date of a later session is not a deadline unless the action
  has to be finished before it.

Rules:
- Response language: English.
- Do not invent information that is not present in the transcript.
- Normalize dates to YYYY-MM-DD.
- Remove duplicates. Keep actions separate from decisions and risks.
- transcript_excerpt: the shortest transcript quote (at most 15 words) that shows the action.

Return ONLY valid JSON, with no extra text, using this schema:

{
  "session_summary": "maximum 4 sentences",
  "proposed_actions": [
    {
      "id": "A1",
      "title": "infinitive verb + clear objective",
      "description": "one short sentence",
      "owner": "person name or null",
      "due_date": "YYYY-MM-DD or null",
      "priority": "high|medium|low",
      "transcript_excerpt": "short quote from the transcript"
    }
  ],
  "decisions": ["decisions made in the session"],
  "risks": ["identified blockers or risks"],
  "follow_up_questions": ["questions needed for actions with unclear owner/deadline"]
}

Transcript:
{{TRANSCRIPT}}
""".strip()


_TEXT = {"type": "string", "maxLength": 400}
_NULLABLE = {"type": ["string", "null"], "maxLength": 200}

# Decoding constraint for PROPOSED_ACTION_PROMPT. Plain JSON mode lets a model answer "{}" (Qwen3 did, on
# whole sessions); with required keys and a non-empty summary that is no longer a valid answer.
PROPOSED_ACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "session_summary": {"type": "string", "minLength": 10, "maxLength": 1000},
        "proposed_actions": {
            "type": "array",
            "maxItems": 40,
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "maxLength": 10},
                    "title": {"type": "string", "minLength": 3, "maxLength": 200},
                    "description": _TEXT,
                    "owner": _NULLABLE,
                    "due_date": {"type": ["string", "null"], "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                    "priority": {"enum": ["high", "medium", "low"]},
                    "transcript_excerpt": _TEXT,
                },
                "required": ["id", "title", "description", "owner", "due_date", "priority", "transcript_excerpt"],
                "additionalProperties": False,
            },
        },
        "decisions": {"type": "array", "items": _TEXT, "maxItems": 20},
        "risks": {"type": "array", "items": _TEXT, "maxItems": 20},
        "follow_up_questions": {"type": "array", "items": _TEXT, "maxItems": 20},
    },
    "required": ["session_summary", "proposed_actions", "decisions", "risks", "follow_up_questions"],
    "additionalProperties": False,
}

GAP_INSTRUCTION = """
SECOND PASS. These Proposed Actions were already extracted from this transcript:
{{EXISTING}}

Find only the Proposed Actions that are MISSING from that list, following the same rules. Look especially
for small ones: giving access, sending a draft, checking or reviewing something, calling someone,
submitting something, writing rules or documentation. Do not repeat or reword items already listed.
If nothing is missing, return an empty "proposed_actions" list.
""".strip()

GAP_SCHEMA = {
    "type": "object",
    "properties": {"proposed_actions": PROPOSED_ACTION_SCHEMA["properties"]["proposed_actions"]},
    "required": ["proposed_actions"],
    "additionalProperties": False,
}

SUMMARY_PROMPT = """
Write the summary of one session in English, at most 4 sentences, from the summaries of its consecutive
parts and its decisions below. Use only this information.

Part summaries:
{{SUMMARIES}}

Decisions:
{{DECISIONS}}

Return ONLY valid JSON: {"session_summary": "..."}
""".strip()

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"session_summary": PROPOSED_ACTION_SCHEMA["properties"]["session_summary"]},
    "required": ["session_summary"],
    "additionalProperties": False,
}

class DeepSeekPromptBuilder:
    def __init__(
        self,
        template: str = PROPOSED_ACTION_PROMPT,
        assignee_candidates: list[dict] | None = None,
        session_date: date | None = None,
    ) -> None:
        self._template = template
        self._assignee_candidates = assignee_candidates or []
        self._session_date = session_date

    def build(self, previous_json: str, new_chunk: str) -> str:
        base_prompt = f"{self._date_context()}\n\n" + self._template.replace(
            "{{TRANSCRIPT}}", new_chunk.strip()
        )
        assignee_context = self._format_assignee_context()
        if assignee_context:
            base_prompt = f"{assignee_context}\n\n{base_prompt}"

        include_previous = (
            os.getenv("LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT", "1").strip().lower()
            not in {"0", "false", "no", "off"}
        )
        if not include_previous or not previous_json.strip():
            return base_prompt

        return (
            "You already produced a partial JSON result from earlier transcript chunks. "
            "Update that JSON using the new transcript chunk below, keep valid prior data, "
            "and deduplicate actions. Return ONLY valid JSON.\n\n"
            f"Previous JSON:\n{self._compact_json(previous_json)}\n\n"
            f"{base_prompt}"
        )

    def build_gap(self, chunk: str, existing_actions: list[dict]) -> str:
        lines = [
            f"- {t.get('title')} (owner: {t.get('owner') or '-'}, due: {t.get('due_date') or '-'})"
            for t in existing_actions
            if isinstance(t, dict) and t.get("title")
        ]
        return self.build("", chunk) + "\n\n" + GAP_INSTRUCTION.replace(
            "{{EXISTING}}", "\n".join(lines) or "(none)"
        )

    def _date_context(self) -> str:
        """The Session date plus a calendar of the coming weeks. Small models get the year of
        "Friday, 13 November" wrong without the date and the day of "by Monday" wrong without
        the calendar; a lookup table is far more reliable than date arithmetic.
        Session date: the constructor argument, else LLM_SESSION_DATE, else today."""
        raw = os.getenv("LLM_SESSION_DATE", "").strip()
        today = self._session_date or (date.fromisoformat(raw) if raw else date.today())
        labels = {0: " (today)", 1: " (tomorrow)"}
        days = [today + timedelta(days=offset) for offset in range(42)]
        calendar = "\n".join(
            "  " + ", ".join(
                f"{d.strftime('%a')} {d.isoformat()}{labels.get((d - today).days, '')}"
                for d in days[week:week + 7]
            )
            for week in range(0, len(days), 7)
        )
        return (
            f"The session took place today, {today.isoformat()} ({today.strftime('%A')}).\n"
            f"Calendar for resolving dates:\n{calendar}"
        )

    def _compact_json(self, raw_json: str) -> str:
        text = raw_json.strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
            return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except json.JSONDecodeError:
            return text

    def _format_assignee_context(self) -> str:
        lines: list[str] = []
        for candidate in self._assignee_candidates[:25]:
            if not isinstance(candidate, dict):
                continue

            name = str(candidate.get("name") or "").strip()
            if not name:
                continue

            raw_skills = candidate.get("skills")
            skills: list[str] = []
            if isinstance(raw_skills, list):
                for raw_skill in raw_skills[:12]:
                    skill = str(raw_skill or "").strip()
                    if skill:
                        skills.append(skill)

            skills_text = ", ".join(skills) if skills else "no listed skills"
            lines.append(f"- {name}: {skills_text}")

        if not lines:
            return ""

        return (
            "Assignable team members. Use these candidates for owner selection "
            "when their skills clearly match a Proposed Action:\n" + "\n".join(lines)
        )


ACTION_ASSIGNMENT_PROMPT = """
You are a senior Project Manager. Your only job is to assign each open action to the single
team member whose skills best fit it.

You receive:
1) Team members and their skills (extracted from the company database).
2) Open Proposed Actions (id, title, description, Transcript excerpt, current owner if any).

Rules:
- For each action, choose exactly ONE owner whose skills best match the work required.
- Choose ONLY from the provided team members. Reuse their exact "id" and "name".
- If a Proposed Action already names an explicit owner that matches a provided member, keep that member.
- If no member's skills are a reasonable match for a Proposed Action, set assigned_user_id=null and owner=null.
- Decide on skill relevance to the action content, never on list order.
- Do not invent members, ids, names, or skills.
- Provide a confidence between 0 and 1 and a short rationale naming the matching skills.

Team members:
{{CANDIDATES}}

Open Proposed Actions:
{{PROPOSED_ACTIONS}}

Return ONLY valid JSON, with no extra text, using this schema:

{
  "assignments": [
    {
      "action_id": "A1",
      "assigned_user_id": "member id or null",
      "owner": "member name or null",
      "confidence": 0.0,
      "rationale": "short reason referencing the matching skills"
    }
  ]
}
""".strip()


class AssignmentPromptBuilder:
    def __init__(self, template: str = ACTION_ASSIGNMENT_PROMPT) -> None:
        self._template = template

    def build(self, actions: list[dict], candidates: list[dict]) -> str:
        return (
            self._template
            .replace("{{CANDIDATES}}", self._format_candidates(candidates))
            .replace("{{PROPOSED_ACTIONS}}", self._format_actions(actions))
        )

    def _format_candidates(self, candidates: list[dict]) -> str:
        lines: list[str] = []
        for candidate in candidates[:50]:
            if not isinstance(candidate, dict):
                continue
            name = str(candidate.get("name") or "").strip()
            member_id = str(candidate.get("id") or "").strip()
            if not name or not member_id:
                continue
            raw_skills = candidate.get("skills")
            skills = [str(s or "").strip() for s in raw_skills] if isinstance(raw_skills, list) else []
            skills = [s for s in skills if s]
            skills_text = ", ".join(skills) if skills else "no listed skills"
            lines.append(f"- id={member_id} | {name} | skills: {skills_text}")
        return "\n".join(lines) if lines else "(no team members provided)"

    def _format_actions(self, actions: list[dict]) -> str:
        slim: list[dict] = []
        for action in actions:
            if not isinstance(action, dict):
                continue
            slim.append(
                {
                    "id": str(action.get("id") or "").strip(),
                    "title": str(action.get("title") or "").strip(),
                    "description": str(action.get("description") or "").strip(),
                    "transcript_excerpt": str(action.get("transcript_excerpt") or "").strip(),
                    "current_owner": action.get("owner"),
                }
            )
        return json.dumps(slim, ensure_ascii=False, indent=2)
