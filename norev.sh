#!/usr/bin/env bash
# Transcribe a recorded Session and extract Proposed Actions, all locally, into the encrypted store.
#
#   1. Whisper + diarization  Transcript with Speakers in the spoken language (python -m norev.session transcribe)
#   2. LLM                    translation per Speaker turn, generic extraction, Domain stage (... analyse)
#   3. show                   the Proposed Actions waiting for review (... show)
#
# llama-server starts only after Whisper and diarization finished, so they never share the GPU;
# each stage runs on its own GGUF model, one at a time (#62).
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
# Qwen3-8B for translation and extraction: best in the test Sessions, and the same model means no
# reload between them. The Domain stage uses the Domain's [model] (domain.toml).
LLAMACPP_MODEL="${LLAMACPP_MODEL:-$HOME/models/qwen3-8b/Qwen3-8B-Q4_K_M.gguf}"
NOREV_EXTRACT_MODEL="${NOREV_EXTRACT_MODEL:-$LLAMACPP_MODEL}"
NOREV_TRANSLATE_MODEL="${NOREV_TRANSLATE_MODEL:-$LLAMACPP_MODEL}"
# auto: translate unless Whisper heard English | 0: always extract from the original language
NOREV_TRANSLATE="${NOREV_TRANSLATE:-auto}"
# Comma-separated Whisper language codes to choose from, e.g. ro,en,de. Empty: auto-detect any.
export WHISPER_LANGUAGES="${NOREV_LANGUAGES:-${WHISPER_LANGUAGES:-}}"
# Session date (YYYY-MM-DD) used to resolve "Friday", "13 November"... Empty: today.
export LLM_SESSION_DATE="${NOREV_DATE:-${LLM_SESSION_DATE:-}}"
# corporate (default) or medical; others needs a confirmed prompt (#21). Unknown names are an error.
NOREV_DOMAIN="${NOREV_DOMAIN:-}"
# 1: Speakers by diarization (requirements-diarization.txt) | 0: one Transcript without Speakers
NOREV_SPEAKERS="${NOREV_SPEAKERS:-1}"

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
# Sessions are kept only in the encrypted store (ADR 0004), never as plaintext files
[[ "$1" == --* || -n "${NOREV_STORE_KEY_FILE:-}" ]] || die "NOREV_STORE_KEY_FILE is not set. Create a key: $PYTHON -m norev.adapters.store new-key <path outside the store folder>"

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

# ── 1. Transcript with Speakers (Whisper + diarization, no LLM) ───────────────

log "[1/3] Transcribing $1 (Whisper: ${WHISPER_MODEL:-large-v3-turbo}, languages: ${2:-${WHISPER_LANGUAGES:-auto}}, Domain: ${NOREV_DOMAIN:-corporate})"
TRANSCRIBE_ARGS=()
[[ -n "${2:-}" ]] && TRANSCRIBE_ARGS+=(--language "$2")
[[ -n "${NOREV_DOMAIN:-}" ]] && TRANSCRIBE_ARGS+=(--domain "$NOREV_DOMAIN")
[[ -n "$LLM_SESSION_DATE" ]] && TRANSCRIBE_ARGS+=(--date "$LLM_SESSION_DATE")
[[ "$NOREV_SPEAKERS" == 0 ]] && TRANSCRIBE_ARGS+=(--no-speakers)
SESSION="$("$PYTHON" -m norev.session transcribe "$1" "${TRANSCRIBE_ARGS[@]}")"
log "Session $SESSION transcribed into the encrypted store"

# ── 2-3. Translation per Speaker turn, extraction and Domain stage ─────────────

# analyse starts llama-server itself with each stage's model, one at a time (#62): translation,
# extraction, then the Domain's [model] (domain.toml, looked up under NOREV_MODELS_DIR)
export LLAMA_SERVER NOREV_TRANSLATE_MODEL NOREV_EXTRACT_MODEL LLAMACPP_PORT LLAMACPP_CTX LLAMACPP_GPU_LAYERS
export NOREV_MODELS_DIR="${NOREV_MODELS_DIR:-$HOME/models}"
export LLM_MODEL="$NOREV_EXTRACT_MODEL"
[[ -x "$LLAMA_SERVER" ]] || die "llama-server not found at $LLAMA_SERVER. Set LLAMA_SERVER."
ANALYSE_ARGS=()
[[ "$NOREV_TRANSLATE" == 0 ]] && ANALYSE_ARGS+=(--no-translate)
log "[2/3] Translating ($(model_name "$NOREV_TRANSLATE_MODEL")), extracting Proposed Actions ($(model_name "$NOREV_EXTRACT_MODEL")) and running the Domain stage"
"$PYTHON" -m norev.session analyse "$SESSION" "${ANALYSE_ARGS[@]}"

log "[3/3] Result"
"$PYTHON" -m norev.session show "$SESSION"
