# Recorded LLM responses

Three short, **synthetic** Romanian family-doctor consultations (written for tests, no real
patient) and what the current pipeline's LLM answered for them, so tests and the other laptops
can run the extraction without a model.

| Folder | Speakers | Content |
|---|---|---|
| `consult_1` | doctor, patient | high blood pressure: prescription, home readings, blood tests, ECG, follow-up visit |
| `consult_2` | doctor, patient | chest infection: X-ray referral, antibiotic, sick leave, blood tests, follow-up visit |
| `consult_3` | doctor, patient, family member | diabetes and a foot wound: surgery referral, community nurse, pill box, lab tests |

Each folder:
- `transcript.json`: the Transcript. Session date (2026-11-06), language, Speakers with their
  Role, segments `{start, end, speaker_id, text}`. The times come from the synthetic audio clips
  (dev data, not in git).
- `responses.json`: every LLM call in order: `stage` (`translate`, then `extract`), `prompt`,
  `json_schema` and `response`, the text llama-server returned. The translation is per Speaker
  turn (`norev/adapters/llm/translator.py`, decision #13): several turns per call, each tagged with
  an id, answered as `{"turns": [{"id", "text"}]}`.
- `transcript.en.json`: the English Transcript built from that answer. One segment per Speaker
  turn, with the turn's `speaker_id`, `start` and `end`, and `untranslated` (true when the turn
  kept its original text because the answer left it out).
- `proposed_actions.json`: the generic Proposed Actions the extractor built from those responses.

The Proposed Actions are **what the model produced, not a gold standard**. Known mistakes, kept
on purpose so tests see real output:
- In `consult_1`, the follow-up visit on 13 November is missed, although the translation has it.
- In `consult_3`, the surgery appointment that Ana said she would book is given to the doctor.
- In `consult_1`, the translation says "call 122" where the doctor said 112 ("o sută doisprezece").

The generic extraction still ignores Speakers (it sees only the text); the Domain stage sees
them. Re-recording can change the Proposed Actions a little even at temperature 0.

## Using them in a test

```python
import json
from pathlib import Path
from norev.testing.fakes import FakeLlmClient

recorded = json.loads(Path("tests/fixtures/llm/consult_1/responses.json").read_text(encoding="utf-8"))
llm = FakeLlmClient([call["response"] for call in recorded["calls"]])
```

`python tests/fixtures/llm/record.py --check` replays the responses through the current code,
with no model, and fails if a prompt or a result changed: then the fixtures need re-recording.
`tests/test_adapters_translator.py` replays the translations in `./check.sh`.

## Re-recording (GPU laptop only)

Model: Qwen3-8B Q4_K_M (the `norev.sh` default), llama.cpp `3ed7284`. One slot, so the call
order is deterministic:

```bash
~/Repositories/llama.cpp/build/bin/llama-server -m ~/models/qwen3-8b/Qwen3-8B-Q4_K_M.gguf \
    --host 127.0.0.1 --port 8080 -c 4096 --parallel 1 -ngl 99 -fa on -ctk q8_0 -ctv q8_0 \
    --alias Qwen3-8B-Q4_K_M --reasoning off &
LLM_MODEL=Qwen3-8B-Q4_K_M LLAMACPP_URL=http://127.0.0.1:8080 .venv/bin/python tests/fixtures/llm/record.py
```

## Using the GPU laptop's llama-server from another laptop

**Synthetic data only.** Over the LAN the text leaves your laptop, so a real Session must never
go through it (ADR 0001).

On the GPU laptop, listen on the LAN, without the web chat page, and open the port only to the
teammates' addresses:

```bash
hostname -I          # the address teammates use
~/Repositories/llama.cpp/build/bin/llama-server -m ~/models/qwen3-8b/Qwen3-8B-Q4_K_M.gguf \
    --host 0.0.0.0 --port 8080 -c 16384 --parallel 4 -ngl 99 -fa on -ctk q8_0 -ctv q8_0 \
    --alias Qwen3-8B-Q4_K_M --reasoning off --no-webui
sudo ufw allow from <teammate-ip> to any port 8080 proto tcp   # once per teammate
```

There is no password: the LLM client does not send an API key yet (`LLM/` is frozen in
wave 1), so anyone who can reach the port can use the model. Stop the server when you are done.

On your laptop:

```bash
curl http://<gpu-laptop-ip>:8080/health    # {"status":"ok"}
export LLAMACPP_URL=http://<gpu-laptop-ip>:8080 LLM_MODEL=Qwen3-8B-Q4_K_M WHISPER_DEVICE=cpu
./norev.sh synthetic_clip.wav ro           # sees the running server and uses it for every LLM stage
```
