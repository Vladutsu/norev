#!/usr/bin/env python3
"""Check and fetch the dev data listed in data.manifest. Standard library only.

    scripts/data.py verify   every listed file is in the data dir, with the right size and sha256
    scripts/data.py fetch    copy missing or wrong files from NOREV_DATA_SOURCE, then verify

Data dir: --data, else NOREV_DATA_DIR, else /data.
NOREV_DATA_SOURCE: a local folder or a base URL (http/https) with the same layout as the data dir.
Manifest lines: "<sha256>  <size in bytes>  <relative path>"; blank lines and "#" comments are ignored.
"""
import argparse
import hashlib
import os
import shutil
import sys
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent


class Entry(NamedTuple):
    sha256: str
    size: int
    path: str


def read_manifest(manifest: Path) -> list[Entry]:
    entries = []
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3 or len(parts[0]) != 64 or not parts[1].isdigit():
            raise ValueError(f"{manifest}:{number}: expected '<sha256> <size> <path>', got {line!r}")
        path = PurePosixPath(parts[2])
        # fetch writes these paths into the data dir, so they must stay inside it
        if path.is_absolute() or ".." in path.parts or ":" in parts[2]:
            raise ValueError(f"{manifest}:{number}: path must be relative to the data dir: {parts[2]!r}")
        entries.append(Entry(parts[0].lower(), int(parts[1]), path.as_posix()))
    return entries


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def problem_with(entry: Entry, path: Path) -> str | None:
    if not path.is_file():
        return f"{entry.path}: missing"
    size = path.stat().st_size
    if size != entry.size:
        return f"{entry.path}: size {size}, expected {entry.size}"
    if sha256_of(path) != entry.sha256:
        return f"{entry.path}: checksum does not match the manifest"
    return None


def verify(manifest: Path, data_dir: Path) -> list[str]:
    problems = (problem_with(entry, data_dir / entry.path) for entry in read_manifest(manifest))
    return [problem for problem in problems if problem]


def _open_source(source: str, relative_path: str):
    if source.startswith(("http://", "https://")):
        return urllib.request.urlopen(source.rstrip("/") + "/" + urllib.parse.quote(relative_path), timeout=60)
    return (Path(source) / relative_path).open("rb")


def fetch(manifest: Path, data_dir: Path, source: str) -> list[str]:
    problems = []
    for entry in read_manifest(manifest):
        target = data_dir / entry.path
        if problem_with(entry, target) is None:
            continue
        partial = target.with_name(target.name + ".partial")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with _open_source(source, entry.path) as remote, partial.open("wb") as local:
                shutil.copyfileobj(remote, local)
            problem = problem_with(entry, partial)
            if problem:
                problems.append(f"{problem} (in {source})")
                continue
            os.replace(partial, target)
        except OSError as error:
            problems.append(f"{entry.path}: cannot fetch from {source}: {error}")
        finally:
            partial.unlink(missing_ok=True)
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["verify", "fetch"])
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "data.manifest")
    parser.add_argument("--data", type=Path, default=Path(os.getenv("NOREV_DATA_DIR") or "/data"))
    args = parser.parse_args(argv)

    if args.command == "fetch":
        source = os.getenv("NOREV_DATA_SOURCE", "").strip()
        if not source:
            print("NOREV_DATA_SOURCE is not set (a local folder or a base URL)", file=sys.stderr)
            return 2
        problems = fetch(args.manifest, args.data, source)
    else:
        problems = verify(args.manifest, args.data)

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"{len(problems)} dev data file(s) do not match {args.manifest}", file=sys.stderr)
        return 1
    print(f"dev data in {args.data} matches {args.manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
