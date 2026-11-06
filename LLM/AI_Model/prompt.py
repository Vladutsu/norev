import json
import os
from datetime import date, timedelta


PM_TASK_PROMPT = """
You are a senior Project Manager with a rigorous, execution-focused mindset. You receive (part of) a
meeting transcript and must extract every action item. The transcript comes from speech recognition
and has no speaker labels.

What is a task:
- Any commitment or request to do something: "X, can you ...?" followed by agreement, "please send ...",
  "I'll do it", "I'll ask IT today".
- Include small items too: giving someone access, sending a draft, checking figures, writing rules,
  calling someone, submitting something for approval.
- One action per task: split "write the tests and update the docs" into two tasks.
- NOT a task: items that were cancelled or dropped ("we won't do X anymore" - record it as a decision),
  ideas nobody committed to, and decisions.

Owner:
- The person who accepted the task or was asked to do it, written as in the transcript.
- If a task was reassigned ("Ionuț was supposed to, but Elena takes it over"), use only the final owner.
- If the speaker commits themself ("I'll do it", "I'll send it") and their name is not stated, owner=null.
  Never pick a person who is only mentioned in the sentence.
- If assignable team members are provided and the transcript names no owner, choose one only when their
  skills clearly match the task; use the exact candidate name. Otherwise owner=null.

Due date:
- Resolve "today", "tomorrow", weekday names ("by Monday", "on Thursday") and "next week" with the
  calendar above. "By Monday" means that Monday.
- If no deadline is stated, due_date=null. The date of a later meeting is not a deadline unless the task
  has to be finished before it.

Rules:
- Response language: English.
- Do not invent information that is not present in the transcript.
- Normalize dates to YYYY-MM-DD.
- Remove duplicates. Keep tasks separate from decisions and risks.
- evidence_quote: the shortest transcript quote (at most 15 words) that shows the task.

Return ONLY valid JSON, with no extra text, using this schema:

{
  "meeting_summary": "maximum 4 sentences",
  "tasks": [
    {
      "id": "T1",
      "title": "infinitive verb + clear objective",
      "description": "one short sentence",
      "owner": "person name or null",
      "due_date": "YYYY-MM-DD or null",
      "priority": "high|medium|low",
      "evidence_quote": "short quote from the transcript"
    }
  ],
  "decisions": ["decisions made in the meeting"],
  "risks": ["identified blockers or risks"],
  "follow_up_questions": ["questions needed for tasks with unclear owner/deadline"]
}

Transcript:
{{TRANSCRIPT}}
""".strip()


_TEXT = {"type": "string", "maxLength": 400}
_NULLABLE = {"type": ["string", "null"], "maxLength": 200}

# Decoding constraint for PM_TASK_PROMPT. Plain JSON mode lets a model answer "{}" (Qwen3 did, on
# whole meetings); with required keys and a non-empty summary that is no longer a valid answer.
PM_TASK_SCHEMA = {
    "type": "object",
    "properties": {
        "meeting_summary": {"type": "string", "minLength": 10, "maxLength": 1000},
        "tasks": {
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
                    "evidence_quote": _TEXT,
                },
                "required": ["id", "title", "description", "owner", "due_date", "priority", "evidence_quote"],
                "additionalProperties": False,
            },
        },
        "decisions": {"type": "array", "items": _TEXT, "maxItems": 20},
        "risks": {"type": "array", "items": _TEXT, "maxItems": 20},
        "follow_up_questions": {"type": "array", "items": _TEXT, "maxItems": 20},
    },
    "required": ["meeting_summary", "tasks", "decisions", "risks", "follow_up_questions"],
    "additionalProperties": False,
}

GAP_INSTRUCTION = """
SECOND PASS. These action items were already extracted from this transcript:
{{EXISTING}}

Find only the action items that are MISSING from that list, following the same rules. Look especially
for small ones: giving access, sending a draft, checking or reviewing something, calling someone,
submitting something, writing rules or documentation. Do not repeat or reword items already listed.
If nothing is missing, return an empty "tasks" list.
""".strip()

GAP_SCHEMA = {
    "type": "object",
    "properties": {"tasks": PM_TASK_SCHEMA["properties"]["tasks"]},
    "required": ["tasks"],
    "additionalProperties": False,
}

