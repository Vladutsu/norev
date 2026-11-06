# norev — meeting transcription + task extraction (CLI)

Transcribes a meeting recording locally and extracts project-management tasks (summary, tasks with
owner/deadline/priority, decisions, risks). Everything runs on the machine: Whisper (faster-whisper)
for speech and local LLMs served by llama.cpp.

Pipeline:

1. **Whisper** transcribes in the spoken language: a preselected list or auto-detection.
2. **Translator LLM** translates the transcript to English (skipped when the meeting is in English).
3. **Extractor LLM** extracts the tasks from the English transcript.

Each LLM stage can use its own GGUF model (default: Qwen3-8B for both, so it is loaded once). Models
are loaded one at a time, so each stage only needs the GPU memory of its own model.

Reliability and speed:
- Output is constrained to a JSON schema, so a model cannot answer `{}` or rename fields.
- The extraction prompt carries a calendar of the coming weeks, so "by Monday" resolves to the right date.
- Transcript chunks are processed 4 at a time (`llama-server --parallel 4`); a 30-minute meeting takes
  under 2 minutes end to end on an RTX 4070 Laptop GPU.
- Tasks repeated across chunks are merged by owner and title similarity.
- Optional second pass (`LLM_SECOND_PASS=1`) looks for missed tasks; tasks it adds are marked
  `[verifică]` because they more often have the wrong owner.
- Temperature 0: the same recording gives the same tasks.

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
./norev.sh meeting.mp4                          # auto-detect the language
./norev.sh meeting.mp4 ro                       # force Romanian
NOREV_LANGUAGES=ro,en,de ./norev.sh meeting.mp4 # pick the most likely of these languages
NOREV_DATE=2026-11-06 ./norev.sh meeting.mp4    # meeting date for "Friday", "13 November"...
./norev.sh --list-llm-models
```

Supported: `.wav .mp3 .m4a .flac .ogg .opus .aac .webm .mp4 .mkv .mov .avi`.

Output:
- `Outputs/Audio/<name>.json` — transcript in the spoken language, with timestamped segments
- `Outputs/Audio/<name>.en.json` — English translation (when translated)
- `Outputs/Json/<name>.pm_tasks.json` — summary, tasks, decisions, risks

The transcript has no speaker labels, so "I'll do it" cannot be tied to a person: such tasks get
no owner rather than a guessed one.

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `NOREV_LANGUAGES` | empty (auto-detect) | Whisper language codes to choose from, e.g. `ro,en,de` |
| `NOREV_TRANSLATE` | `auto` | `auto` translates non-English meetings, `0` extracts from the original language |
| `LLAMACPP_MODEL` | Qwen3-8B (see above) | default model for both LLM stages |
| `NOREV_TRANSLATE_MODEL` | `$LLAMACPP_MODEL` | GGUF for the translation stage |
| `NOREV_EXTRACT_MODEL` | `$LLAMACPP_MODEL` | GGUF for the task extraction stage |
| `LLM_PARALLEL` | `4` | concurrent requests (llama-server slots) per stage |
| `LLM_SECOND_PASS` | `0` | `1`: second look for missed tasks (more found, but more wrong owners; ~3x slower extraction) |
| `LLM_TEMPERATURE` | `0` | sampling temperature |
| `NOREV_DATE` | today | meeting date used to resolve relative dates |
| `LLAMA_SERVER` | `~/Repositories/llama.cpp/build/bin/llama-server` | llama.cpp server binary |
| `LLAMACPP_PORT` / `LLAMACPP_CTX` / `LLAMACPP_GPU_LAYERS` | `8080` / `4096` / `99` | server options (`LLAMACPP_CTX` is per slot) |
| `WHISPER_MODEL` | `large-v3-turbo` | `small`, `medium`, `large-v3-turbo`, … |
| `WHISPER_DEVICE` | `cuda` | `cuda` or `cpu` (falls back to CPU on GPU OOM) |
| `LLM_CHUNK_CHARS` | `4000` | transcript chunk size sent to the extractor |
| `LLM_PROVIDER` | `llamacpp` | `llamacpp` or `ollama` |
