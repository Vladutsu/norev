# norev — meeting transcription + task extraction (CLI)

Transcribes an audio/video file locally with Whisper (faster-whisper) and extracts
project-management tasks (summary, tasks with owner/deadline/priority, decisions, risks)
with a local LLM served by llama.cpp.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Also needed:
- `llama-server` from [llama.cpp](https://github.com/ggml-org/llama.cpp), default `~/Repositories/llama.cpp/build/bin/llama-server`
- a GGUF model, default `~/models/Qwen2.5-7B-Instruct-Q4_K_M.gguf`

## Usage

```bash
./norev.sh <audio_or_video_file> [language]   # e.g. ./norev.sh meeting.mp4 ro
./norev.sh --list-llm-models
```

`norev.sh` reuses a `llama-server` already running on the port, otherwise starts one and
stops it when done. Supported: `.wav .mp3 .m4a .flac .ogg .opus .aac .webm .mp4 .mkv .mov .avi`.

Output:
- `Outputs/Audio/<name>.json` — transcript with timestamped segments and timings
- `Outputs/Json/<name>.pm_tasks.json` — extracted summary, tasks, decisions, risks

## Configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `LLAMACPP_MODEL` | `~/models/Qwen2.5-7B-Instruct-Q4_K_M.gguf` | GGUF model to load |
| `LLAMA_SERVER` | `~/Repositories/llama.cpp/build/bin/llama-server` | llama.cpp server binary |
| `LLAMACPP_PORT` / `LLAMACPP_CTX` / `LLAMACPP_GPU_LAYERS` | `8080` / `4096` / `99` | server options |
| `WHISPER_MODEL` | `large-v3-turbo` | `small`, `medium`, `large-v3-turbo`, … |
| `WHISPER_DEVICE` | `cuda` | `cuda` or `cpu` (falls back to CPU on GPU OOM) |
| `LLM_CHUNK_CHARS` | `4000` | transcript chunk size sent to the LLM |
| `LLM_PROVIDER` | `llamacpp` | `llamacpp` or `ollama` |
