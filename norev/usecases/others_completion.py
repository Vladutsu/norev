"""Others completion (#83): the result the User asked for in the third box, made from the Transcript.

One local model call after the Domain stage, with the completion template the User saw when they
confirmed. The result is only text shown in review: nothing is executed or sent from here, and an
invalid answer leaves the result missing (with a Suggestion) without touching the Proposed Actions.
"""
import json
from datetime import date

from norev.core.models import OthersPrompt, Suggestion, Transcript
from norev.core.ports import LlmClient
from norev.usecases.others_prompt import fill, render_completion

MAX_OUTPUT = 8000  # characters of the result
FAILED = Suggestion("Rezultatul nu a putut fi generat. Regenerează analiza ca să încerci din nou.")


def check_template(template: str, prompt: OthersPrompt) -> None:
    """If the template changed since the User confirmed, the prompt is not the one they saw: refused."""
    if prompt.completion and render_completion(template, prompt.completion) != prompt.completion_prompt:
        raise ValueError("the Others completion template changed since the User confirmed it; confirm it again")


def complete(llm: LlmClient, template: str, schema: dict, prompt: OthersPrompt, transcript: Transcript,
             session_date: date, *, model: str | None = None) -> str | None:
    """The result, or None when the model's answer is not a valid one. "" without a request: no model call."""
    if not prompt.completion:
        return ""
    check_template(template, prompt)
    values = {
        # one line per segment, as domain_stage.build_prompt: newlines in the text cannot forge lines
        "transcript": "\n".join(f"[{s.speaker_id or '?'}] {' '.join(s.text.split())}" for s in transcript.segments),
        "session_date": session_date.isoformat(),
        "user_completion": prompt.completion,
    }
    raw = llm.generate_json(fill(template, values), model=model, json_schema=schema)
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:
        return None
    output = answer.get("output") if isinstance(answer, dict) else None
    output = output.strip() if isinstance(output, str) else ""
    return output if 0 < len(output) <= MAX_OUTPUT else None
