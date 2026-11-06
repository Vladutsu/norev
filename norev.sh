#!/usr/bin/env bash
# Transcribe an audio/video file with Whisper and extract PM tasks with llama.cpp.
#
# Usage: ./norev.sh <audio_or_video_file> [language]
#        ./norev.sh --list-llm-models
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'
log()  { echo -e "${GREEN}[norev]${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[norev]${NC} $*" >&2; }
die()  { echo -e "${RED}[norev] ERROR:${NC} $*" >&2; exit 1; }

[[ $# -ge 1 ]] || die "Usage: ./norev.sh <audio_or_video_file> [language]"

# ── config (override from the environment) ────────────────────────────────────

LLAMA_SERVER="${LLAMA_SERVER:-$HOME/Repositories/llama.cpp/build/bin/llama-server}"
LLAMACPP_MODEL="${LLAMACPP_MODEL:-$HOME/models/Qwen2.5-7B-Instruct-Q4_K_M.gguf}"
LLAMACPP_PORT="${LLAMACPP_PORT:-8080}"
# 4096 fits one transcript chunk + prompt and leaves room for Whisper on an 8 GB GPU
LLAMACPP_CTX="${LLAMACPP_CTX:-4096}"
LLAMACPP_GPU_LAYERS="${LLAMACPP_GPU_LAYERS:-99}"

export LLM_PROVIDER="${LLM_PROVIDER:-llamacpp}"
export LLAMACPP_URL="${LLAMACPP_URL:-http://127.0.0.1:${LLAMACPP_PORT}}"
export LLM_MODEL="${LLM_MODEL:-$(basename "$LLAMACPP_MODEL" .gguf)}"
# Grammar-constrained JSON output; without it smaller models sometimes emit invalid JSON
export LLM_FORCE_JSON_FORMAT="${LLM_FORCE_JSON_FORMAT:-1}"
# Process each transcript chunk on its own and merge in Python; feeding the accumulated
# JSON back into every prompt overflows the context on long recordings
export LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT="${LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT:-0}"

PYTHON="$SCRIPT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || die ".venv missing. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

# ── llama.cpp server: reuse a running one, otherwise start and stop our own ──

LLAMA_PID=""
cleanup() {
    if [[ -n "$LLAMA_PID" ]]; then
        log "Stopping llama-server (PID $LLAMA_PID)"
        kill "$LLAMA_PID" 2>/dev/null || true
        wait "$LLAMA_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

if curl -sf "$LLAMACPP_URL/health" >/dev/null 2>&1; then
    log "Using llama-server already running at $LLAMACPP_URL"
else
    [[ -x "$LLAMA_SERVER" ]] || die "llama-server not found at $LLAMA_SERVER. Set LLAMA_SERVER."
    [[ -f "$LLAMACPP_MODEL" ]] || die "GGUF model not found at $LLAMACPP_MODEL. Set LLAMACPP_MODEL."

    LOG_FILE="$SCRIPT_DIR/Outputs/llama-server.log"
    mkdir -p "$(dirname "$LOG_FILE")"
    log "Starting llama-server ($(basename "$LLAMACPP_MODEL")), log: $LOG_FILE"
    "$LLAMA_SERVER" -m "$LLAMACPP_MODEL" \
        --host 127.0.0.1 --port "$LLAMACPP_PORT" \
        -c "$LLAMACPP_CTX" -ngl "$LLAMACPP_GPU_LAYERS" \
        --alias "$LLM_MODEL" >"$LOG_FILE" 2>&1 &
    LLAMA_PID=$!

    for _ in $(seq 1 120); do
        curl -sf "$LLAMACPP_URL/health" >/dev/null 2>&1 && break
        kill -0 "$LLAMA_PID" 2>/dev/null || die "llama-server exited during startup. See $LOG_FILE"
        sleep 1
    done
    curl -sf "$LLAMACPP_URL/health" >/dev/null 2>&1 || die "llama-server did not become ready. See $LOG_FILE"
fi

# ── run ────────────────────────────────────────────────────────────────────────

if [[ "$1" != --* ]]; then
    log "Transcribing $1 (Whisper: ${WHISPER_MODEL:-large-v3-turbo}, LLM: $LLM_MODEL)"
fi
OUTPUT="$("$PYTHON" -m LLM.Whisper.whisper "$@")"

if [[ "$1" == --* ]]; then
    echo "$OUTPUT"
    exit 0
fi

log "Transcript + tasks written to $OUTPUT"
"$PYTHON" - "$OUTPUT" <<'EOF'
import json, sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
print()
print(
    f"Language: {data.get('detected_language')} | device: {data.get('runtime_device')}"
    f" | transcription: {data.get('transcription_elapsed_seconds')}s"
    f" | tasks: {data.get('pm_tasks_elapsed_seconds')}s"
)
if data.get("pm_tasks_error"):
    print("\nTask extraction failed:", data["pm_tasks_error"])
    sys.exit(1)
pm_file = data.get("pm_tasks_output_file")
pm = json.load(open(pm_file, encoding="utf-8")) if pm_file else {}
print("Tasks file:", pm_file)
if pm.get("meeting_summary"):
    print("\nSummary:\n ", pm["meeting_summary"])
tasks = pm.get("tasks") or []
print(f"\nTasks ({len(tasks)}):")
for task in tasks:
    who = task.get("assignee") or task.get("owner") or "-"
    due = task.get("due_date") or task.get("deadline") or "-"
    print(f"  - [{task.get('priority', '-')}] {task.get('title')}  (who: {who}, due: {due})")
for key in ("decisions", "risks"):
    items = pm.get(key) or []
    if items:
        print(f"\n{key.capitalize()}:")
        for item in items:
            print("  -", item if isinstance(item, str) else json.dumps(item, ensure_ascii=False))
EOF
