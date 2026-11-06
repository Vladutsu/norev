# AGENTS.md — norev

Rules for any AI agent working in this project. Read this first, every session.
This file lives at the repo root (remote `Vladutsu/norev`, branch `main`); it is committed so everyone on the repo works the same way.

## Hard rules (from the owner — never break these)

1. **Do not modify anything in `docs/` without explicit confirmation.**
   Propose the exact change (diff or full text) and wait for "ok/confirm".
2. **Do not `git commit` (or push) without explicit confirmation.**
   Show `git status` + `git diff --stat` + proposed commit message, then wait.
3. Ask before adding a dependency, changing a public interface, or deleting code/files.
4. Never send Session data (audio, Transcripts, Contacts) to any external service.
   No public LLM APIs, ever (ADR 0001).

## What we are building (shared goals, from `docs/`)

A **local assistant** that records a conversation, transcribes it with speaker
attribution, and turns it into **Proposed Actions** a human approves or rejects
**before anything leaves the machine**. First and only Domain for now: **medical
(family doctor)**, Romanian language.

Flow: record → Live Transcript (feedback only) → stop → Transcript (authoritative,
whole audio, with Speakers) → extraction → User approves/edits/rejects → Connector executes.

Decisions already taken (ADRs in `docs/adr/`):
- **0001** Local-first. The app orchestrates; LLMs run locally (llama.cpp). A
  private-cloud LLM may come later, only through the Central Processor.
- **0002** The User sees and edits the **exact outbound payload** per action before
  approving. No automatic redaction. The approval screen is a security boundary.
- **0003** **Industry-agnostic core**; Roles, Action Types, prompts and Suggestion
  rules live in a pluggable **Domain**. Each Domain picks its own LLM.
- **0004** Encrypted at rest + audit log of every outbound send; no app lock.
  Remote access (ngrok) is out of scope until it brings authentication.

Decided in OPEN-QUESTIONS: desktop shell = **pywebview**; per-Domain model
(medical model not chosen yet; ~4B Q4 fits the 6 GB VRAM target).

**Use the vocabulary in `docs/GLOSSARY.md`** (Session, User, Contact,
Transcript, Speaker, Role, Proposed Action, Suggestion, Connector, Domain,
Action Type, Central Processor) in code names, comments and messages.
Avoid the listed "_Avoid_" synonyms (e.g. say Proposed Action, not task/todo).

## Current state of the code (gap vs goals)

- `LLM/Whisper/` — transcribes an existing file (faster-whisper). No mic
  recording, no diarization (segments have no Speaker).
- `LLM/AI_Model/` — chunked LLM extraction via llama.cpp/ollama. Prompt and
  schema are **project-management**, hard-coded in `prompt.py` → must move into a Domain.
- `extractor.py` `RealtimeTaskExtractor` extracts during transcription — conflicts
  with "extract only from the final Transcript". Open question #1 (recommended: drop it).
- No tests yet.

Open questions (#1–#8, #10 in `OPEN-QUESTIONS.md`) are **not decided**. If a task
depends on one, stop and ask; don't silently pick the recommendation.

## Tech & commands

- Python 3 (dataclasses, type hints, `X | None`), settings read from env vars via
  `Settings.from_environment()` frozen dataclasses. Follow that pattern.
- Setup: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
- Run: `./norev.sh <audio_or_video> [lang]` (starts/reuses `llama-server`)
- Before every push to `main`: `./check.sh` (same checks as CI in `.github/workflows/ci.yml`;
  no GPU or models needed). Push only if it ends with `OK`.
- Module entry: `.venv/bin/python -m LLM.Whisper.whisper ...` (run from the repo root)
- Env vars and defaults: see `README.md`.
- Development runs in the shared container (`docs/ARCHITECTURE.md`, Development environment):
  same image, same commands, same dev data checked against the manifest. Never commit models
  or audio; change dev data only through the manifest.
- Hardware target: single NVIDIA GPU, 6–8 GB VRAM; Whisper and the LLM share it.

## How to work (ponytail + agent-skills)

**Ponytail — lazy senior dev:**
- Smallest change that fully solves the task. No speculative abstractions, no
  config knobs nobody asked for, no new deps if stdlib/existing code does it (YAGNI).
- Prefer deleting code to adding it. Reuse what's in `LLM/` before writing new helpers.
- If you knowingly cut a corner, leave a `# shortcut: <why>` comment so it shows up
  in the debt ledger (`/ponytail-debt`).
- Replies: short, plain, one read. Say what changed and what's left; no essays.

**agent-skills — pick the skill for the phase:**
- Vague feature → `interview-me` / `spec-driven-development` (spec before code).
- Have a spec → `planning-and-task-breakdown`, then `incremental-implementation`
  (thin slices, each verified before the next).
- Logic/bug → `test-driven-development` (failing test first; add pytest when the
  first test is needed — ask before adding the dep).
- Broken → `debugging-and-error-recovery` (reproduce → localize → fix → guard).
- Before proposing a commit → `code-review-and-quality` (or `/ponytail-review`).
- Data handling, Central Processor, Connectors → `security-and-hardening`
  (this is patient health data).
- Architecture decision → `documentation-and-adrs`: draft the ADR, **show it, don't
  write it into `docs/` until confirmed**.
- Commits → `git-workflow-and-versioning`: small atomic commits,
  conventional messages (`feat:`, `fix:`, `docs:`), matching existing history.

**Always:**
- State assumptions before non-trivial work; stop and ask on conflicts between
  code and docs (docs/ADRs win unless the owner says otherwise).
- Touch only what the task needs. No drive-by refactors.
- Verify with evidence (run it, test it) before saying done.

## graphify (optional, local only)

Some machines have a knowledge graph of the repo at `graphify-out/` (gitignored, never commit it).
Skip this section if that folder doesn't exist.

- Read `graphify-out/GRAPH_REPORT.md` before reading source files, grepping, or answering codebase questions.
- For "how does X relate to Y" questions, prefer `graphify query "<question>"`, `graphify path "<A>" "<B>"`
  or `graphify explain "<concept>"` over grep.
- After modifying code, run `graphify update .` (AST only, no LLM). The local post-commit hook also does it.
- INFERRED edges are model guesses: confirm them in the code before relying on them.

## Gotchas

- Files in `docs/` use CRLF; on WSL/Linux they can show as modified with no real
  change (`git diff --ignore-cr-at-eol` is empty). Don't "fix" this without asking.
- `Outputs/`, `.venv/`, `__pycache__/` are gitignored — never commit them.
