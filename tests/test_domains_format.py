"""Every folder in domains/ follows the Domain format in domains/README.md (OPEN-QUESTIONS #4).

Standard library only; no model is run.
"""
import json
import re
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

DOMAINS = Path(__file__).resolve().parent.parent / "domains"

# shortcut: Connectors are core code that does not exist yet; keep in sync with domains/README.md.
KNOWN_CONNECTORS = {"email_draft", "google_calendar", "local_document", "trello"}
FIELD_TYPES = {"string", "date", "time"}
GENERIC_FIELDS = {"title", "description", "owner", "due_date", "transcript_excerpt"}
PLACEHOLDERS = {"session_date", "roles", "action_types", "transcript", "proposed_actions"}
REQUIRED_PLACEHOLDERS = {"transcript", "proposed_actions"}
NAME = re.compile(r"[a-z][a-z0-9_]*")
PATTERNS = {"date": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$", "time": "^[0-9]{2}:[0-9]{2}$"}


def domain_folders() -> list[Path]:
    return sorted(p for p in DOMAINS.iterdir() if p.is_dir())


def _text(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _unique_names(items: list[dict], what: str) -> list[str]:
    names = [item.get("name") for item in items]
    for name in names:
        assert isinstance(name, str) and NAME.fullmatch(name), f"{what} name {name!r} is not snake_case"
    assert len(set(names)) == len(names), f"duplicate {what} names: {names}"
    return names


def _variants(schema: dict) -> list[dict]:
    return schema.get("anyOf", [schema])


def _allows_null(schema: dict) -> bool:
    for variant in _variants(schema):
        types = variant.get("type")
        if types == "null" or (isinstance(types, list) and "null" in types):
            return True
    return False


def check_domain(folder: Path) -> None:
    """Raise AssertionError with the first problem found in one Domain folder."""
    path = folder / "domain.toml"
    assert path.is_file(), f"{folder.name}: no domain.toml"
    domain = tomllib.loads(path.read_text(encoding="utf-8"))

    assert domain.get("format") == 1, "format must be 1"
    assert domain.get("name") == folder.name, f"name {domain.get('name')!r} must equal the folder name"
    assert _text(domain.get("label")), "label missing"

    model = domain.get("model", {})
    assert _text(model.get("gguf")), "model.gguf missing"
    assert isinstance(model.get("context"), int) and model["context"] > 0, "model.context must be a positive int"

    roles = domain.get("roles", [])
    assert roles, "at least one Role"
    _unique_names(roles, "Role")
    for role in roles:
        assert _text(role.get("label")), f"Role {role['name']}: label missing"

    action_types = domain.get("action_types", [])
    assert action_types, "at least one Action Type"
    _unique_names(action_types, "Action Type")
    for action_type in action_types:
        where = f"Action Type {action_type['name']}"
        assert _text(action_type.get("label")), f"{where}: label missing"
        assert _text(action_type.get("description")), f"{where}: description missing"
        fields = action_type.get("fields", [])
        assert fields, f"{where}: at least one field"
        field_names = _unique_names(fields, f"{where} field")
        for field in fields:
            assert _text(field.get("label")), f"{where}.{field['name']}: label missing"
            assert field.get("type") in FIELD_TYPES, f"{where}.{field['name']}: type must be one of {FIELD_TYPES}"
            assert isinstance(field.get("required"), bool), f"{where}.{field['name']}: required must be true or false"
        steps = action_type.get("workflow", [])
        assert steps, f"{where}: Workflow has no steps"
        _unique_names(steps, f"{where} Workflow step")
        for step in steps:
            step_where = f"{where} step {step['name']}"
            assert _text(step.get("label")), f"{step_where}: label missing"
            connector = step.get("connector")
            if connector is None:
                assert "sends" not in step, f"{step_where}: sends without a connector"
                assert "alternatives" not in step, f"{step_where}: alternatives without a connector"
                continue
            assert connector in KNOWN_CONNECTORS, f"{step_where}: unknown connector {connector!r}"
            alternatives = step.get("alternatives", [])
            assert isinstance(alternatives, list), f"{step_where}: alternatives must be a list"
            repeated = len(set(alternatives)) != len(alternatives) or connector in alternatives
            assert not repeated, f"{step_where}: alternatives repeat a Connector"
            for alternative in alternatives:
                assert alternative in KNOWN_CONNECTORS, f"{step_where}: unknown alternative {alternative!r}"
            sends = step.get("sends")
            assert isinstance(sends, list) and sends, f"{step_where}: sends must list the payload fields"
            unknown = set(sends) - set(field_names) - GENERIC_FIELDS
            assert not unknown, f"{step_where}: sends unknown fields {sorted(unknown)}"

    prompts = domain.get("prompts", {})
    prompt_path = folder / prompts.get("domain_stage", "")
    schema_path = folder / prompts.get("domain_stage_schema", "")
    assert prompt_path.is_file() and prompt_path != folder, "prompts.domain_stage file missing"
    assert schema_path.is_file() and schema_path != folder, "prompts.domain_stage_schema file missing"

    used = set(re.findall(r"\{\{(.*?)\}\}", prompt_path.read_text(encoding="utf-8")))  # as norev/usecases/domain_stage.py
    if folder.name == "others":  # #21: the User's confirmed instructions go in one fixed slot
        assert "user_instructions" in used, "the others prompt must have {{user_instructions}}"
        used -= {"user_instructions"}
        for key in ("instructions", "instructions_schema"):
            assert (folder / prompts.get(key, "")).is_file() and prompts.get(key), f"prompts.{key} file missing"
        generation = (folder / prompts["instructions"]).read_text(encoding="utf-8")
        assert re.findall(r"\{\{(.*?)\}\}", generation) == ["context"], "instructions prompt must use {{context}} once, nothing else"
    assert used <= PLACEHOLDERS, f"unknown placeholders {sorted(used - PLACEHOLDERS)}"
    assert REQUIRED_PLACEHOLDERS <= used, f"prompt must use {sorted(REQUIRED_PLACEHOLDERS - used)}"

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert set(schema.get("required", [])) == {"proposed_actions", "suggestions"}, "schema top-level keys"
    variants = schema["properties"]["proposed_actions"]["items"]["anyOf"]
    by_type = {v["properties"]["action_type"]["const"]: v for v in variants}
    assert set(by_type) == {a["name"] for a in action_types}, "schema Action Types differ from domain.toml"
    for action_type in action_types:
        props = by_type[action_type["name"]]["properties"]["fields"]
        where = f"schema {action_type['name']}"
        names = [f["name"] for f in action_type["fields"]]
        assert set(props["properties"]) == set(names), f"{where}: fields differ from domain.toml"
        assert set(props.get("required", [])) == set(names), f"{where}: every field must be present (null if not said)"
        assert props.get("additionalProperties") is False, f"{where}: additionalProperties must be false"
        for field in action_type["fields"]:
            field_schema = props["properties"][field["name"]]
            # the model must be able to answer null instead of inventing a value (OPEN-QUESTIONS #6)
            assert _allows_null(field_schema), f"{where}.{field['name']}: must allow null"
            if field["type"] in PATTERNS:
                patterns = {v.get("pattern") for v in _variants(field_schema)}
                assert PATTERNS[field["type"]] in patterns, f"{where}.{field['name']}: missing {field['type']} pattern"


class DomainsFormatTest(unittest.TestCase):
    def test_there_are_corporate_and_medical_domains(self):
        self.assertLessEqual({"corporate", "medical"}, {f.name for f in domain_folders()})

    def test_every_domain_follows_the_format(self):
        for folder in domain_folders():
            with self.subTest(domain=folder.name):
                check_domain(folder)

    def broken_copy(self, relative: str, old: str, new: str) -> Path:
        """A copy of the medical Domain with `old` replaced by `new` in one file."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        copy = Path(tmp.name) / "medical"
        shutil.copytree(DOMAINS / "medical", copy)
        path = copy / relative
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return copy

    def test_check_catches_an_unknown_connector(self):
        copy = self.broken_copy("domain.toml", '"google_calendar"', '"smtp"')
        with self.assertRaisesRegex(AssertionError, "unknown connector 'smtp'"):
            check_domain(copy)

    def test_check_catches_a_field_that_cannot_be_null(self):
        copy = self.broken_copy(
            "prompts/domain_stage.schema.json", '"reason": {"type": ["string", "null"]}', '"reason": {"type": "string"}'
        )
        with self.assertRaisesRegex(AssertionError, "reason: must allow null"):
            check_domain(copy)

    def test_check_catches_an_unknown_placeholder(self):
        copy = self.broken_copy("prompts/domain_stage.md", "{{roles}}", "{{speakers}}")
        with self.assertRaisesRegex(AssertionError, "unknown placeholders"):
            check_domain(copy)


if __name__ == "__main__":
    unittest.main()
