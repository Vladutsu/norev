import json
from pathlib import Path
from typing import Any

from LLM.AI_Model.client import LLMClient, build_llm_client
from LLM.AI_Model.prompt import AssignmentPromptBuilder
from LLM.AI_Model.settings import DeepSeekSettings


class ProposedActionAssigner:
    """Second DeepSeek pass.

    Takes the actions already extracted into the session JSON plus every team
    member's skills (pulled from the database) and asks the model to assign each
    action to the best-fit member, then writes the chosen owner back into the JSON.
    """

    def __init__(
        self,
        settings: DeepSeekSettings | None = None,
        client: LLMClient | None = None,
        prompt_builder: AssignmentPromptBuilder | None = None,
        assignee_candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        self._settings = settings or DeepSeekSettings.from_environment()
        self._client = client or build_llm_client(self._settings)
        self._prompt_builder = prompt_builder or AssignmentPromptBuilder()
        self._candidates = self._normalize_candidates(assignee_candidates or [])
        self._id_index = {candidate["id"]: candidate for candidate in self._candidates}
        self._name_index = {
            self._normalize_text(candidate["name"]).lower(): candidate
            for candidate in self._candidates
        }

    @property
    def has_candidates(self) -> bool:
        return bool(self._candidates)

    def assign_file(self, output_path: Path) -> bool:
        """Reassign owners in-place. Returns True when the JSON was modified."""
        if not self._candidates:
            return False

        path = Path(output_path)
        if not path.exists():
            return False

        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return False

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False

        if not isinstance(payload, dict):
            return False

        actions = payload.get("proposed_actions")
        if not isinstance(actions, list) or not actions:
            return False

        assignment_map = self._request_assignments(actions)
        if not assignment_map:
            return False

        if not self._apply_assignments(actions, assignment_map):
            return False

        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return True

    def _request_assignments(self, actions: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
        prompt = self._prompt_builder.build(actions, self._candidates)
        try:
            generated = self._client.generate_json(prompt, model=self._settings.model)
            payload = json.loads(generated)
        except Exception:
            return {}

        assignments = payload.get("assignments") if isinstance(payload, dict) else None
        if not isinstance(assignments, list):
            return {}

        resolved: dict[str, dict[str, Any] | None] = {}
        for item in assignments:
            if not isinstance(item, dict):
                continue
            action_id = self._normalize_text(item.get("action_id"))
            if not action_id:
                continue
            resolved[action_id] = self._resolve_candidate(item)
        return resolved

    def _resolve_candidate(self, item: dict[str, Any]) -> dict[str, Any] | None:
        member_id = str(item.get("assigned_user_id") or "").strip()
        if member_id and member_id in self._id_index:
            return self._id_index[member_id]

        owner = self._normalize_text(item.get("owner")).lower()
        if owner and owner in self._name_index:
            return self._name_index[owner]

        return None

    def _apply_assignments(
        self,
        actions: list[dict[str, Any]],
        assignment_map: dict[str, dict[str, Any] | None],
    ) -> bool:
        changed = False
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_id = self._normalize_text(action.get("id"))
            if not action_id or action_id not in assignment_map:
                continue

            candidate = assignment_map[action_id]
            if candidate is None:
                continue

            if (
                action.get("owner") == candidate["name"]
                and action.get("assigned_user_id") == candidate["id"]
            ):
                continue

            action["owner"] = candidate["name"]
            action["assigned_user_id"] = candidate["id"]
            action["assignment_reason"] = "llm-skill-match"
            changed = True
        return changed

    def _normalize_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            member_id = str(candidate.get("id") or "").strip()
            name = str(candidate.get("name") or "").strip()
            if not member_id or not name or member_id in seen_ids:
                continue

            raw_skills = candidate.get("skills")
            skills: list[str] = []
            if isinstance(raw_skills, list):
                for raw_skill in raw_skills:
                    skill = str(raw_skill or "").strip()
                    if skill:
                        skills.append(skill)

            seen_ids.add(member_id)
            normalized.append({"id": member_id, "name": name, "skills": skills})
        return normalized

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.strip()
