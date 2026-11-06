"""norev on Windows without developer setup (#48). Run through norev.cmd.

  norev.cmd                     check this machine, then open the app
  norev.cmd doctor              only the check: what is missing and how to add it
  norev.cmd new-key             create the store key (never overwrites); on Windows sealed with DPAPI
  norev.cmd protect-key         once: seal an existing plain key file with DPAPI (asks first)
  norev.cmd export-key <file>   back the key up, on purpose, outside norev's folder (asks first)
  norev.cmd import-key <file>   seal a key backup on this Windows account
  norev.cmd setup-models        download the Whisper (and diarization) models, once, on request
  norev.cmd process <file> [ro] the norev.sh flow: transcribe, analyse with llama-server, show

Paths: norev/launcher/paths.py. Nothing is downloaded except by setup-models.
"""
import argparse
import os
import site
import subprocess
import sys
from pathlib import Path

from norev.launcher.paths import AppPaths, apply_defaults, cuda_dll_dirs

ROOT = Path(__file__).resolve().parent.parent.parent

# The same LLM settings as norev.sh (see the comments there)
LLM_DEFAULTS = {
    "LLM_PROVIDER": "llamacpp",
    "LLM_FORCE_JSON_FORMAT": "1",
    "LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT": "0",
    "LLM_TEMPERATURE": "0",
    "LLM_SECOND_PASS": "0",
    "LLM_PARALLEL": "4",
}


def prepare(paths: AppPaths, *, offline: bool = True) -> None:
    apply_defaults(paths, os.environ, offline=offline)
    if paths.windows:
        dirs = cuda_dll_dirs([Path(p) for p in site.getsitepackages()])
        for d in dirs:
            os.add_dll_directory(str(d))
        if dirs:  # child processes (llama-server, norev.session) find the same DLLs
            os.environ["PATH"] = os.pathsep.join([*map(str, dirs), os.environ.get("PATH", "")])


def doctor(paths: AppPaths) -> int:
    from norev.launcher.doctor import ready, report, run_checks

    checks = run_checks(paths)
    print(report(checks))
    return 0 if ready(checks) else 1


BACKUP_WARNING = ("Fără cheie, datele salvate nu mai pot fi citite de nimeni. Cheia e legată de acest cont "
                  "Windows: dacă reinstalezi Windows sau schimbi contul, doar backup-ul o mai poate aduce înapoi.")


def _confirmed(question: str, ask) -> bool:
    return ask(f"{question} Scrie DA ca să continui: ").strip() == "DA"


def new_key(paths: AppPaths) -> int:
    from norev.adapters.store.encrypted import create_key_file
    from norev.launcher.keys import create_protected_key

    existing = next((k for k in (paths.key, paths.plain_key) if k.is_file()), None)
    if existing:
        print(f"Cheia există deja: {existing}. Nu o suprascriu.")
        return 0
    if paths.windows:
        create_protected_key(paths.key)
        print(f"Am creat cheia, protejată de contul tău Windows: {paths.key}")
    else:
        create_key_file(paths.key)
        print(f"Am creat cheia: {paths.key}")
    print(f"{BACKUP_WARNING}\nFă un backup acum: norev.cmd export-key E:\\norev-key.bak")
    return 0


def protect_key(paths: AppPaths, ask=input) -> int:
    from norev.adapters.store.encrypted import StoreKeyError
    from norev.launcher.keys import protect_existing

    if not paths.windows:
        print("Protecția DPAPI există doar pe Windows; aici cheia rămâne fișier.")
        return 1
    if paths.key.is_file():
        print(f"Cheia e deja protejată: {paths.key}")
        return 0
    if not paths.plain_key.is_file():
        print(f"Nu am găsit cheia neprotejată în {paths.plain_key}.")
        return 1
    if not _confirmed(f"Mut cheia {paths.plain_key} sub protecția contului tău Windows. Datele rămân neschimbate.", ask):
        print("Nu am schimbat nimic.")
        return 1
    try:
        protect_existing(paths.plain_key, paths.key, paths.store)
    except StoreKeyError as e:
        print(f"Nu am protejat cheia: {e}. Nu am schimbat nimic.")
        return 1
    print(f"Cheia e protejată: {paths.key}. Datele se deschid cu ea.")
    print(BACKUP_WARNING)
    if _confirmed(f"Șterg fișierul neprotejat {paths.plain_key}? Fă întâi backup-ul (export-key).", ask):
        paths.plain_key.unlink()
        print("Am șters fișierul neprotejat.")
    else:
        print(f"Am păstrat {paths.plain_key}. Șterge-l după ce faci backup-ul.")
    return 0


