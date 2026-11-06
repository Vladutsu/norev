"""Others Domain prompt (#21): the User's context -> instructions generated locally -> a prompt the
User sees, edits and confirms.

The model writes only the instructions; they fill {{user_instructions}} in the Domain's own
template, which keeps the fixed rules, placeholders, Action Type, schema and Workflow. Everything
after that is the usual Domain stage (ask_domain/apply_answer), so the answer is validated as for
any Domain and every Proposed Action still waits for the User's approval (ADR 0002).
"""
import dataclasses
import json

from norev.core.models import Domain, OthersPrompt, ValidationError
from norev.core.ports import LlmClient

MAX_CONTEXT = 2000  # characters of the User's context
MAX_INSTRUCTIONS = 4000  # characters of the generated or edited instructions
SLOT = "{{user_instructions}}"


def check_context(context: object) -> str:
    """The context as sent to the model; empty or longer than MAX_CONTEXT raises ValidationError."""
    text = context.strip() if isinstance(context, str) else ""
    if not text:
        raise ValidationError("empty", "context")
    if len(text) > MAX_CONTEXT:
        raise ValidationError("too_long", "context")
    return text


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


def generate_instructions(llm: LlmClient, template: str, schema: dict, context: object, *, model: str | None = None) -> str:
    """One local model call with only the User's context (no audio, Contact or other Session).

    Anything but valid instructions raises and returns nothing, so the caller keeps the previous
    context and prompt; the User retries explicitly.
    """
    text = check_context(context)
    # one pass, as build_prompt: placeholder text inside the context is not replaced
    raw = llm.generate_json(template.replace("{{context}}", text), model=model, json_schema=schema)
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


def confirm(domain: Domain, context: object, instructions: object) -> OthersPrompt:
    """The User's confirmation of exactly this context, these instructions and the prompt they make."""
    text = check_instructions(instructions)
    return OthersPrompt(check_context(context), text, render_prompt(domain, text))


def others_domain(domain: Domain, prompt: OthersPrompt) -> Domain:
    """The Domain the Domain stage runs with: same Action Types, schema and Workflow, confirmed prompt.

    If the Domain's template changed since the User confirmed, the prompt would no longer be the one
    they saw: refused, so they confirm again instead of running a prompt they never approved.
    """
    if render_prompt(domain, prompt.instructions) != prompt.prompt:
        raise ValueError("the Others template changed since the User confirmed the prompt; confirm it again")
    return dataclasses.replace(domain, prompt_template=prompt.prompt)
