import os
import subprocess
import sys
import time
from pathlib import Path

from LLM.AI_Model import deepseek
from LLM.Whisper.model_manager import WhisperModelManager
from LLM.Whisper.settings import WhisperSettings


class TranscriptionService:
    def __init__(self, model_manager: WhisperModelManager, settings: WhisperSettings) -> None:
        self._model_manager = model_manager
        self._settings = settings

    def transcribe_media_json(
        self,
        file_path: str,
        language: str,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        on_segment=None,
        skip_llm: bool = False,
        assignee_candidates: list[dict] | None = None,
    ) -> dict:
        total_started_at = time.perf_counter()
        proposed_actions_output_path = deepseek.output_path_for_input(file_path, self._settings.output_dir)
        llm_settings = self._resolve_llm_settings(
            llm_provider=llm_provider,
            llm_model=llm_model,
        )

        transcribe_started_at = time.perf_counter()
        out_segments, full_text, transcript_items_for_actions, info = (
            self._transcribe_segments_with_recovery(file_path, language, on_segment=on_segment)
        )
        transcription_elapsed = time.perf_counter() - transcribe_started_at

        if skip_llm:
            proposed_actions_error, proposed_actions_elapsed = None, 0.0
        else:
            proposed_actions_error, proposed_actions_elapsed = self._extract_proposed_actions(
                proposed_actions_output_path=proposed_actions_output_path,
                transcript_items=transcript_items_for_actions,
                llm_settings=llm_settings,
                assignee_candidates=assignee_candidates,
            )
        total_elapsed = time.perf_counter() - total_started_at

        return {
            "text": " ".join(full_text),
            "segments": out_segments,
            "detected_language": getattr(info, "language", None),
            "detected_language_probability": getattr(info, "language_probability", None),
            "runtime_device": self._model_manager.runtime_device,
            "runtime_compute_type": self._model_manager.runtime_compute_type,
            "model": self._settings.model_name,
            "input_file": file_path,
            "proposed_actions_output_file": str(proposed_actions_output_path),
            "proposed_actions_error": proposed_actions_error,
            "llm_provider": llm_settings.provider,
            "llm_model": llm_settings.model,
            "llm_generation_options": {
                "temperature": llm_settings.temperature,
                "request_timeout_seconds": llm_settings.request_timeout_seconds,
                "max_retries": llm_settings.max_retries,
                "retry_backoff_seconds": llm_settings.retry_backoff_seconds,
                "response_max_tokens": llm_settings.response_max_tokens,
                "keep_alive": llm_settings.keep_alive,
            },
            "transcription_elapsed_seconds": round(transcription_elapsed, 3),
            "proposed_actions_elapsed_seconds": round(proposed_actions_elapsed, 3),
            "total_elapsed_seconds": round(total_elapsed, 3),
        }

    def _transcribe_segments_with_recovery(
        self,
        file_path: str,
        language: str,
        on_segment=None,
    ) -> tuple[list[dict[str, float | str]], list[str], list[str], object]:
        retried_for_oom = False
        while True:
            try:
                segments, info = self._model_manager.transcribe(file_path, language, on_segment=on_segment)
                out_segments, full_text, transcript_items = self._collect_segments(segments)
                return out_segments, full_text, transcript_items, info
            except Exception as exc:
                if retried_for_oom or not self._looks_like_oom(exc):
                    raise
                retried_for_oom = True
                self._release_ollama_models_if_needed()
                self._model_manager.force_cpu_fallback(str(exc))

    @staticmethod
    def _collect_segments(
        segments: object,
    ) -> tuple[list[dict[str, float | str]], list[str], list[str]]:
        out_segments: list[dict[str, float | str]] = []
        full_text: list[str] = []
        transcript_items_for_actions: list[str] = []
        for seg in segments:
            txt = seg.text.strip()
            if not txt:
                continue
            full_text.append(txt)
            transcript_items_for_actions.append(txt)
            out_segment = {
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "text": txt,
            }
            if getattr(seg, "words", None):  # only with WHISPER_WORD_TIMESTAMPS=1
                out_segment["words"] = [
                    {"start": round(float(w.start), 3), "end": round(float(w.end), 3), "word": w.word}
                    for w in seg.words
                ]
            out_segments.append(out_segment)
        return out_segments, full_text, transcript_items_for_actions

    def _extract_proposed_actions(
        self,
        proposed_actions_output_path: Path,
        transcript_items: list[str],
        llm_settings: deepseek.DeepSeekSettings,
        assignee_candidates: list[dict] | None = None,
    ) -> tuple[str | None, float]:
        if not transcript_items:
            return None, 0.0

        started_at = time.perf_counter()
        try:
            extractor = deepseek.RealtimeActionExtractor(
                output_path=proposed_actions_output_path,
                settings=llm_settings,
                assignee_candidates=assignee_candidates,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started_at
            return f"Could not start DeepSeek action extractor: {exc}", elapsed

        try:
            for text in transcript_items:
                extractor.push_segment(text)
            extractor.close()
            self._assign_proposed_actions(
                proposed_actions_output_path=proposed_actions_output_path,
                llm_settings=llm_settings,
                assignee_candidates=assignee_candidates,
            )
            return None, time.perf_counter() - started_at
        except Exception as exc:
            return str(exc), time.perf_counter() - started_at

    def _assign_proposed_actions(
        self,
        proposed_actions_output_path: Path,
        llm_settings: deepseek.DeepSeekSettings,
        assignee_candidates: list[dict] | None,
    ) -> None:
        """Second DeepSeek pass: assign each extracted action to the best-fit team member.

        Best-effort. A failure here never discards the actions already extracted.
        """
        if os.getenv("LLM_AUTO_ASSIGN", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        if not assignee_candidates:
            return

        assign_model = os.getenv("LLM_ASSIGN_MODEL", "").strip()
        assign_settings = (
            llm_settings.with_overrides(model=assign_model) if assign_model else llm_settings
        )

        try:
            assigner = deepseek.ProposedActionAssigner(
                settings=assign_settings,
                assignee_candidates=assignee_candidates,
            )
            assigner.assign_file(proposed_actions_output_path)
        except Exception:
            # Assignment is optional polish; keep the extracted actions regardless.
            pass

    def list_llm_models(self, provider: str | None = None) -> list[str]:
        llm_settings = self._resolve_llm_settings(llm_provider=provider, llm_model=None)
        catalog = deepseek.ModelCatalogService(settings=llm_settings)
        return catalog.list_models(provider=provider)

    @staticmethod
    def _looks_like_oom(exc: Exception) -> bool:
        message = str(exc).lower()
        markers = ("out of memory", "memory allocation", "alloc_failed", "cuda")
        return any(marker in message for marker in markers)

    def _release_ollama_models_if_needed(self) -> None:
        requested_device = (self._settings.model_device or "").lower()
        if requested_device == "cpu":
            return

        enabled = os.getenv("WHISPER_RELEASE_OLLAMA_MODELS_BEFORE_TRANSCRIBE", "0").strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            return

        try:
            result = subprocess.run(
                ["ollama", "ps"],
                check=False,
                capture_output=True,
                text=True,
            )
        except Exception:
            return

        if result.returncode != 0:
            return

        model_names: list[str] = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if not parts:
                continue
            model_name = parts[0].strip()
            if model_name:
                model_names.append(model_name)

        for model_name in dict.fromkeys(model_names):
            subprocess.run(
                ["ollama", "stop", model_name],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _resolve_llm_settings(
        self,
        llm_provider: str | None,
        llm_model: str | None,
    ) -> deepseek.DeepSeekSettings:
        base = deepseek.DeepSeekSettings.from_environment()
        return base.with_overrides(provider=llm_provider, model=llm_model)
