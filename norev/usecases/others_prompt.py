"""Others Domain prompt (#21): the User's context -> instructions generated locally -> a prompt the
User sees, edits and confirms.

The model writes only the instructions; they fill {{user_instructions}} in the Domain's own
template, which keeps the fixed rules, placeholders, Action Type, schema and Workflow. Everything
after that is the usual Domain stage (ask_domain/apply_answer), so the answer is validated as for
any Domain and every Proposed Action still waits for the User's approval (ADR 0002).

#83: the User fills three boxes instead of one: the kind of conversation (required), what counts as an
action and the result they want. The first two fill instructions.md; the third goes, as written, into
the completion template (usecases/others_completion.py), never through the instructions model.
Every box is data: each template is filled in one pass, so placeholder text in a box stays as typed.
"""
import dataclasses
import json
import re

from norev.core.models import Domain, OthersPrompt, ValidationError
from norev.core.ports import LlmClient

MAX_CONTEXT = 2000  # characters of the User's three boxes together, so the 8B model's prompt does not grow
MAX_INSTRUCTIONS = 4000  # characters of the generated or edited instructions
SLOT = "{{user_instructions}}"
COMPLETION_SLOT = "{{user_completion}}"


def fill(template: str, values: dict[str, str]) -> str:
    """Every `{{name}}` of `values` in one pass: text a value brings in is never replaced again."""
    names = "|".join(map(re.escape, values))
    return re.sub(r"\{\{(" + names + r")\}\}", lambda m: values[m[1]], template)


def check_context(context: object) -> str:
    """The context as sent to the model; empty or longer than MAX_CONTEXT raises ValidationError."""
    text = context.strip() if isinstance(context, str) else ""
    if not text:
        raise ValidationError("empty", "context")
    if len(text) > MAX_CONTEXT:
        raise ValidationError("too_long", "context")
    return text


def check_boxes(conversation: object, focus: object = "", completion: object = "") -> tuple[str, str, str]:
    """The three boxes as used: the first is required; together at most MAX_CONTEXT characters."""
    boxes = (check_context(conversation), *(v.strip() if isinstance(v, str) else "" for v in (focus, completion)))
    if sum(map(len, boxes)) > MAX_CONTEXT:
        raise ValidationError("too_long", "context")
    return boxes


def check_instructions(instructions: object) -> str:
    """Generated or User-edited instructions; they cannot carry placeholders into the template."""
    text = instructions.strip() if isinstance(instructions, str) else ""
    if not text:
        raise ValidationError("empty", "instructions")
    if len(text) > MAX_INSTRUCTIONS:
        raise ValidationError("too_long", "instructions")
    if "{{" in text or "}}" in text:
        raise ValidationError("placeholder", "instructions")
    return text


def generate_instructions(llm: LlmClient, template: str, schema: dict, context: object, *,
                          focus: object = "", model: str | None = None) -> str:
    """One local model call with only the User's first two boxes (no audio, Contact or other Session).

    Anything but valid instructions raises and returns nothing, so the caller keeps the previous
    context and prompt; the User retries explicitly.
    """
    conversation, focus, _ = check_boxes(context, focus)
    raw = llm.generate_json(fill(template, {"conversation": conversation, "focus": focus}), model=model, json_schema=schema)
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:  # not re-raised: the error would carry the raw answer
        answer = None
    if not isinstance(answer, dict):
        raise ValueError("instructions answer is not a JSON object")
    return check_instructions(answer.get("instructions"))


def render_prompt(domain: Domain, instructions: object) -> str:
    """The full Domain stage prompt the User sees, with the other placeholders still unfilled."""
    if domain.prompt_template.count(SLOT) != 1:
        raise ValueError(f"Domain {domain.name!r} has no single {SLOT} slot")
    return domain.prompt_template.replace(SLOT, check_instructions(instructions))


def render_completion(template: str, completion: object) -> str:
    """The completion prompt the User sees: their third box in its slot, the Transcript still unfilled."""
    if template.count(COMPLETION_SLOT) != 1:
        raise ValueError(f"the completion template has no single {COMPLETION_SLOT} slot")
    return fill(template, {"user_completion": completion if isinstance(completion, str) else ""})


def confirm(domain: Domain, context: object, instructions: object, *, focus: object = "", completion: object = "",
            completion_template: str = "") -> OthersPrompt:
    """The User's confirmation of exactly these boxes, these instructions and the prompts they make."""
    text = check_instructions(instructions)
    conversation, focus, completion = check_boxes(context, focus, completion)
    completion_prompt = render_completion(completion_template, completion) if completion else ""
    return OthersPrompt(conversation, text, render_prompt(domain, text), focus, completion, completion_prompt)


def others_domain(domain: Domain, prompt: OthersPrompt) -> Domain:
    """The Domain the Domain stage runs with: same Action Types, schema and Workflow, confirmed prompt.

    If the Domain's template changed since the User confirmed, the prompt would no longer be the one
    they saw: refused, so they confirm again instead of running a prompt they never approved.
    """
    if render_prompt(domain, prompt.instructions) != prompt.prompt:
        raise ValueError("the Others template changed since the User confirmed the prompt; confirm it again")
    return dataclasses.replace(domain, prompt_template=prompt.prompt)