SUMMARY_PROMPT = """
Write the summary of one meeting in English, at most 4 sentences, from the summaries of its consecutive
parts and its decisions below. Use only this information.

Part summaries:
{{SUMMARIES}}

Decisions:
{{DECISIONS}}

Return ONLY valid JSON: {"meeting_summary": "..."}
""".strip()

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"meeting_summary": PM_TASK_SCHEMA["properties"]["meeting_summary"]},
    "required": ["meeting_summary"],
    "additionalProperties": False,
}

class DeepSeekPromptBuilder:
    def __init__(
        self,
        template: str = PM_TASK_PROMPT,
        assignee_candidates: list[dict] | None = None,
    ) -> None:
        self._template = template
        self._assignee_candidates = assignee_candidates or []

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
            "and deduplicate tasks. Return ONLY valid JSON.\n\n"
            f"Previous JSON:\n{self._compact_json(previous_json)}\n\n"
            f"{base_prompt}"
        )

    def build_gap(self, chunk: str, existing_tasks: list[dict]) -> str:
        lines = [
            f"- {t.get('title')} (owner: {t.get('owner') or '-'}, due: {t.get('due_date') or '-'})"
            for t in existing_tasks
            if isinstance(t, dict) and t.get("title")
        ]
        return self.build("", chunk) + "\n\n" + GAP_INSTRUCTION.replace(
            "{{EXISTING}}", "\n".join(lines) or "(none)"
        )

    @staticmethod
    def _date_context() -> str:
        """Today's date plus a calendar of the coming weeks. Small models get the year of
        "Friday, 13 November" wrong without the date and the day of "by Monday" wrong without
        the calendar; a lookup table is far more reliable than date arithmetic."""
        raw = os.getenv("LLM_MEETING_DATE", "").strip()
        today = date.fromisoformat(raw) if raw else date.today()
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
            f"The meeting took place today, {today.isoformat()} ({today.strftime('%A')}).\n"
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
            "when their skills clearly match a task:\n" + "\n".join(lines)
        )


TASK_ASSIGNMENT_PROMPT = """
You are a senior Project Manager. Your only job is to assign each open task to the single
team member whose skills best fit it.

You receive:
1) Team members and their skills (extracted from the company database).
2) Open tasks (id, title, description, evidence quote, current owner if any).

Rules:
- For each task, choose exactly ONE owner whose skills best match the work required.
- Choose ONLY from the provided team members. Reuse their exact "id" and "name".
- If a task already names an explicit owner that matches a provided member, keep that member.
- If no member's skills are a reasonable match for a task, set assigned_user_id=null and owner=null.
- Decide on skill relevance to the task content, never on list order.
- Do not invent members, ids, names, or skills.
- Provide a confidence between 0 and 1 and a short rationale naming the matching skills.

Team members:
{{CANDIDATES}}

Open tasks:
{{TASKS}}

Return ONLY valid JSON, with no extra text, using this schema:

{
  "assignments": [
    {
      "task_id": "T1",
      "assigned_user_id": "member id or null",
      "owner": "member name or null",
      "confidence": 0.0,
      "rationale": "short reason referencing the matching skills"
    }
  ]
}
""".strip()


class AssignmentPromptBuilder:
    def __init__(self, template: str = TASK_ASSIGNMENT_PROMPT) -> None:
        self._template = template

    def build(self, tasks: list[dict], candidates: list[dict]) -> str:
        return (
            self._template
            .replace("{{CANDIDATES}}", self._format_candidates(candidates))
            .replace("{{TASKS}}", self._format_tasks(tasks))
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

    def _format_tasks(self, tasks: list[dict]) -> str:
        slim: list[dict] = []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            slim.append(
                {
                    "id": str(task.get("id") or "").strip(),
                    "title": str(task.get("title") or "").strip(),
                    "description": str(task.get("description") or "").strip(),
                    "evidence_quote": str(task.get("evidence_quote") or "").strip(),
                    "current_owner": task.get("owner"),
                }
            )
        return json.dumps(slim, ensure_ascii=False, indent=2)
