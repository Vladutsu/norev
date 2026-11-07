# norev — Session Transcript + Proposed Action extraction (CLI)

Transcribes a recorded Session locally and extracts Proposed Actions (Session summary, Proposed Actions
with owner/deadline/priority and the Transcript excerpt they came from, decisions, risks). The
extraction prompt is still project-management oriented (see `LLM/AI_Model/prompt.py`). Everything runs on the machine: Whisper (faster-whisper)
for speech and local LLMs served by llama.cpp.

Pipeline:

1. **Whisper** transcribes in the spoken language: a preselected list or auto-detection.
2. **Translator LLM** translates the Transcript to English (skipped when the Session is in English).
3. **Extractor LLM** extracts the Proposed Actions from the English Transcript.

Each LLM stage can use its own GGUF model (default: Qwen3-8B for both, so it is loaded once). Models
are loaded one at a time, so each stage only needs the GPU memory of its own model.

**GPU memory** (measured, llama-server with Qwen3-8B Q4_K_M and a q8_0 KV cache): 5.9 GB with the
default 4 parallel slots, 5.0 GB with `LLM_PARALLEL=1`. The defaults are tested on an 8 GB GPU; on the
6 GB target in `AGENTS.md` they are at the limit (the desktop also uses some VRAM), so use
`LLM_PARALLEL=1` there. Whisper and the LLM never run at the same time.

Reliability and speed:
- Output is constrained to a JSON schema, so a model cannot answer `{}` or rename fields.
- The extraction prompt carries a calendar of the coming weeks, so "by Monday" resolves to the right date.
- Transcript chunks are processed 4 at a time (`llama-server --parallel 4`); a 30-minute Session takes
  about 2 minutes end to end on an RTX 4070 Laptop GPU.
- Proposed Actions repeated across chunks are merged by owner and title similarity.
- Optional second pass (`LLM_SECOND_PASS=1`) looks for missed Proposed Actions; the ones it adds are
  marked `[verifică]` because they more often have the wrong owner.