def export_key(paths: AppPaths, destination: str, ask=input) -> int:
    from norev.adapters.store.encrypted import StoreKeyError
    from norev.launcher.keys import export_backup

    key = next((k for k in (paths.key, paths.plain_key) if k.is_file()), None)
    if key is None:
        print("Nu există cheie de exportat.")
        return 1
    print(f"Backup-ul e cheia NEPROTEJATĂ: oricine îl are poate citi datele. {BACKUP_WARNING}")
    if not _confirmed(f"Scriu backup-ul în {destination}. Ține-l pe un stick USB, separat de calculator.", ask):
        print("Nu am exportat nimic.")
        return 1
    try:
        export_backup(key, Path(destination), paths.root)
    except FileExistsError:
        print(f"{destination} există deja; nu îl suprascriu.")
        return 1
    except (ValueError, StoreKeyError) as e:
        print(f"Nu am exportat: {e}")
        return 1
    print(f"Backup scris: {destination}")
    return 0


def import_key(paths: AppPaths, source: str) -> int:
    from norev.adapters.store.encrypted import StoreKeyError
    from norev.launcher.keys import protect_existing

    if not paths.windows:
        print(f"Pe acest sistem cheia e un fișier: copiaz-o în {paths.key}.")
        return 1
    try:
        protect_existing(Path(source), paths.key, paths.store)
    except FileExistsError:
        print(f"Există deja o cheie protejată: {paths.key}. Nu o suprascriu.")
        return 1
    except StoreKeyError as e:
        print(f"Nu am importat: {e}")
        return 1
    print(f"Cheia din {source} e protejată de acest cont Windows: {paths.key}")
    return 0


def setup_models(paths: AppPaths) -> int:
    """The only command that downloads: Whisper, and the diarization model when it is installed."""
    from faster_whisper.utils import download_model

    from norev.launcher.doctor import DIARIZATION_MODEL

    cache = str(paths.hf_home / "hub")
    name = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
    print(f"Descarc modelul Whisper {name} în {paths.hf_home} ...")
    download_model(name, cache_dir=cache)
    try:
        import nemo  # noqa: F401
    except ImportError:
        print("Diarizarea nu e instalată (install.cmd --speakers); sar peste modelul ei.")
    else:
        from huggingface_hub import snapshot_download

        print(f"Descarc modelul de diarizare {DIARIZATION_MODEL} ...")
        snapshot_download(DIARIZATION_MODEL, cache_dir=cache)
    print("Gata. Modelul LLM (GGUF) și llama-server nu se descarcă automat: vezi README, secțiunea Windows.")
    return 0


def process(paths: AppPaths, media: str, language: str | None) -> int:
    from norev.launcher.llama import LlamaError, server_from_environment

    if not os.environ.get("NOREV_STORE_KEY_FILE"):
        print("Lipsește cheia store-ului. Rulează: norev.cmd new-key")
        return 1
    for name, value in LLM_DEFAULTS.items():
        os.environ.setdefault(name, value)
    server = server_from_environment(paths.logs / "llama-server.log")
    os.environ.setdefault("LLAMACPP_URL", server.url)
    os.environ["LLM_MODEL"] = server.alias
    session = [sys.executable, "-m", "norev.session"]

    print(f"[1/3] Transcriu {media}")
    args = [*session, "transcribe", media, *(["--language", language] if language else [])]
    if os.environ.get("NOREV_DOMAIN"):
        args += ["--domain", os.environ["NOREV_DOMAIN"]]
    result = subprocess.run(args, stdout=subprocess.PIPE, text=True, cwd=ROOT)
    if result.returncode:
        return result.returncode
    session_id = result.stdout.strip().splitlines()[-1]

    print("[2/3] Traduc, extrag acțiunile propuse și rulez etapa Domain")
    try:
        with server:  # Whisper finished, so the GPU is free for the LLM
            code = subprocess.run([*session, "analyse", session_id], cwd=ROOT).returncode
    except LlamaError as e:
        print(f"{e}. Rulează norev.cmd doctor.")
        return 1
    if code:
        return code
    print("[3/3] Rezultat")
    return subprocess.run([*session, "show", session_id], cwd=ROOT).returncode


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="norev.cmd", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("ui")
    sub.add_parser("doctor")
    sub.add_parser("new-key")
    sub.add_parser("protect-key")
    sub.add_parser("export-key").add_argument("destination")
    sub.add_parser("import-key").add_argument("source")
    sub.add_parser("setup-models")
    p = sub.add_parser("process")
    p.add_argument("media")
    p.add_argument("language", nargs="?")
    args = parser.parse_args(argv)

    paths = AppPaths.from_environment()
    prepare(paths, offline=args.command != "setup-models")
    if args.command == "doctor":
        return doctor(paths)
    if args.command == "new-key":
        return new_key(paths)
    if args.command == "protect-key":
        return protect_key(paths)
    if args.command == "export-key":
        return export_key(paths, args.destination)
    if args.command == "import-key":
        return import_key(paths, args.source)
    if args.command == "setup-models":
        return setup_models(paths)
    if args.command == "process":
        return process(paths, args.media, args.language)

    doctor(paths)
    print("\nPornesc aplicația ...")
    from norev.ui.__main__ import main as ui_main

    return ui_main([])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
