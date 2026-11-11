"""#22: only the Central Processor holds the real LLM client and Connectors, and every stage that
calls a model (extraction, translation, Domain stage, Others) calls it through the processor."""
import ast
import json
import os
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from LLM.AI_Model.client import LlamaCppLLMClient
from LLM.AI_Model.settings import DeepSeekSettings
from norev.adapters.llm.legacy_extractor import LegacyActionExtractor
from norev.adapters.llm.translator import TurnTranslator
from norev.core.models import Segment, Session, Transcript
from norev.core.ports import LlmClient
from norev.processor.central import CentralProcessor
from norev.processor.compose import build_central_processor, build_google_calendar
from norev.processor.identity import LocalUser
from norev.testing.fakes import FakeLlmClient, FixedClock, InMemorySessionStore
from norev.usecases.domain_stage import run_domain_stage
from test_usecases_domain_stage import DOMAIN

ROOT = Path(__file__).resolve().parent.parent
AT = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)

# Modules that build a real network client or a real Connector
REAL_CLIENTS = ("LLM.AI_Model.client", "norev.adapters.connectors")
ALLOWED = ("norev/processor/", "norev/adapters/connectors/")


def _imports(path: Path) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found += [(node.lineno, node.module)] + [(node.lineno, f"{node.module}.{a.name}") for a in node.names]
    return found


class CompositionTest(unittest.TestCase):
    def test_only_the_processor_imports_a_real_client_or_connector(self) -> None:
        violations = []
        for path in sorted((ROOT / "norev").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel.startswith(ALLOWED):
                continue
            for line, name in _imports(path):
                if any(name == m or name.startswith(m + ".") for m in REAL_CLIENTS):
                    violations.append(f"{rel}:{line}: {name}")
        self.assertEqual(violations, [], "build real clients and Connectors in norev/processor only:\n" + "\n".join(violations))

    def test_no_relative_imports_that_the_check_would_miss(self) -> None:
        for path in sorted((ROOT / "norev").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            relative = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level]
            self.assertEqual(relative, [], f"{path.relative_to(ROOT)} uses relative imports")

    def test_build_holds_the_real_client_inside_the_processor(self) -> None:
        with patch.dict(os.environ, {"LLM_PROVIDER": "llamacpp", "LLAMACPP_URL": "http://127.0.0.1:9"}):
            processor = build_central_processor(audit=None, identity=LocalUser("u"))
        self.assertIsInstance(processor, LlmClient)
        self.assertIsInstance(processor._llm, LlamaCppLLMClient)  # built, not connected: port 9 has no server

    def test_google_calendar_is_built_here_and_given_to_the_processor(self) -> None:
        from norev.adapters.connectors.google_calendar import GoogleCalendarSettings
        from test_connectors_google_auth import MemorySecrets

        account, connector = build_google_calendar(MemorySecrets(), GoogleCalendarSettings("id", "secret"))
        self.assertEqual(account.status(), {"available": True, "connected": False})  # built, nothing contacted
        with patch.dict(os.environ, {"LLM_PROVIDER": "llamacpp", "LLAMACPP_URL": "http://127.0.0.1:9"}):
            processor = build_central_processor(audit=None, identity=LocalUser("u"), connectors=[connector])
        self.assertTrue(processor.has_connector("google_calendar"))


class StagesGoThroughTheProcessorTest(unittest.TestCase):
    def _processor(self, responses: list[str]) -> tuple[CentralProcessor, FakeLlmClient]:
        llm = FakeLlmClient(responses)
        return CentralProcessor(llm, None, identity=LocalUser("u"), clock=FixedClock(AT)), llm

    def test_translation(self) -> None:
        processor, llm = self._processor([json.dumps({"turns": [{"id": "1", "text": "Good morning."}]})])
        english = TurnTranslator(processor, model="qwen").translate(
            Transcript((Segment(0.0, 1.0, "Bună dimineața.", "S1"),), language="ro"), "ro"
        )
        self.assertEqual(english.segments[0].text, "Good morning.")
        self.assertEqual(llm.calls[0][1], "qwen")

    def test_extraction(self) -> None:
        processor, llm = self._processor([json.dumps({"session_summary": "s", "proposed_actions": []})])
        env = {"LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT": "0", "LLM_SECOND_PASS": "0", "LLM_PROVIDER": "llamacpp"}
        with patch.dict(os.environ, env):
            extractor = LegacyActionExtractor(client=processor, settings=DeepSeekSettings.from_environment())
            result = extractor.extract(Transcript((Segment(0.0, 1.0, "Come back on Monday."),), "en"), date(2026, 11, 6))
        self.assertEqual(result["proposed_actions"], [])
        self.assertEqual(len(llm.calls), 1)

    def test_domain_stage(self) -> None:
        processor, llm = self._processor([json.dumps({"proposed_actions": [], "suggestions": []})])
        store = InMemorySessionStore()
        store.save_session(Session(id="s1", started_at=AT, domain=DOMAIN.name))
        run_domain_stage("s1", store, DOMAIN, processor, Transcript((), "en"))
        [(_prompt, model, schema)] = llm.calls
        self.assertEqual((model, schema), (DOMAIN.model, DOMAIN.answer_schema))

    def test_others_generation_uses_the_injected_client(self) -> None:
        # Others (#21) gets the processor as its LlmClient; the call reaches only the injected client
        processor, llm = self._processor(['{"prompt": "p"}'])
        schema = {"type": "object"}
        self.assertEqual(processor.generate_json("context", model="others.gguf", json_schema=schema), '{"prompt": "p"}')
        self.assertEqual(llm.calls, [("context", "others.gguf", schema)])


if __name__ == "__main__":
    unittest.main()
