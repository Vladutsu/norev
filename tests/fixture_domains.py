"""The medical and others Domains, kept only as test fixtures: this version ships Corporate alone.

The core is industry-agnostic (ADR 0003); its tests still exercise it with more than one Domain.
"""
from pathlib import Path

TEST_DOMAINS = Path(__file__).resolve().parent / "fixtures" / "domains"

# The modes of the full app, for tests that drive the UI with every Domain
ALL_MODES = (
    ("corporate", "Corporate", "Implicit"),
    ("medical", "Medical", "Exemplu"),
    ("others", "Others", "Prompt propriu"),
)
_ALL = None


def all_domains_dir() -> Path:
    """A domains folder with the shipped Corporate and the fixture Domains, made once per test run."""
    global _ALL
    if _ALL is None:
        import atexit
        import shutil
        import tempfile

        _ALL = Path(tempfile.mkdtemp(prefix="norev-domains-"))
        atexit.register(shutil.rmtree, _ALL, True)
        shutil.copytree(TEST_DOMAINS.parent.parent.parent / "domains" / "corporate", _ALL / "corporate")
        for name in ("medical", "others"):
            shutil.copytree(TEST_DOMAINS / name, _ALL / name)
    return _ALL
