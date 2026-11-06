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
6 GB target in `agent.md` they are at the limit (the desktop also uses some VRAM), so use
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

## Usage

```bash
./norev.sh session.mp4                          # auto-detect the language
./norev.sh session.mp4 ro                       # force Romanian
NOREV_LANGUAGES=ro,en,de ./norev.sh session.mp4 # pick the most likely of these languages
NOREV_DATE=2026-11-06 ./norev.sh session.mp4    # Session date for "Friday", "13 November"...
./norev.sh --list-llm-models
```

Supported: `.wav .mp3 .m4a .flac .ogg .opus .aac .webm .mp4 .mkv .mov .avi`.

### Recording from a microphone

```bash
./norev.sh --record                             # record (Enter stops), then transcribe + extract
./norev.sh --record ro                          # same, forcing Romanian
python -m LLM.Recorder.record --list-devices    # input device names
python -m LLM.Recorder.record                   # record only -> Outputs/Recordings/session-<time>.wav
python -m LLM.Recorder.record out.wav --seconds 60 --device "Microphone Array (Realtek(R) Audio)"
```

Records 16 kHz mono WAV (what Whisper uses; about 115 MB per hour) through PyAV's FFmpeg capture
devices, so no extra dependency: DirectShow on Windows, ALSA on Linux (`default` or `plughw:C,D`).
Stop with Enter or Ctrl+C; the audio so far is always kept, and the file stays valid even if the
process is killed. The PyAV Linux wheel has no PulseAudio support, so inside WSL there is usually no
capture device: record on Windows (`python -m LLM.Recorder.record`) and transcribe the WAV.
The WAV is not encrypted yet (ADR 0004).

Output:
- `Outputs/Recordings/session-<time>.wav` — Session audio, when recorded with `--record`
- `Outputs/Audio/<name>.json` — Transcript in the spoken language, with timestamped segments
- `Outputs/Audio/<name>.en.json` — English translation (when translated)
- `Outputs/Json/<name>.proposed_actions.json` — `session_summary`, `proposed_actions`, `decisions`, `risks`

The Transcript has no Speaker attribution yet, so "I'll do it" cannot be tied to a person: such
Proposed Actions get no owner rather than a guessed one.

## Before pushing

```bash
./check.sh
```

Same checks as CI (`.github/workflows/ci.yml`, runs on every push to `main`): shell syntax,
Python compiles, every module imports, unit tests in `tests/` if any. No GPU or models needed.
To run CI on a branch: GitHub, Actions, CI, Run workflow.

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `NOREV_LANGUAGES` | empty (auto-detect) | Whisper language codes to choose from, e.g. `ro,en,de` |
| `NOREV_TRANSLATE` | `auto` | `auto` translates non-English Sessions, `0` extracts from the original language |
| `LLAMACPP_MODEL` | Qwen3-8B (see above) | default model for both LLM stages |
| `NOREV_TRANSLATE_MODEL` | `$LLAMACPP_MODEL` | GGUF for the translation stage |
| `NOREV_EXTRACT_MODEL` | `$LLAMACPP_MODEL` | GGUF for the Proposed Action extraction stage |
| `LLM_PARALLEL` | `4` | concurrent requests (llama-server slots) per stage |
| `LLM_SECOND_PASS` | `0` | `1`: second look for missed Proposed Actions (more found, but more wrong owners; ~3x slower extraction) |
| `LLM_TEMPERATURE` | `0` | sampling temperature |
| `NOREV_DATE` | today | Session date used to resolve relative dates |
| `LLAMA_SERVER` | `~/Repositories/llama.cpp/build/bin/llama-server` | llama.cpp server binary |
| `LLAMACPP_PORT` / `LLAMACPP_CTX` / `LLAMACPP_GPU_LAYERS` | `8080` / `4096` / `99` | server options (`LLAMACPP_CTX` is per slot) |
| `WHISPER_MODEL` | `large-v3-turbo` | `small`, `medium`, `large-v3-turbo`, … |
| `WHISPER_DEVICE` | `cuda` | `cuda` or `cpu` (falls back to CPU on GPU OOM) |
| `LLM_CHUNK_CHARS` | `4000` | Transcript chunk size sent to the extractor |
| `LLM_PROVIDER` | `llamacpp` | `llamacpp` or `ollama` |
| `NOREV_INPUT_DEVICE` | system default | microphone for `--record` (see `--list-devices`) |
