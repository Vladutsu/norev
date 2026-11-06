# Domains

A Domain is everything industry-specific (ADR 0003): its model, Roles, Action Types with their
fields and Workflows, and the prompt for the Domain stage (ADR 0005). The core knows nothing about
any industry; it loads a Domain from a folder here. Terms follow `docs/GLOSSARY.md`.

To add a Domain, copy `medical/`, rename the folder, change the files, and run `./check.sh`:
`tests/test_domains_format.py` checks every folder against this page.

## Folder

```
domains/<name>/
  domain.toml                       model, Roles, Action Types, Workflows, prompt paths
  prompts/domain_stage.md           prompt for the Domain stage
  prompts/domain_stage.schema.json  JSON schema of the Domain stage's answer
```

`<name>` is snake_case and equals `name` in `domain.toml`. Files are UTF-8. Prompts are in English
(the pipeline extracts from the English translation of the Transcript); `label` values are what
the User sees, in the Domain's language.

## domain.toml

Top level:

| Key | Type | |
|---|---|---|
| `format` | int | always `1`; raised when this format changes |
| `name` | string | snake_case, same as the folder |
| `label` | string | shown to the User |

`[model]`: the model for the Domain stage, loaded instead of the generic model while this stage runs.

| Key | Type | |
|---|---|---|
| `gguf` | string | GGUF file name, or a path to it |
| `context` | int | context size in tokens |

`[prompts]`: paths relative to the Domain folder.

| Key | |
|---|---|
| `domain_stage` | the prompt template |
| `domain_stage_schema` | the JSON schema the answer is constrained to |
| `instructions` | `others` only (#21): the prompt that writes the instructions from the User's context; `{{context}}` is its only placeholder |
| `instructions_schema` | `others` only: the JSON schema of that answer, `{"instructions": "<text>"}` |

`[[roles]]`: one table per Role a Speaker can have, at least one.

| Key | Type | |
|---|---|---|
| `name` | string | snake_case, unique |
| `label` | string | shown to the User |
| `description` | string | optional; who this is |

`[[action_types]]`: one table per Action Type, at least one.

| Key | Type | |
|---|---|---|
| `name` | string | snake_case, unique; the value of `action_type` in the model's answer |
| `label` | string | shown to the User |
| `description` | string | when a Proposed Action is of this type; the model sees it |

`[[action_types.fields]]`: the Domain fields of the Action Type above it, at least one.

| Key | Type | |
|---|---|---|
| `name` | string | snake_case, unique within the Action Type |
| `label` | string | shown to the User |
| `type` | string | `string`, `date` (YYYY-MM-DD) or `time` (HH:MM, 24-hour) |
| `required` | bool | `true`: an empty value blocks approval until the User fills it (OPEN-QUESTIONS #6) |
| `description` | string | optional; what goes in it; the model sees it |

`[[action_types.workflow]]`: the Workflow of the Action Type above it, in run order, at least one
step. It runs only after the User approves the Proposed Action.

| Key | Type | |
|---|---|---|
| `name` | string | snake_case, unique within the Workflow |
| `label` | string | shown to the User |
| `connector` | string | optional; the one Connector this step uses |
| `sends` | list of strings | required with `connector`, not allowed without: the payload fields this step sends |

`sends` may name the Action Type's fields and the generic fields of a Proposed Action: `title`,
`description`, `owner`, `due_date`, `transcript_excerpt`. Steps with a Connector send through the
Central Processor exactly the payload the User approved (ADR 0002); steps without one run locally.

Known Connectors (core code, not part of a Domain):

| Connector | Does |
|---|---|
| `google_calendar` | creates events in the User's Google Calendar |
| `email_draft` | saves an email draft (.eml) on this machine; the User sends it from their mail app |
| `local_document` | saves a document on this machine |

A new Connector is added in the core first, then to this table and to `KNOWN_CONNECTORS` in the test.

## Prompt: prompts/domain_stage.md

Plain text. The core replaces these placeholders before calling the model:

| Placeholder | Replaced with | |
|---|---|---|
| `{{transcript}}` | the Transcript, one line per segment, with its Speaker | required |
| `{{proposed_actions}}` | the generic Proposed Actions as a JSON list (`id`, `title`, `description`, `owner`, `due_date`, `transcript_excerpt`) | required |
| `{{session_date}}` | the Session date, YYYY-MM-DD | optional |
| `{{roles}}` | `name: description` of every Role | optional |
| `{{action_types}}` | every Action Type with its description and fields (name, type, description) | optional |
| `{{user_instructions}}` | `others` only (#21): the instructions the User confirmed, filled before the others; exactly once | required in `others` |

Any other `{{...}}` is an error. Single braces are left as they are, so JSON examples need no escaping.

The prompt must never ask the model to invent a value: what the Transcript does not say is `null`,
and the User fills required fields before approving (OPEN-QUESTIONS #6).

## Answer: prompts/domain_stage.schema.json

The model's output is constrained to this schema. It must be:

```json
{
  "proposed_actions": [
    {"id": "<id of the generic Proposed Action>", "action_type": "<Action Type name>", "fields": {"<field>": "<value or null>"}}
  ],
  "suggestions": [
    {"text": "...", "transcript_excerpt": "..."}
  ]
}
```

- `proposed_actions.items` is an `anyOf` with one object per Action Type, whose `action_type` is a
  `const` with that Action Type's name.
- Its `fields` lists every field of that Action Type in `properties` and in `required`, with
  `additionalProperties: false`. Every field allows `null`, so the model can say "not said"
  instead of making a value up. `date` fields have the pattern `^[0-9]{4}-[0-9]{2}-[0-9]{2}$`,
  `time` fields `^[0-9]{2}:[0-9]{2}$`.
- A generic Proposed Action left out of the answer does not fit the Domain and is dropped.
- A field whose schema is exactly `{"type": "null"}` is filled only by the User (e.g. attendee
  emails, since a Speaker is not a Contact): the core keeps it null whatever the model answers.

The answer is untrusted: the core validates it again against `domain.toml` before showing it.

## Others: the User's prompt (#21)

`others` keeps its Action Type, fields, Workflow and schema fixed; only `{{user_instructions}}`
comes from the User (`norev/usecases/others_prompt.py`):

1. The User describes the conversation: the context, at most **2000 characters** (`MAX_CONTEXT`);
   an empty or longer one is refused before any model call.
2. The local model writes instructions from `prompts/instructions.md`, given only the context
   (no audio, Contact or other Session). The answer must be non-empty, at most **4000 characters**
   (`MAX_INSTRUCTIONS`) and contain no `{{` or `}}`; otherwise nothing changes and the User retries.
3. The User sees the full prompt, edits the instructions (same rules) and confirms. The Session
   keeps, encrypted, the context, the instructions and that exact prompt. If this template changes
   later, the Session's prompt is refused until the User confirms again; it is never regenerated
   silently. A new prompt for an analysed Session needs an explicit regeneration, which keeps decided
   Proposed Actions.
