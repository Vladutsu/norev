# norev — Romanian, Local council

A self-contained version of norev with **one language (Romanian)** and **one Domain (Local council)**,
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
2. **Installs only what that tier needs**: CUDA wheels only for NVIDIA; the sherpa-onnx diarization
   (CPU, every tier).
3. **Downloads**, once: Whisper large-v3-turbo, the two sherpa-onnx diarization models (~100 MB), and,
   checked by SHA-256, the tier's GGUF and the matching llama.cpp build (CUDA, Vulkan, Metal or CPU).
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

## Local council

Import the recording of a council meeting (or record one) and review:

- **Proces-verbal (draft)**: written in Romanian from the spoken Transcript, in chronological,
  timestamped sections of ~4000 characters each (one slot with an 8192-token context). Every
  fragment is processed; a failed fragment fails the analysis instead of publishing half the minutes.
- **Hotărâri și voturi**: one pass over every agenda and vote line of the whole meeting. A result is
  kept only when an exact quote from the Transcript announces it; the rest is marked `[de verificat]`.
- **Speakers**: diarization with sherpa-onnx on the CPU, with **no limit on the number of Speakers**
  (a council meeting has 20+; the Sortformer model stops at 4). Each Transcript segment gets a TitaNet
  embedding; segments are grouped at cosine distance 0.60 (`NOREV_DIARIZATION_THRESHOLD`: lower splits
  more Speakers, higher merges more). Speaker Roles (chair, mayor, secretary...) are proposed, and only
  the ones the User confirms are written into the minutes.
- **Exports**: the minutes as an editable Word file; the conversation as Word, text or JSON with
  timestamps and Speaker ids. An edited file is never overwritten.

Names, votes, quorum and adopted decisions must be checked against the recording and the attendance list.

## What differs from the full app

- The app has no mode or language picker; the backend and the CLI accept only `local_council` and `ro`.
- No translation: the minutes are written from the spoken Romanian Transcript.
- `corporate` stays in `domains/` only for the synthetic examples of `--demo`; `medical` and `others`
  live only in `tests/fixtures/domains/`, so the industry-agnostic core keeps its tests (ADR 0003).
- Diarization is sherpa-onnx (`requirements-diarization.txt`), not NeMo: ~30 MB instead of ~6 GB.
  `NOREV_DIARIZER=sortformer` still picks NeMo Sortformer when it is installed by hand.
- llama.cpp is pinned to build `b12249` (`NOREV_LLAMA_BUILD` changes it).

## Checks

`./check.sh` (Windows: `check.cmd`): compile, imports, unit tests. No GPU, model or network needed.
