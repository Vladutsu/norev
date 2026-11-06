# Shared dev container (docs/ARCHITECTURE.md, Development environment). How to use it: devenv/README.md
#   target dev  CPU only, no models: ./check.sh and the tests, on every laptop
#   target gpu  CUDA runtime + llama.cpp at a fixed commit, models mounted at /models (laptop 4 only)
# The repo is not copied in: docker-compose.yml mounts it at /workspace.
ARG CUDA_VERSION=12.8.1

# ── dev ───────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS dev
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY requirements.txt /tmp/requirements.txt
# The venv sits where norev.sh and check.sh look for it (.venv in the repo); CUDA wheels only in gpu, like CI
RUN python -m venv /workspace/.venv \
    && grep -v '^nvidia-' /tmp/requirements.txt > /tmp/requirements-cpu.txt \
    && /workspace/.venv/bin/pip install --no-cache-dir -r /tmp/requirements-cpu.txt
ENV PATH=/workspace/.venv/bin:$PATH \
    NOREV_DATA_DIR=/data \
    WHISPER_DEVICE=cpu
ENTRYPOINT ["bash", "/workspace/devenv/entrypoint.sh"]
CMD ["./check.sh"]

# ── llama.cpp build ───────────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu24.04 AS llamacpp
# v0.9.0; norev.sh needs a recent server (--reasoning off, -fa on)
ARG LLAMACPP_COMMIT=cd959bae99ff4bed22bef2604f45241c09bbb8c8
# Turing, Ampere, Ada, Blackwell. Set only laptop 4's to build faster (devenv/README.md)
ARG CUDA_ARCHITECTURES="75;86;89;120"
RUN apt-get update && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git init -q /src && cd /src \
    && git fetch -q --depth 1 https://github.com/ggml-org/llama.cpp "$LLAMACPP_COMMIT" \
    && git checkout -q FETCH_HEAD
# Static binary, no web UI (nothing downloaded at build time), no HTTPS (the server only listens)
RUN cmake -S /src -B /build -DCMAKE_BUILD_TYPE=Release \
        -DGGML_CUDA=ON -DGGML_NATIVE=OFF -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCHITECTURES" \
        -DBUILD_SHARED_LIBS=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_OPENSSL=OFF \
        -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
    && cmake --build /build --config Release --target llama-server -j"$(nproc)"

# ── gpu ───────────────────────────────────────────────────────────────────────
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu24.04 AS gpu
RUN apt-get update && apt-get install -y --no-install-recommends python3.12 python3.12-venv curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /workspace
COPY requirements.txt /tmp/requirements.txt
RUN python3.12 -m venv /workspace/.venv \
    && /workspace/.venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt
COPY --from=llamacpp /build/bin/llama-server /opt/llama.cpp/llama-server
ENV PATH=/workspace/.venv/bin:$PATH \
    NOREV_DATA_DIR=/data \
    LLAMA_SERVER=/opt/llama.cpp/llama-server \
    LLAMACPP_MODEL=/models/qwen3-8b/Qwen3-8B-Q4_K_M.gguf \
    HF_HOME=/models/huggingface
ENTRYPOINT ["bash", "/workspace/devenv/entrypoint.sh"]
CMD ["./check.sh"]
