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
   checked by SHA-256, Qwen3-4B-Instruct-2507 Q4_K_M and the matching llama.cpp build (CUDA, Vulkan, Metal or CPU).
4. **Checks that the llama.cpp build runs** on this machine; if it does not (e.g. no Vulkan driver),
   it installs the CPU build instead.

Run `norev.cmd setup` again after changing hardware or upgrading an installation with only the 8B model.
Setup detects hardware again and downloads missing assets.
`norev.cmd doctor` shows the chosen tier.

## Hardware tiers

Hardware detection selects the llama.cpp backend and Whisper device. Council analysis defaults to
Qwen3-4B-Instruct-2507 Q4_K_M with one 8,192-token slot across tiers. Environment settings override defaults.

## Local council

Import the recording of a council meeting (or record one) and review:

- **Proces-verbal (draft)**: Qwen3-4B reads the corrected Romanian transcript in overlapping windows,
  extracts timestamped notes and uses them to draft chronological sections. The app checks the rendered
  prompt against 65% of the llama-server context before sending each request.
- **Hotărâri, voturi și prezență**: vote counts are linked to source turns inside the corresponding vote
  interval. Attendance is checked separately. Missing or conflicting evidence is marked `[de verificat]`.
- **Speakers**: diarization with sherpa-onnx on the CPU, with **no limit on the number of Speakers**
  (a council meeting has 20+; the Sortformer model stops at 4). Each Transcript segment gets a TitaNet
  embedding; the longer segments are grouped, groups whose mean voices are within 0.40 become one
  Speaker (`NOREV_SPEAKER_MERGE`: lower splits more Speakers, higher merges more), and short replies
  ("Da.", "5.") join the nearest Speaker instead of becoming one. On the 51-min Sibiu meeting of
  27.04.2026 this finds 15 Speakers and keeps the 14 people identified from the content apart, for any
  merge value from 0.35 to 0.45 (a single distance cut found 33 and put two people together).
  Speaker Roles (chair, mayor, secretary...) are proposed for review and used in follow-up extraction.
  The draft uses confirmed roles when attributing debate notes.
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
  Set `NOREV_VOICE_PROFILES` to the path of a local TitaNet profile JSON to enable speaker names
  with Sherpa on newly transcribed sessions. The file contains `model_sha256`, `dimension` (192)
  and `profiles` with `speaker_name` and a normalized `embedding` for each person. Segments
  with weak, ambiguous, conflicting, or too-short matches keep their anonymous speaker label.
- llama.cpp is pinned to build `b12249` (`NOREV_LLAMA_BUILD` changes it).

## Checks

`./check.sh` (Windows: `check.cmd`): compile, imports, unit tests. No GPU, model or network needed.
