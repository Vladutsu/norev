"""Loads a Domain from its folder (format: domains/README.md, ADR 0003).

Minimal checks only; the full format check is tests/test_domains_format.py, run in CI.
"""
import json
import tomllib
from pathlib import Path

from norev.core.models import ActionType, Domain, DomainField, Role, Workflow, WorkflowStep

DOMAINS = Path(__file__).resolve().parents[3] / "domains"


def _read_inside(folder: Path, relative: str) -> str:
    """Read a file named in [prompts]; refuse paths that leave the Domain folder."""
    path = (folder / relative).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError(f"Domain {folder.name}: prompt path {relative!r} is outside the Domain folder")
    return path.read_text(encoding="utf-8")


def load_domain(folder: Path) -> Domain:
    data = tomllib.loads((folder / "domain.toml").read_text(encoding="utf-8"))
    if data.get("format") != 1:
        raise ValueError(f"Domain {folder.name}: format must be 1, not {data.get('format')!r}")
    if data.get("name") != folder.name:
        raise ValueError(f"Domain {folder.name}: name {data.get('name')!r} must equal the folder name")

    prompts = data["prompts"]
    return Domain(
        name=data["name"],
        label=data["label"],
        model=data["model"]["gguf"],
        context=data["model"]["context"],
        roles=tuple(Role(r["name"], r.get("description", ""), r.get("label", "")) for r in data["roles"]),
        action_types=tuple(
            ActionType(
                name=a["name"],
                fields=tuple(
                    DomainField(f["name"], f["type"], f["required"], f.get("label", ""), f.get("description", ""))
                    for f in a["fields"]
                ),
                workflow=Workflow(
                    tuple(
                        WorkflowStep(s["name"], s.get("connector"), tuple(s.get("sends", ())), s.get("label", ""))
                        for s in a["workflow"]
                    )
                ),
                label=a.get("label", ""),
                description=a.get("description", ""),
            )
            for a in data["action_types"]
        ),
        prompt_template=_read_inside(folder, prompts["domain_stage"]),
        answer_schema=json.loads(_read_inside(folder, prompts["domain_stage_schema"])),
    )


def load_others(folder: Path) -> tuple[Domain, str, dict]:
    """The Others Domain (#21), plus the prompt and schema that generate its instructions."""
    domain = load_domain(folder)
    prompts = tomllib.loads((folder / "domain.toml").read_text(encoding="utf-8"))["prompts"]
    return domain, _read_inside(folder, prompts["instructions"]), json.loads(_read_inside(folder, prompts["instructions_schema"]))
