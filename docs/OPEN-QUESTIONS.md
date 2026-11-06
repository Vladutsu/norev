# Open questions

Raised during design but not yet decided. Recommendations are proposals, not decisions.

1. **Existing realtime extraction.** `LLM/AI_Model/extractor.py` (`RealtimeTaskExtractor`) runs the LLM while Whisper transcribes, which conflicts with extracting only from the final Transcript. Recommended: drop it and keep only chunking for long Transcripts.
2. **Meaning of "ngrok".** Remote access from the User's other devices, multiple users, or something else? Recorded as out of scope in ADR 0004 until clarified.
3. **Encryption key storage.** Recommended: OS credential store (Windows Credential Manager / DPAPI).
4. **How a Domain is defined.** Recommended: declarative (a folder with YAML + prompt files); Connectors stay code in the core.
5. **LLM passes after recording stops.** Recommended: three separate passes (Role assignment, Proposed Actions, Suggestions) rather than one call.
6. **Relative dates and missing fields.** Recommended: resolve relative dates against the Session date; required fields left empty block approval, and the LLM never invents values.
7. **Failed execution.** Recommended: Failed shows the error and offers a manual Retry; no automatic retry (avoids duplicate invites).
8. **Session history.** Recommended: past Sessions can be reopened, and Proposed Actions still pending can be decided later.
9. **Desktop shell.** Decided: pywebview. Deadline still unknown.
10. **Medical LLM.** Decided that each Domain picks its own model; the medical model is not chosen yet (MedGemma 4B was the candidate, to test on a Romanian Transcript; 6 GB VRAM limits it to ~4B Q4).

## Known gaps in the current code

- No microphone recording: `LLM/Whisper` only takes an existing file.
- No speaker diarization: segments have no Speaker.
- Project-management prompt and task schema are hard-coded in `LLM/AI_Model/prompt.py`.