- Temperature 0: the same recording gives the same Proposed Actions.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Also needed:
- `llama-server` from [llama.cpp](https://github.com/ggml-org/llama.cpp), default `~/Repositories/llama.cpp/build/bin/llama-server`
- a GGUF model, default `~/models/qwen3-8b/Qwen3-8B-Q4_K_M.gguf` ([Qwen/Qwen3-8B-GGUF](https://huggingface.co/Qwen/Qwen3-8B-GGUF), Apache 2.0) for translation and extraction

## Windows (no developer setup)

One install, one way to start (#48). Needs Python 3.12 from python.org ("Add python.exe to PATH")
and, for speed, a current NVIDIA driver; without a GPU everything runs on the CPU, slowly.

```bat
install.cmd               :: once: .venv, Python packages, the store key, then the check
install.cmd --speakers    :: the same, plus diarization (Speakers); a much larger download
norev.cmd                 :: check this machine, then open the app
norev.cmd doctor          :: only the check: what is missing and how to add it
norev.cmd setup-models    :: download the Whisper (and diarization) models, once
norev.cmd process session.mp4 ro   :: the norev.sh flow: transcribe, analyse, show
check.cmd                 :: the same Python checks as ./check.sh
```

Nothing is downloaded without asking: the app starts offline (`HF_HUB_OFFLINE=1`), only
`setup-models` downloads models, and the GGUF model and `llama-server` are copied by hand:

- `llama-server.exe`: a Windows CUDA release of [llama.cpp](https://github.com/ggml-org/llama.cpp/releases),
  unzipped into `%LOCALAPPDATA%\norev\llama.cpp\`
- the GGUF model (`Qwen3-8B-Q4_K_M.gguf`, see Setup) in `%LOCALAPPDATA%\norev\models\`

Everything stays in the User's profile, not in the app folder (`NOREV_HOME` moves it all;
each path also has its own variable, table below):

| What | Where |
|---|---|
| Encrypted store | `%LOCALAPPDATA%\norev\norev.db` |
| Store key (back it up on a USB stick) | `%LOCALAPPDATA%\norev\keys\norev.key` |
| GGUF models | `%LOCALAPPDATA%\norev\models\` |
| Whisper and diarization models | `%LOCALAPPDATA%\norev\models\hf\` |
| llama-server | `%LOCALAPPDATA%\norev\llama.cpp\llama-server.exe` |
| llama-server log | `%LOCALAPPDATA%\norev\logs\` |

## Usage

Sessions are kept only in the encrypted store (ADR 0004). Create the key once, outside the
store's folder, and back it up: without it the store cannot be read.

```bash
.venv/bin/python -m norev.adapters.store new-key ~/.config/norev/norev.key
export NOREV_STORE_KEY_FILE=~/.config/norev/norev.key
```

```bash
./norev.sh session.mp4                          # Corporate Domain, auto-detect the language
NOREV_DOMAIN=medical ./norev.sh consult.mp4     # Medical Domain (example)
NOREV_SPEAKERS=0 ./norev.sh session.mp4         # no diarization (requirements-diarization.txt not installed)
./norev.sh session.mp4 ro                       # force Romanian
NOREV_LANGUAGES=ro,en,de ./norev.sh session.mp4 # pick the most likely of these languages
NOREV_DATE=2026-11-06 ./norev.sh session.mp4    # Session date for "Friday", "13 November"...
./norev.sh --list-llm-models
```

Supported: `.wav .mp3 .m4a .flac .ogg .opus .aac .webm .mp4 .mkv .mov .avi`.

`norev.sh` runs the three steps of `python -m norev.session`, which can also be run by hand:

```bash
SESSION=$(.venv/bin/python -m norev.session transcribe session.mp4 [--domain corporate|medical] [--language ro] [--date 2026-11-06] [--no-speakers])
.venv/bin/python -m norev.session analyse "$SESSION" [--no-translate]   # needs llama-server running
.venv/bin/python -m norev.session analyse "$SESSION" --regenerate [--domain medical]
.venv/bin/python -m norev.session show "$SESSION"
```

- `transcribe`: the audio is sealed in the store, then Whisper + diarization (decoded in memory);
  the Transcript with Speakers is saved in the store; prints the Session id.
- `analyse`: translation per Speaker turn, extraction (only from this final Transcript), Domain stage.
  Running it again regenerates the Proposed Actions; once any of them was decided it is refused
  unless you ask for `--regenerate`, which replaces only the undecided ones and keeps every decision.
  `--regenerate --domain X` also changes the Session's Domain; it waits until no approved action is
  still to be executed.
  Turns that could not be translated are kept in the spoken language and the result is marked partial.
- `show`: the Proposed Actions waiting for review, Suggestions, and any untranslated turns.

The Domain is `corporate` unless `--domain`/`NOREV_DOMAIN` says otherwise; an unknown name is an
error. `others` runs only with a prompt the User saw, edited and confirmed (#21); the CLI cannot show
one, so it refuses `others`. Speaker ids
(`S1`, `S2`, ...) only tell voices apart, they are not people: Roles and owners are not guessed from them.

Nothing is written to `Outputs/` except `llama-server.log`. The old stages
`python -m LLM.pipeline translate|extract` are deprecated (they write plaintext JSON) and print a warning.

### Desktop app

```bash
.venv/bin/pip install -r requirements-ui.txt
.venv/bin/python -m norev.ui           # record (microphone) or import a file, review, approve, export
.venv/bin/python -m norev.ui --demo    # synthetic examples, reviewed but never executed
```

Same flow as `norev.sh`, started from the window: the audio is sealed in the store (never written to
a file), Whisper and diarization run first, each in a child process that ends before the next model
loads (so no CUDA context stays on the GPU), then the app starts `llama-server` for the analysis and
stops it after (same variables as `norev.sh`; a server already running at `LLAMACPP_URL` is used).
An approved Proposed Action is executed only on **Execute**, through the Workflow executor and the
Central Processor; a failed one is retried by hand, and a step interrupted by a restart is shown for
the User to confirm, never sent again on its own. While recording, the Recording screen shows a Live
Transcript, feedback only: the same Whisper model and device (`WHISPER_*`) in a child process, about
every 5 s, kept in memory and never analysed. Stop ends it before the final Transcript; if it cannot
load (no model, not enough VRAM), the screen says so and the recording goes on.

## Before pushing

```bash
./check.sh        # Windows: check.cmd (same Python checks, no bash syntax step)
```

Same checks as CI (`.github/workflows/ci.yml`, runs on every push to `main`): shell syntax,
Python compiles, every module imports, unit tests in `tests/` if any. No GPU or models needed.
To run CI on a branch: GitHub, Actions, CI, Run workflow.

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `NOREV_STORE_KEY_FILE` | required | key of the encrypted store (see Usage) |
| `NOREV_STORE_PATH` | `%LOCALAPPDATA%/norev/norev.db` or `~/.local/share/norev/norev.db` | the encrypted store |
| `NOREV_HOME` | `%LOCALAPPDATA%/norev` or `~/.local/share/norev` | `norev.cmd` only: root of the store, key, models, llama-server and logs (Windows section) |
| `NOREV_DOMAIN` | `corporate` | `corporate` or `medical` (`others` needs a confirmed prompt, not from the CLI) |
| `NOREV_SPEAKERS` | `1` | `0`: skip diarization (one Transcript without Speakers) |
| `NOREV_LANGUAGES` | empty (auto-detect) | Whisper language codes to choose from, e.g. `ro,en,de` |
| `NOREV_TRANSLATE` | `auto` | `auto` translates non-English Sessions, `0` extracts from the original language |
| `LLAMACPP_MODEL` | Qwen3-8B (see above) | default model for the analysis |
| `NOREV_EXTRACT_MODEL` | `$LLAMACPP_MODEL` | GGUF for extraction; the Domain stage uses the Domain's `[model]` (domain.toml) |
| `NOREV_TRANSLATE_MODEL` | `$NOREV_EXTRACT_MODEL` | GGUF for translation; llama-server restarts only when the next stage's model differs |
| `NOREV_MODELS_DIR` | `~/models` (norev.sh), `%LOCALAPPDATA%\norev\models` | where a Domain's `[model] gguf` is looked up, subfolders included |
| `LLM_PARALLEL` | `4` | concurrent requests (llama-server slots) per stage |
| `LLM_SECOND_PASS` | `0` | `1`: second look for missed Proposed Actions (more found, but more wrong owners; ~3x slower extraction) |
| `LLM_TEMPERATURE` | `0` | sampling temperature |
| `NOREV_DATE` | today | Session date used to resolve relative dates |
| `NOREV_TIMEZONE` | `Europe/Bucharest` | IANA time zone of Google Calendar events (explicit in every payload) |
| `NOREV_GOOGLE_CLIENT_ID` / `NOREV_GOOGLE_CLIENT_SECRET` | empty (Google Calendar off) | OAuth client of your Google Cloud project, type "Desktop app"; never commit them. The User's refresh token goes into the encrypted store |
| `NOREV_JIRA_CLIENT_ID` / `NOREV_JIRA_CLIENT_SECRET` | empty (Jira off) | OAuth 2.0 (3LO) app from the Atlassian developer console, scopes `read:jira-work write:jira-work offline_access`; never commit them. The refresh token and the chosen project go into the encrypted store |
| `NOREV_JIRA_REDIRECT_PORT` | `8765` | loopback port of the Jira sign-in; register `http://127.0.0.1:<port>` as the app's callback URL |
| `LLAMA_SERVER` | `~/Repositories/llama.cpp/build/bin/llama-server` | llama.cpp server binary |
| `LLAMACPP_PORT` / `LLAMACPP_CTX` / `LLAMACPP_GPU_LAYERS` | `8080` / `4096` / `99` | server options (`LLAMACPP_CTX` is per slot) |
| `WHISPER_MODEL` | `large-v3-turbo` | `small`, `medium`, `large-v3-turbo`, … |
| `WHISPER_DEVICE` | `cuda` | `cuda` or `cpu` (falls back to CPU on GPU OOM) |
| `WHISPER_BEAM_SIZE` | `5` | 1 is ~5-13% faster but lost words a Domain needs (#66) |
| `LLM_CHUNK_CHARS` | `4000` | Transcript chunk size sent to the extractor |
| `LLM_PROVIDER` | `llamacpp` | `llamacpp` or `ollama` |
| `NOREV_EXPORT_DIR` | `~/Documents/norev` | desktop app: where approved Proposed Actions are exported (`local_document`) |
| `NOREV_INPUT_DEVICE` | system default | desktop app: microphone name, or its number in `python -m norev.adapters.recorder --list-devices` |
