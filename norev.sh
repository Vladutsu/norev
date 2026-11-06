#!/usr/bin/env bash
# Transcribe a recorded Session and extract Proposed Actions, all locally.
#
#   1. Whisper        transcript in the spoken language (preselected list or auto-detection)
#   2. Diarization    who spoke when (NVIDIA Sortformer), if NeMo is installed (requirements-diarization.txt)
#   3. LLM translator  transcript -> English, per Speaker turn when there are Speakers (skipped for English)
#   4. LLM extractor   English Transcript -> Session summary, Proposed Actions, decisions, risks
#   5. Speaker owners  "I'll do it" -> the Speaker's name, only if that Speaker spoke before and was named
#
# Each LLM stage can use its own GGUF model. Models are loaded one at a time, so a stage only
# needs the GPU memory of its own model; consecutive stages with the same model share one server.
#
# Usage: ./norev.sh <audio_or_video_file> [language]
#        NOREV_LANGUAGES=ro,en,de ./norev.sh session.mp4
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
# Qwen3-8B for both stages: best translation and extraction in the test Sessions, and one model
# means no reload between stages
LLAMACPP_MODEL="${LLAMACPP_MODEL:-$HOME/models/qwen3-8b/Qwen3-8B-Q4_K_M.gguf}"
NOREV_EXTRACT_MODEL="${NOREV_EXTRACT_MODEL:-$LLAMACPP_MODEL}"
NOREV_TRANSLATE_MODEL="${NOREV_TRANSLATE_MODEL:-$LLAMACPP_MODEL}"
# auto: translate unless Whisper heard English | 0: always extract from the original language
NOREV_TRANSLATE="${NOREV_TRANSLATE:-auto}"
# auto: diarize when NeMo is installed | 1: required | 0: off (no Speakers, as before)
NOREV_DIARIZE="${NOREV_DIARIZE:-auto}"
# Comma-separated Whisper language codes to choose from, e.g. ro,en,de. Empty: auto-detect any.
export WHISPER_LANGUAGES="${NOREV_LANGUAGES:-${WHISPER_LANGUAGES:-}}"
# Session date (YYYY-MM-DD) used to resolve "Friday", "13 November"... Empty: today.
export LLM_SESSION_DATE="${NOREV_DATE:-${LLM_SESSION_DATE:-}}"

LLAMACPP_PORT="${LLAMACPP_PORT:-8080}"
# 4096 fits one transcript chunk + prompt and leaves room for Whisper on an 8 GB GPU
LLAMACPP_CTX="${LLAMACPP_CTX:-4096}"
LLAMACPP_GPU_LAYERS="${LLAMACPP_GPU_LAYERS:-99}"
# Chunks are independent, so translation and extraction send this many requests at once; the GPU
# batches them, which is what keeps 30-60 minute Sessions fast. Each slot gets LLAMACPP_CTX tokens.
export LLM_PARALLEL="${LLM_PARALLEL:-4}"

export LLM_PROVIDER="${LLM_PROVIDER:-llamacpp}"
export LLAMACPP_URL="${LLAMACPP_URL:-http://127.0.0.1:${LLAMACPP_PORT}}"
# Grammar-constrained JSON output; without it smaller models sometimes emit invalid JSON
export LLM_FORCE_JSON_FORMAT="${LLM_FORCE_JSON_FORMAT:-1}"
# Process each transcript chunk on its own and merge in Python; feeding the accumulated
# JSON back into every prompt overflows the context on long recordings
export LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT="${LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT:-0}"
# Deterministic output: the same recording gives the same Proposed Actions
export LLM_TEMPERATURE="${LLM_TEMPERATURE:-0}"
# Second look per chunk for missed Proposed Actions, marked [verifică]. Off by default: in tests it
# found 2 of 3 missed ones but gave both the wrong owner, and it triples extraction time on long Sessions
export LLM_SECOND_PASS="${LLM_SECOND_PASS:-0}"

PYTHON="$SCRIPT_DIR/.venv/bin/python"
[[ -x "$PYTHON" ]] || die ".venv missing. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"

model_name() { basename "$1" .gguf; }

# ── llama.cpp server: one model at a time ─────────────────────────────────────

LLAMA_PID=""
LLAMA_LOADED=""
EXTERNAL_SERVER=0
if curl -sf "$LLAMACPP_URL/health" >/dev/null 2>&1; then
    EXTERNAL_SERVER=1
    warn "Using llama-server already running at $LLAMACPP_URL for every stage"
fi

stop_llama_server() {
    [[ -n "$LLAMA_PID" ]] || return 0
    log "Stopping llama-server ($(model_name "$LLAMA_LOADED"))"
    kill "$LLAMA_PID" 2>/dev/null || true
    wait "$LLAMA_PID" 2>/dev/null || true
    LLAMA_PID=""
    LLAMA_LOADED=""
}
trap stop_llama_server EXIT INT TERM

