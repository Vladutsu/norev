"""PayloadBuilder (core/ports, #18) for the `local_document` Connector (#23).

It makes, at preview time, the final payload of each `local_document` step: the exact document text
and the path it will be written to, so the User approves both (ADR 0002). The folder is the app's,
given when the builder is built, and the file name comes from the content: no option, Domain or
model answer chooses the path. LocalDocumentConnector.send() checks the same rule again.
"""
from datetime import date
from pathlib import Path
from typing import Any

from norev.adapters.connectors.local_document import NAME, destination, export_dir_of, render
from norev.core.models import Domain, ProposedAction, UnsupportedWorkflow, ValidationError
from norev.usecases.approve import domain_digest

GENERIC_FIELDS = ("title", "description", "owner", "due_date", "transcript_excerpt")  # domains/README.md
MAX_CONTENT = 100_000  # characters; a longer edited document is refused


def _value(action: ProposedAction, name: str) -> Any:
    if name in GENERIC_FIELDS:
        value = getattr(action, name)
        return value.isoformat() if isinstance(value, date) else value
    return action.fields.get(name)


class LocalDocumentPayloadBuilder:
    """For Workflows whose Connector steps are all `local_document`.

    options: `{}`, or `{"<step name>": {"content": "<the User's edited text>"}}` to replace the
    rendered document. Any other option, or a step with another Connector, is refused.
    """

    def __init__(self, export_dir: Path) -> None:
        self.export_dir = export_dir_of(export_dir)

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]:
        action_type = domain.action_type(action.action_type)
        if action_type is None:
            raise ValidationError("unknown_action_type", "action_type")
        connector_steps = [s for s in action_type.workflow.steps if s.connector]
        for step in connector_steps:
            if step.connector != NAME:
                raise UnsupportedWorkflow(f"no payload builder for Connector {step.connector!r}")
        if set(options) - {s.name for s in connector_steps}:
            raise ValidationError("unknown_options", "options")

        steps = []
        for step in connector_steps:
            option = options.get(step.name, {})
            if not isinstance(option, dict) or set(option) - {"content"}:
                raise ValidationError("invalid_options", step.name)
            content = option.get("content")
            if content is None:
                content = render({name: _value(action, name) for name in step.sends})
            elif not isinstance(content, str) or not content.strip() or len(content) > MAX_CONTENT:
                raise ValidationError("invalid_content", step.name)
            payload = {"destination": str(destination(self.export_dir, content)), "content": content}
            steps.append({"step": step.name, "connector": NAME, "payload": payload})
        return {"version": 1, "domain": domain.name, "domain_digest": domain_digest(domain),
                "action_type": action.action_type, "steps": steps}
