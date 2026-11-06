import json
import os
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any

from LLM.AI_Model.client import LLMClient, OllamaTimeoutError, build_llm_client
from LLM.AI_Model.prompt import (
    GAP_SCHEMA,
    PM_TASK_SCHEMA,
    SUMMARY_PROMPT,
    SUMMARY_SCHEMA,
    DeepSeekPromptBuilder,
)
from LLM.AI_Model.settings import DeepSeekSettings

# Words that do not tell two tasks apart ("Fix the iOS issue" == "Fix iOS")
_TITLE_STOPWORDS = {
    "the", "and", "for", "with", "from", "into", "onto", "about", "all", "any", "new",
    "issue", "issues", "problem", "problems", "task", "tasks", "item", "items",
}
# Extra filler in decisions/risks ("Decide to...", "Risk of ... could lead to ...")
_STATEMENT_STOPWORDS = _TITLE_STOPWORDS | {
    "decide", "decided", "decision", "risk", "main", "could", "would", "may", "might", "lead",
    "leading", "result", "only", "after", "until", "not", "will", "that", "this", "than", "which",
    "need", "needed", "there", "their", "being",
}
# Owners that mean "whoever was speaking": compatible with any owner when de-duplicating
_ANONYMOUS_OWNERS = {
    "", "-", "i", "me", "we", "us", "speaker", "the speaker", "organizer", "moderator",
    "none", "null", "n/a", "unknown", "tbd",
}


