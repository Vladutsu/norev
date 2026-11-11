"""Dependency rule for core and usecases (docs/ARCHITECTURE.md, "Layers").

ADR 0001/0004: only the Central Processor talks to the network or a model, so core and
usecases import no network client, model runtime or UI framework, and none of the outer
layers (adapters, processor, ui).
ADR 0003: the core has no industry-specific code, so it never imports from a Domain
(or from the legacy `LLM` package, whose prompt is project-management specific).
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKED_DIRS = [ROOT / "norev" / "core", ROOT / "norev" / "usecases"]

FORBIDDEN = (
    "socket", "ssl", "urllib", "urllib3", "http", "ftplib", "smtplib",  # network
    "requests", "httpx", "aiohttp", "websockets",
    "faster_whisper", "ctranslate2", "llama_cpp", "ollama", "openai",  # model runtimes
    "webview",  # UI framework
    "norev.adapters", "norev.processor", "norev.ui",  # outer layers
    "LLM", "domains", "norev.domains",  # industry-specific code
)


def _is_forbidden(name: str) -> bool:
    return any(name == f or name.startswith(f + ".") for f in FORBIDDEN)


def forbidden_imports(source: str, filename: str = "<string>", package: str = "") -> list[str]:
    """Return "file:line: module" for every import of a forbidden module.

    `package` is the importing module's package (e.g. "norev.core"), used to resolve relative imports.
    """
    found = []
    for node in ast.walk(ast.parse(source, filename)):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:  # relative: resolve against the importing package
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(parts + ([base] if base else []))
            # `from x import y` may import the submodule x.y
            names = [base] + [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
        else:
            continue
        hits = sorted({n for n in names if _is_forbidden(n)})
        if hits:
            found.append(f"{filename}:{node.lineno}: {hits[0]}")
    return found


class CoreImportRuleTest(unittest.TestCase):
    def test_core_and_usecases_import_no_forbidden_module(self):
        violations = []
        for directory in CHECKED_DIRS:  # usecases may not exist yet
            for path in sorted(directory.rglob("*.py")):
                rel = path.relative_to(ROOT)
                package = ".".join(rel.parent.parts)
                violations += forbidden_imports(path.read_text(encoding="utf-8"), str(rel), package)
        self.assertEqual(violations, [], "forbidden imports (ADR 0001/0003/0004):\n" + "\n".join(violations))

    def test_checker_flags_forbidden_imports(self):
        for source in (
            "import socket",
            "from urllib import request",
            "import LLM.AI_Model",
            "import llama_cpp",
            "from norev.domains.medical import x",
            "from norev import domains",
            "from ..domains import x",
            "from .. import adapters",
            "import norev.adapters.llm",
        ):
            with self.subTest(source=source):
                self.assertEqual(len(forbidden_imports(source, package="norev.core")), 1)

    def test_checker_allows_stdlib_and_core_imports(self):
        for source in ("import json", "from norev.core import models", "from . import models", "from .ports import Clock"):
            with self.subTest(source=source):
                self.assertEqual(forbidden_imports(source, package="norev.core"), [])


if __name__ == "__main__":
    unittest.main()
