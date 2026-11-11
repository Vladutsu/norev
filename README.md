# norev — Romanian, Corporate

A self-contained version of norev with **one language (Romanian)** and **one Domain (Corporate)**,
which sets itself up for the laptop it is installed on. Everything still runs locally (ADR 0001):
the only network traffic is the one-time download of models and llama.cpp.

## Install

| | |
|---|---|
| Windows | double-click `norev.cmd` (the first time it runs `install.cmd`; Python 3.12 is installed with winget if missing) |
| Linux | `./norev-app.sh` (the first time it runs `./install.sh`; needs `python3.12` + `python3.12-venv`) |

`install.cmd --no-speakers` / `./install.sh --no-speakers` skips diarization (smaller download,
Transcript without Speakers). The first run then does, with no questions asked:

1. **Detects the hardware** (RAM, GPU vendor and VRAM) and picks a tier, saved in
   `%LOCALAPPDATA%\norev\hardware.json` (Linux: `~/.local/share/norev/hardware.json`).
2. **Installs only what that tier needs**: CUDA wheels only for NVIDIA, a CPU-only torch for
   diarization elsewhere (~200 MB instead of ~6 GB).
3. **Downloads**, once and checked by SHA-256: Whisper large-v3-turbo, the diarization model, the
   tier's GGUF and the matching llama.cpp build (CUDA, Vulkan, Metal or CPU).
4. **Checks that the llama.cpp build runs** on this machine; if it does not (e.g. no Vulkan driver),
   it installs the CPU build instead.

Run `norev.cmd setup` again after changing hardware: it detects again and downloads only what is missing.
`norev.cmd doctor` shows the chosen tier.

## Hardware tiers

| Laptop | Tier | LLM | llama.cpp | Whisper large-v3-turbo |
|---|---|---|---|---|
| NVIDIA ≥ 8 GB VRAM | `gpu` | Qwen3-8B Q4_K_M, 4 slots | CUDA | GPU, float16 |
| NVIDIA 4–6 GB VRAM | `gpu-small` | Qwen3-8B, split GPU/RAM by llama.cpp `--fit` | CUDA | GPU, float16 / int8_float16 |
| AMD/Intel dedicated GPU | `gpu-small` | Qwen3-8B | Vulkan | CPU, int8 |
| Integrated GPU, ≥ 12 GB RAM | `integrated` | Qwen3-8B, 2 slots | Vulkan (Metal on Mac) | CPU, int8 |
| Integrated GPU, 8 GB RAM | `integrated` | **Qwen3-4B-Instruct-2507** Q4_K_M | Vulkan | CPU, int8 |
| No GPU | `cpu` | Qwen3-8B, or 4B under 12 GB RAM | CPU | CPU, int8 |

Accuracy is the same as the reference setup (Qwen3-8B, Whisper large-v3-turbo, beam 5) on every
tier except the **8 GB RAM** machines, which get the 4B model. Without an NVIDIA GPU everything
is slower, but the model and settings that produce the result are the same. Thresholds and rules: `norev/launcher/hardware.py`.
Any variable set in the environment (README of the full app) still overrides the saved tier.

## What differs from the full app

- The UI has no mode or language picker; the backend and the CLI accept only `corporate` and `ro`.
- Translation RO → EN stays on: the extraction prompts are in English.
- `medical` and `others` Domains live only in `tests/fixtures/domains/`, so the industry-agnostic
  core keeps its tests (ADR 0003).
- llama.cpp is pinned to build `b12249` (`NOREV_LLAMA_BUILD` changes it).

## Checks

`./check.sh` (Windows: `check.cmd`): compile, imports, unit tests. No GPU, model or network needed.