# Usage: use_model <model.gguf>  — starts llama-server with it unless it is already loaded
use_model() {
    local model="$1"
    export LLM_MODEL="$(model_name "$model")"
    [[ "$EXTERNAL_SERVER" == 1 || "$LLAMA_LOADED" == "$model" ]] && return 0
    stop_llama_server

    [[ -x "$LLAMA_SERVER" ]] || die "llama-server not found at $LLAMA_SERVER. Set LLAMA_SERVER."
    [[ -f "$model" ]] || die "GGUF model not found at $model."
    local extra=()
    # Qwen3 "thinks" before answering by default, which only slows down translation/extraction
    [[ "$(basename "$model")" == *Qwen3* ]] && extra=(--reasoning off)

    local log_file="$SCRIPT_DIR/Outputs/llama-server.log"
    mkdir -p "$(dirname "$log_file")"
    log "Starting llama-server ($(model_name "$model")), log: $log_file"
    "$LLAMA_SERVER" -m "$model" \
        --host 127.0.0.1 --port "$LLAMACPP_PORT" \
        -c "$(( LLAMACPP_CTX * LLM_PARALLEL ))" --parallel "$LLM_PARALLEL" -ngl "$LLAMACPP_GPU_LAYERS" \
        -fa on -ctk q8_0 -ctv q8_0 \
        --alias "$LLM_MODEL" "${extra[@]}" >>"$log_file" 2>&1 &
    LLAMA_PID=$!
    LLAMA_LOADED="$model"

    for _ in $(seq 1 120); do
        curl -sf "$LLAMACPP_URL/health" >/dev/null 2>&1 && return 0
        kill -0 "$LLAMA_PID" 2>/dev/null || die "llama-server exited during startup. See $log_file"
        sleep 1
    done
    die "llama-server did not become ready. See $log_file"
}

if [[ "$1" == --* ]]; then
    use_model "$NOREV_EXTRACT_MODEL"
    "$PYTHON" -m LLM.Whisper.whisper "$@"
    exit 0
fi

DIARIZE=0
if [[ "$NOREV_DIARIZE" != 0 ]]; then
    if "$PYTHON" -c 'import nemo' 2>/dev/null; then
        DIARIZE=1
    elif [[ "$NOREV_DIARIZE" == 1 ]]; then
        die "NOREV_DIARIZE=1 but NeMo is not installed: .venv/bin/pip install -r requirements.txt -r requirements-diarization.txt"
    fi
fi

# ── 1. Whisper ─────────────────────────────────────────────────────────────────

log "[1/5] Transcribing $1 (Whisper: ${WHISPER_MODEL:-large-v3-turbo}, languages: ${2:-${WHISPER_LANGUAGES:-auto}})"
# Word times let diarization split a Whisper segment that holds several Speakers
TRANSCRIPT="$(WHISPER_SKIP_LLM=1 WHISPER_WORD_TIMESTAMPS="$DIARIZE" "$PYTHON" -m LLM.Whisper.whisper "$@")"
LANGUAGE="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["detected_language"] or "")' "$TRANSCRIPT")"
log "Transcript ($LANGUAGE) written to $TRANSCRIPT"

# ── 2. Diarization ─────────────────────────────────────────────────────────────

SPEAKERS=0
if [[ "$DIARIZE" == 1 ]]; then
    log "[2/5] Diarization (NVIDIA Sortformer, at most 4 Speakers)"
    # NeMo logs a lot; the stage prints the number of Speakers as its last line
    SPEAKERS="$("$PYTHON" -m LLM.pipeline diarize "$TRANSCRIPT" 2>>"$SCRIPT_DIR/Outputs/diarization.log" | tail -n 1)"
    log "$SPEAKERS Speakers"
else
    log "[2/5] Diarization skipped (NOREV_DIARIZE=$NOREV_DIARIZE, NeMo installed: no or not asked)"
fi

# ── 3. Translation ─────────────────────────────────────────────────────────────

if [[ "$NOREV_TRANSLATE" != 0 && "$LANGUAGE" != en ]]; then
    use_model "$NOREV_TRANSLATE_MODEL"
    log "[3/5] Translating $LANGUAGE -> en (LLM: $LLM_MODEL)"
    TRANSCRIPT="$("$PYTHON" -m LLM.pipeline translate "$TRANSCRIPT")"
    log "Translation written to $TRANSCRIPT"
else
    log "[3/5] Translation skipped (language: $LANGUAGE)"
fi

# ── 4. Proposed Action extraction ──────────────────────────────────────────────

use_model "$NOREV_EXTRACT_MODEL"
log "[4/5] Extracting Proposed Actions (LLM: $LLM_MODEL)"
PROPOSED_ACTIONS="$("$PYTHON" -m LLM.pipeline extract "$TRANSCRIPT")"

# ── 5. Owners from named Speakers ──────────────────────────────────────────────

if [[ "$SPEAKERS" -ge 2 ]]; then
    log "[5/5] Naming Speakers and owners of \"I'll do it\" Proposed Actions (LLM: $LLM_MODEL)"
    ASSIGNED="$("$PYTHON" -m LLM.pipeline owners "$TRANSCRIPT" "$PROPOSED_ACTIONS")"
    log "$ASSIGNED Proposed Actions got the name of the Speaker who took them on"
else
    log "[5/5] Speaker owners skipped (fewer than 2 Speakers)"
fi
stop_llama_server

"$PYTHON" -m LLM.pipeline show "$TRANSCRIPT" "$PROPOSED_ACTIONS"
