# Architecture

How the code is layered and tested, so that the ADRs can be checked by tests instead of
relying on discipline. Terms follow `GLOSSARY.md`. This is a proposal: anything marked
"not decided" stays open until it becomes an ADR or is closed in `OPEN-QUESTIONS.md`.

## Principles

### 1. Layers, with the dependency rule enforced by a test

| Layer | Contains | May depend on |
|---|---|---|
| `core` | Session, Transcript, Speaker, Role, Proposed Action (with its states), Suggestion; ports as `typing.Protocol` | nothing outside the standard library, no I/O |
| `usecases` | finalize Transcript, extract generic Proposed Actions, run the Domain stage (Action Types, fields, Roles, Suggestions), approve, run the Workflow | `core` |
| `processor` | Central Processor: model calls, Connector sends, audit log | `core`, `usecases` |
| `adapters` | Whisper, LLM client (llama.cpp/ollama), encrypted store, Connectors | `core` (implement its ports) |
| `domains` | per-industry model, Roles, Action Types, Workflows, prompts (ADR 0005) | nothing; the core loads them |

Ports: `Transcriber`, `Diarizer`, `LlmClient`, `ActionExtractor`, `Connector`, `SessionStore`, `TeamStore`, `AuditLog`, `ExecutionAuditLog`, `ExecutionStore`, `Clock`.

A small pytest walks the imports of `core` and `usecases` and fails on network or
framework modules (`socket`, `urllib`, `requests`, `http`, `faster_whisper`, `webview`, ...)
and on any import from `domains`. This makes two ADRs checkable:
- ADR 0001/0004: only the Central Processor talks to the network or a model.
- ADR 0003: the core contains no industry-specific code.

### 2. Invariants live in the type

- A Proposed Action moves only through `Proposed -> Approved/Rejected -> Executed/Failed`.
  Any other transition raises. Approval stores the exact payload that was approved (ADR 0002).
- Objects that hold patient data have a repr that prints ids and lengths, never content,
  so it cannot leak into logs or tracebacks.

### 3. Raw input is the source of truth, everything else is derived

The Transcript (and the audio, if the User keeps it, ADR 0004) is the source of truth.
Proposed Actions and Suggestions are derived from it and tagged with the Domain and model
that produced them, so they can be regenerated after a prompt or model change without
transcribing again.

### 4. Opaque references, not file paths

The core refers to audio and Transcripts by opaque ids. Only the store adapter knows where
the bytes are and how they are encrypted (ADR 0004), so storage can change in one place.

### 5. One gate for every outbound call

Every model call and every Connector send goes through one function in the Central
Processor. For a Connector it checks that the action is Approved with exactly that payload,
writes the audit entry, then calls out. No other module gets a network client.

### 6. Execution order and failure

For an approved action: write the audit entry, call the Connector, then mark Executed, or
Failed with the error. No automatic retry. A manual Retry reuses the same approved payload,
so it never sends something the User did not see (matches OPEN-QUESTIONS #7).

### 7. A written contract at each seam

Two seams get a written, versioned contract that the core validates against:
- the Domain folder format (OPEN-QUESTIONS #4);
- the LLM output schema for each pass: Roles, Proposed Actions, Suggestions (#5).

## Testing

- **Fakes, not mocks:** a fake `LlmClient` returning recorded responses, a fake
  `Transcriber`, a fixed `Clock`. Extraction, merging and state logic are tested without a
  GPU or llama.cpp.
- **Fixed clock:** relative dates ("next Tuesday") are resolved against a fixed Session date
  in tests (OPEN-QUESTIONS #6).
- **Fixtures:** small text fixtures (Transcripts with expected Proposed Actions, recorded
  LLM responses) live in git under `tests/fixtures/`. Audio clips (synthetic or consented,
  never real patients) are dev data, see below.
- **CI:** GitHub Actions runs `pytest` on CPU only, with no model downloads, plus the import
  test. Model quality is checked manually on a GPU machine.

## Development environment

Five people work on the project at the same time, so development runs in a **shared
container**, not on each machine's own setup. This is for development only: the shipped
desktop app has its own runtime and does not need Docker.

- **One image for everyone.** The container pins the Python version, Python dependencies,
  CUDA runtime, llama.cpp build and Whisper model version. "Works on my machine" is a
  container bug, fixed in the image, not locally.
- **Same entry point.** The backend is run, and tests are run, with the same commands inside
  the container for everyone. Paths, environment variables and defaults come from the
  container config, not from each person's shell.
- **Dev data is not in git.** Models and audio clips are too large. They live in a shared
  location and are mounted into the container at a fixed path (e.g. `/data`), read-only.
- **Same input for everyone.** A manifest committed in git lists every dev data file with
  its relative path and checksum. A script checks (and downloads, if missing) the files
  against the manifest before the backend or tests run, so everyone's backend sees exactly
  the same input. Changing dev data means changing the manifest in a reviewed commit.
- **One laptop runs the models.** Only one team laptop has the GPU for the LLMs and diarization. Everything else is built and tested without a model: fakes and recorded LLM responses in `tests/fixtures/`. To try the real model, point `LLAMACPP_URL` at that laptop's llama-server, with synthetic data only.
- **GPU is optional.** Tests that need a GPU or a real model are marked and skipped when
  none is available; the rest run the same on any machine and in CI.

Not decided: Docker Compose vs devcontainer, where the shared data is hosted, and the
manifest format.

## Frontend (only if the pywebview UI uses React + TypeScript; not decided)

```
ui/src/
  app/                      routes, theme
  features/<name>/          api.ts, types.ts, validation.ts (+ .test.ts), components/, pages/
  shared/api/               client wrapping the pywebview js_api bridge
  shared/ui/                common components
```

Features: `sessions`, `contacts`, `actions` (the approval screen), `suggestions`. Approval
rules (required fields block approval, OPEN-QUESTIONS #6) live in
`features/actions/validation.ts` and are unit-tested.

## Deliberately not used

| What | Why |
|---|---|
| Microservices, API gateway, message queues | One user, one process, one machine |
| Docker in the shipped app, server database (Postgres, MySQL) | Desktop app; an embedded store is enough. Store choice not decided. Docker is used for development only, see above |
| HTTP API, Swagger | The UI talks to Python through the pywebview bridge |
| Mocking frameworks | Handwritten fakes for a handful of ports are simpler and stay honest |

## Proposed package layout (not decided)

```
norev/
  core/          models, Proposed Action states, ports. No I/O.
  usecases/      finalize, assign_roles, extract, approve, execute
  processor/     Central Processor, audit log
  adapters/      whisper/, llm/, store/, connectors/
  domains/       medical/
  ui/            pywebview shell (+ frontend if React)
  tests/         fixtures/ (small, in git), fakes, import test
  data.manifest  dev data paths + checksums (data itself not in git)
```

The current `LLM/Whisper` and `LLM/AI_Model` become adapters. That move is a refactor of its
own and can happen one module at a time once the ports exist.