class RealtimeTaskExtractor:
    def __init__(
        self,
        output_path: Path,
        settings: DeepSeekSettings | None = None,
        client: LLMClient | None = None,
        prompt_builder: DeepSeekPromptBuilder | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        assignee_candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        self.output_path = output_path
        base_settings = settings or DeepSeekSettings.from_environment()
        self._settings = base_settings.with_overrides(
            provider=llm_provider,
            model=llm_model,
        )
        self._client = client or build_llm_client(self._settings)
        self._prompt_builder = prompt_builder or DeepSeekPromptBuilder(
            assignee_candidates=assignee_candidates,
        )

        self._queue: Queue[str | None] = Queue()
        self._thread = Thread(target=self._worker, daemon=True)
        self._latest_json = ""
        self._error: Exception | None = None
        self._closed = False
        self._thread.start()

    @property
    def llm_provider(self) -> str:
        return self._settings.provider

    @property
    def llm_model(self) -> str:
        return self._settings.model

    def push_segment(self, text: str) -> None:
        if self._closed:
            raise RuntimeError("Cannot push segment: extractor is already closed.")
        clean = text.strip()
        if clean:
            self._queue.put(clean)

    def close(self) -> str:
        if not self._closed:
            self._queue.put(None)
            self._thread.join()
            self._closed = True

        if self._error is not None:
            raise RuntimeError(f"Realtime task extraction failed: {self._error}") from self._error

        if self._latest_json.strip():
            self._persist(self._latest_json)
        return self._latest_json

    def extract_batch(self, segments: list[str], workers: int = 1) -> str:
        """Process a finished transcript: chunk it like the realtime worker, generate the chunks
        concurrently (llama-server --parallel batches them on the GPU), merge in transcript order.

        Concurrency needs chunk-local prompts; with LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT on, each
        chunk depends on the previous result, so this falls back to one chunk at a time.
        """
        chunks: list[str] = []
        buffer: list[str] = []
        char_count = 0
        for segment in (s.strip() for s in segments):
            if not segment:
                continue
            buffer.append(segment)
            char_count += len(segment)
            if char_count >= self._settings.chunk_char_threshold:
                chunks.append(" ".join(buffer))
                buffer, char_count = [], 0
        if buffer:
            chunks.append(" ".join(buffer))

        include_previous = os.getenv("LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT", "1").strip().lower() not in {
            "0", "false", "no", "off",
        }
        # The realtime worker thread is not used in batch mode
        self._queue.put(None)
        self._thread.join()
        self._closed = True

        if include_previous:
            for chunk in chunks:
                self._process_chunk(chunk)
        else:
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                results = list(pool.map(self._generate_chunk, chunks))
                summaries = [
                    s for s in (self._parse_json_object(r).get("meeting_summary") for rs in results for r in rs)
                    if isinstance(s, str) and s.strip()
                ]
                for chunk_results in results:
                    for generated_json in chunk_results:
                        self._latest_json = self._merge_results(self._latest_json, generated_json)

                if os.getenv("LLM_SECOND_PASS", "0").strip().lower() not in {"0", "false", "no", "off"}:
                    existing = self._parse_json_object(self._latest_json).get("tasks", [])
                    for found in pool.map(lambda c: self._find_missed_tasks(c, existing), chunks):
                        self._latest_json = self._merge_results(self._latest_json, found)

            if len(summaries) > 1:
                self._summarize(summaries)

        if self._latest_json.strip():
            self._persist(self._latest_json)
        return self._latest_json

    def _find_missed_tasks(self, chunk_text: str, existing_tasks: list[dict[str, Any]]) -> str:
        """Second pass: the model sees what was found and looks only for what is missing."""
        prompt = self._prompt_builder.build_gap(chunk_text, existing_tasks)
        try:
            raw = self._client.generate_json(prompt, model=self._settings.model, json_schema=GAP_SCHEMA)
        except Exception:
            return "{}"  # the first pass already produced a result; a failed check adds nothing
        tasks = self._parse_json_object(raw).get("tasks", [])
        # Found only on a second look: more likely to have the wrong owner, so flag for review.
        # A task that duplicates a first-pass task merges into it and keeps that task's flag.
        for task in tasks if isinstance(tasks, list) else []:
            if isinstance(task, dict):
                task["needs_review"] = True
        return json.dumps({"tasks": tasks}, ensure_ascii=False)

    def _summarize(self, summaries: list[str]) -> None:
        """Each chunk summarises only its own part; merging keeps the last one. Rewrite it from all."""
        merged = self._parse_json_object(self._latest_json)
        decisions = merged.get("decisions", [])
        prompt = SUMMARY_PROMPT.replace(
            "{{SUMMARIES}}", "\n".join(f"{i}. {s}" for i, s in enumerate(summaries, start=1))
        ).replace("{{DECISIONS}}", "\n".join(f"- {d}" for d in decisions) or "-")
        try:
            raw = self._client.generate_json(prompt, model=self._settings.model, json_schema=SUMMARY_SCHEMA)
            summary = self._parse_json_object(raw).get("meeting_summary")
        except Exception:
            summary = None
        merged["meeting_summary"] = summary.strip() if isinstance(summary, str) and summary.strip() else " ".join(summaries)
        self._latest_json = json.dumps(merged, ensure_ascii=False, indent=2)

    def _generate_chunk(self, chunk_text: str) -> list[str]:
        """Chunk-local generation; splits the chunk and retries like _process_chunk."""
        prompt = self._prompt_builder.build("", chunk_text)
        try:
            return [self._client.generate_json(prompt, model=self._settings.model, json_schema=PM_TASK_SCHEMA)]
        except Exception as exc:
            if self._should_split_chunk(chunk_text, exc):
                left, right = self._split_chunk(chunk_text)
                if left and right:
                    return self._generate_chunk(left) + self._generate_chunk(right)
            raise

    def _persist(self, content: str) -> None:
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("w", encoding="utf-8") as handle:
            handle.write(content.strip() + "\n")

    def _flush_buffer(self, buffer: list[str]) -> None:
        chunk_text = " ".join(buffer).strip()
        if not chunk_text:
            return

        self._process_chunk(chunk_text)

    def _process_chunk(self, chunk_text: str) -> None:
        prompt = self._prompt_builder.build(self._latest_json, chunk_text)
        try:
            generated_json = self._client.generate_json(
                prompt, model=self._settings.model, json_schema=PM_TASK_SCHEMA
            )
            self._latest_json = self._merge_results(self._latest_json, generated_json)
            self._persist(self._latest_json)
        except Exception as exc:
            if self._should_split_chunk(chunk_text, exc):
                left, right = self._split_chunk(chunk_text)
                if left and right:
                    self._process_chunk(left)
                    self._process_chunk(right)
                    return
            raise

    def _should_split_chunk(self, chunk_text: str, exc: Exception) -> bool:
        if len(chunk_text) < self._settings.min_chunk_char_threshold * 2:
            return False
        if isinstance(exc, OllamaTimeoutError):
            return True
        message = str(exc).lower()
        retryable_markers = (
            "timed out",
            "timeout",
            "valid json",
            "context length",
            "context window",
            "non-empty 'response'",
        )
        return any(marker in message for marker in retryable_markers)

    def _split_chunk(self, chunk_text: str) -> tuple[str, str]:
        words = chunk_text.split()
        if len(words) < 2:
            return chunk_text, ""

        midpoint = len(words) // 2
        window = min(80, max(10, midpoint))
        split_index = midpoint
        for offset in range(window):
            for candidate in (midpoint + offset, midpoint - offset):
                if candidate <= 1 or candidate >= len(words) - 1:
                    continue
                if words[candidate - 1][-1:] in {".", "!", "?", ";"}:
                    split_index = candidate
                    left = " ".join(words[:split_index]).strip()
                    right = " ".join(words[split_index:]).strip()
                    if left and right:
                        return left, right

        left = " ".join(words[:midpoint]).strip()
        right = " ".join(words[midpoint:]).strip()
        return left, right

    def _merge_results(self, previous_json: str, current_json: str) -> str:
        previous_obj = self._parse_json_object(previous_json)
        current_obj = self._parse_json_object(current_json)

        if not previous_obj:
            merged = self._normalize_result_shape(current_obj)
            return json.dumps(merged, ensure_ascii=False, indent=2)

        merged = self._normalize_result_shape(previous_obj)
        current_normalized = self._normalize_result_shape(current_obj)

        current_summary = current_normalized.get("meeting_summary")
        if isinstance(current_summary, str) and current_summary.strip():
            merged["meeting_summary"] = current_summary.strip()

        merged["tasks"] = self._merge_tasks(
            merged.get("tasks", []),
            current_normalized.get("tasks", []),
        )
        merged["decisions"] = self._merge_text_lists(
            merged.get("decisions", []),
            current_normalized.get("decisions", []),
        )
        merged["risks"] = self._merge_text_lists(
            merged.get("risks", []),
            current_normalized.get("risks", []),
        )
        merged["follow_up_questions"] = self._merge_text_lists(
            merged.get("follow_up_questions", []),
            current_normalized.get("follow_up_questions", []),
        )

        return json.dumps(merged, ensure_ascii=False, indent=2)

    def _parse_json_object(self, raw_json: str) -> dict[str, Any]:
        text = raw_json.strip()
        if not text:
            return {}
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise RuntimeError("DeepSeek response must be a JSON object.")
        return parsed

    def _normalize_result_shape(self, payload: dict[str, Any]) -> dict[str, Any]:
        summary = payload.get("meeting_summary")
        meeting_summary = summary.strip() if isinstance(summary, str) else ""

        tasks_raw = payload.get("tasks", [])
        tasks: list[dict[str, Any]] = []
        if isinstance(tasks_raw, list):
            for item in tasks_raw:
                normalized = self._normalize_task(item)
                if normalized is not None:
                    tasks.append(normalized)

        return {
            "meeting_summary": meeting_summary,
            "tasks": self._assign_task_ids(tasks),
            "decisions": self._normalize_text_list(payload.get("decisions", [])),
            "risks": self._normalize_text_list(payload.get("risks", [])),
            "follow_up_questions": self._normalize_text_list(
                payload.get("follow_up_questions", []),
            ),
        }

    def _normalize_task(self, item: Any) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None

        title = self._normalize_text(item.get("title"))
        description = self._normalize_text(item.get("description"))
        if not title and not description:
            return None

        owner = self._normalize_nullable_text(item.get("owner"))
        if owner and owner.strip().lower() in _ANONYMOUS_OWNERS:
            owner = None  # "I"/"me": the speaker, whose name the transcript does not give
        due_date = self._normalize_nullable_text(item.get("due_date"))
        priority = self._normalize_text(item.get("priority")).lower() or "medium"
        if priority not in {"high", "medium", "low"}:
            priority = "medium"

        status = self._normalize_text(item.get("status")).lower() or "open"

        dependencies_raw = item.get("dependencies", [])
        dependencies: list[str] = []
        if isinstance(dependencies_raw, list):
            dependencies = self._normalize_text_list(dependencies_raw)

        confidence = item.get("confidence")
        confidence_value: float | None = None
        if isinstance(confidence, (float, int)):
            confidence_value = float(confidence)
        elif isinstance(confidence, str):
            try:
                confidence_value = float(confidence.strip())
            except ValueError:
                confidence_value = None
        if confidence_value is None:
            confidence_value = 0.0
        confidence_value = max(0.0, min(1.0, confidence_value))

        return {
            "id": self._normalize_text(item.get("id")),
            "title": title,
            "description": description,
            "owner": owner,
            "due_date": due_date,
            "priority": priority,
            "status": status,
            "dependencies": dependencies,
            "confidence": confidence_value,
            "evidence_quote": self._normalize_text(item.get("evidence_quote")),
            "needs_review": bool(item.get("needs_review")),
        }

    def _normalize_text(self, value: Any) -> str:
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.strip()

    def _normalize_nullable_text(self, value: Any) -> str | None:
        text = self._normalize_text(value)
        if not text:
            return None
        if text.lower() in {"null", "none", "n/a", "na", "unknown"}:
            return None
        return text

    def _normalize_text_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []

        deduped: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = self._normalize_text(item)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(text)
        return deduped

    def _merge_text_lists(self, left: Any, right: Any) -> list[str]:
        """Decisions/risks are reworded in every chunk too; drop near-duplicates, keep the first."""
        merged: list[str] = []
        kept_words: list[set[str]] = []
        for text in self._normalize_text_list(left) + self._normalize_text_list(right):
            # 5-letter stems: "integration"/"integrate", "approving"/"approval" compare equal
            words = {
                w[:5] for w in re.findall(r"[^\W_]+", text.lower())
                if len(w) > 2 and w not in _STATEMENT_STOPWORDS
            }
            if any(words and other and len(words & other) / len(words | other) >= 0.4 for other in kept_words):
                continue
            merged.append(text)
            kept_words.append(words)
        return merged

    def _merge_tasks(self, previous_tasks_raw: Any, current_tasks_raw: Any) -> list[dict[str, Any]]:
        previous_tasks = self._normalize_tasks_for_merge(previous_tasks_raw)
        current_tasks = self._normalize_tasks_for_merge(current_tasks_raw)

        merged = previous_tasks.copy()
        for current_task in current_tasks:
            idx = self._find_same_task(merged, current_task)
            if idx is None:
                merged.append(current_task)
            else:
                merged[idx] = self._merge_single_task(merged[idx], current_task)

        return self._assign_task_ids(merged)

    def _find_same_task(self, tasks: list[dict[str, Any]], candidate: dict[str, Any]) -> int | None:
        """The same action is often worded differently in each chunk (or each time a long meeting
        comes back to it): "Implement export of invoices in PDF" vs "Implement invoice export in PDF".
        Match on compatible owners plus overlapping title words rather than on exact text."""
        owner = self._owner_key(candidate)
        words = self._title_words(candidate)
        title = " ".join(sorted(words))
        best_idx, best_score = None, 0.0
        for idx, task in enumerate(tasks):
            other_owner = self._owner_key(task)
            if owner not in _ANONYMOUS_OWNERS and other_owner not in _ANONYMOUS_OWNERS and owner != other_owner:
                continue
            other_words = self._title_words(task)
            if not words or not other_words:
                continue
            jaccard = len(words & other_words) / len(words | other_words)
            ratio = SequenceMatcher(None, title, " ".join(sorted(other_words))).ratio()
            score = max(jaccard, ratio - 0.25)
            if score >= 0.5 and score > best_score:
                best_idx, best_score = idx, score
        return best_idx

    def _owner_key(self, task: dict[str, Any]) -> str:
        """"Ionuț" and "Ionut" are the same person (speech recognition and translation drop diacritics)."""
        owner = unicodedata.normalize("NFKD", self._normalize_text(task.get("owner")).lower())
        return "".join(ch for ch in owner if not unicodedata.combining(ch))

    def _title_words(self, task: dict[str, Any]) -> set[str]:
        title = self._normalize_text(task.get("title")).lower()
        words = re.findall(r"[^\W_]+", title)
        # crude singular form so "invoices"/"invoice" and "tests"/"test" compare equal
        return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words
                if len(w) > 2 and w not in _TITLE_STOPWORDS}

    def _normalize_tasks_for_merge(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        normalized: list[dict[str, Any]] = []
        for item in value:
            task = self._normalize_task(item)
            if task is not None:
                normalized.append(task)
        return normalized

    def _merge_single_task(self, previous: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
        merged = previous.copy()
        for field in [
            "title",
            "description",
            "owner",
            "due_date",
            "priority",
            "status",
            "confidence",
            "evidence_quote",
        ]:
            value = current.get(field)
            if value in ("", None):
                continue
            merged[field] = value

        merged["dependencies"] = self._merge_text_lists(
            previous.get("dependencies", []),
            current.get("dependencies", []),
        )
        if not merged.get("id"):
            merged["id"] = current.get("id", "")
        return merged

    def _assign_task_ids(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assigned: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        numeric_ids = [
            int(task_id[1:])
            for task_id in (self._normalize_text(task.get("id")) for task in tasks)
            if task_id.startswith("T") and task_id[1:].isdigit()
        ]
        next_id = (max(numeric_ids) if numeric_ids else 0) + 1

        for task in tasks:
            normalized_task = task.copy()
            task_id = self._normalize_text(normalized_task.get("id"))
            if not task_id or task_id in used_ids:
                task_id = f"T{next_id}"
                next_id += 1
            normalized_task["id"] = task_id
            used_ids.add(task_id)
            assigned.append(normalized_task)

        return assigned

    def _worker(self) -> None:
        try:
            buffer: list[str] = []
            char_count = 0

            while True:
                item = self._queue.get()
                if item is None:
                    break

                buffer.append(item)
                char_count += len(item)
                if char_count >= self._settings.chunk_char_threshold:
                    self._flush_buffer(buffer)
                    buffer.clear()
                    char_count = 0

            if buffer:
                self._flush_buffer(buffer)
        except Exception as exc:
            self._error = exc
