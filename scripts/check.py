"""The Python half of check.sh, also run by check.cmd on Windows: compile, import every module, tests.

Usage (from the repo root): python scripts/check.py
"""
import compileall
import importlib
import pkgutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sys.path.insert(0, str(ROOT))

    print("==> python compiles", flush=True)
    if not all(compileall.compile_dir(ROOT / pkg, quiet=1) for pkg in ("LLM", "norev")):
        return 1

    print("==> every module imports", flush=True)
    failed = []
    for name in ("LLM", "norev"):
        pkg = importlib.import_module(name)
        for mod in pkgutil.walk_packages(pkg.__path__, pkg.__name__ + "."):
            try:
                importlib.import_module(mod.name)
            except Exception as exc:
                failed.append(f"  {mod.name}: {exc!r}")
    if failed:
        print("import failed:\n" + "\n".join(failed))
        return 1

    if list((ROOT / "tests").glob("test_*.py")):
        print("==> unit tests", flush=True)
        sys.path.insert(0, str(ROOT / "tests"))
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests"))
        if not unittest.TextTestRunner().run(suite).wasSuccessful():
            return 1

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
