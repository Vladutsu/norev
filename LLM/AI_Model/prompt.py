import json
import os


PM_TASK_PROMPT = """
You are a senior Project Manager with a rigorous, execution-focused mindset. You receive a meeting transcript and must extract actionable tasks.

Objective:
1) Identify only concrete actions (not vague ideas).
2) Convert each action into a clear, executable task.
3) Do not invent information that is not present in the transcript.

Rules:
- Response language: English.
- If the transcript explicitly names an owner, use that person.
- If the owner is not explicit and assignable team members are provided, choose an owner only when one member's skills clearly match the task.
- When choosing from assignable team members, use the exact candidate name.
- If there is no clear skill match or the match is ambiguous, set owner=null.
- If the deadline is not explicit, set due_date=null.
- Normalize dates to YYYY-MM-DD when available.
- Be exhaustive: capture every concrete action item in the transcript.
- Split combined statements into separate tasks when they refer to different actions.
- Do not merge different actions into one broad task.
- Remove duplicates.
- Separate tasks from decisions and risks.
- Add a confidence score between 0 and 1.
- For each task, include a short evidence quote from the transcript.

Return ONLY valid JSON, with no extra text, using this schema:

{
  "meeting_summary": "maximum 5 sentences",
  "tasks": [
    {
      "id": "T1",
      "title": "infinitive verb + clear objective",
      "description": "specific expected deliverable",
      "owner": "person name or null",
      "due_date": "YYYY-MM-DD or null",
      "priority": "high|medium|low",
      "status": "open",
      "dependencies": ["task id or description"],
      "confidence": 0.0,
      "evidence_quote": "short relevant quote from transcript"
    }
  ],
  "decisions": [
    "decisions made in the meeting"
  ],
  "risks": [
    "identified blockers or risks"
  ],
  "follow_up_questions": [
    "questions needed for tasks with unclear owner/deadline"
  ]
}

Transcript:
{{TRANSCRIPT}}
""".strip()


class DeepSeekPromptBuilder:
    def __init__(
        self,
        template: str = PM_TASK_PROMPT,
        assignee_candidates: list[dict] | None = None,
    ) -> None:
        self._template = template
        self._assignee_candidates = assignee_candidates or []

    def build(self, previous_json: str, new_chunk: str) -> str:
        base_prompt = self._template.replace("{{TRANSCRIPT}}", new_chunk.strip())
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
