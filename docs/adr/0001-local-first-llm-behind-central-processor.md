# Local-first: the app orchestrates, LLMs run locally, a private-cloud LLM comes later

The app itself orchestrates the flow (record → transcribe → extract → approve → execute) and calls models; it is not a connector that an external LLM drives. Today the domain LLM runs on the user's machine (llama.cpp), because the conversations contain sensitive data (patient health data) and must not leave the machine by default. Later, heavier work may go to a large model hosted in a **private cloud** the operator controls, never a public LLM API, and that call must go through the Central Processor like any other outbound data.

## Considered Options

- **App as a connector (e.g. MCP server) driven by an external LLM**: rejected for now, because the external model would see raw Transcripts.
- **Public cloud LLM with automatic anonymisation**: rejected, because anonymising free-form medical conversation is unreliable.
