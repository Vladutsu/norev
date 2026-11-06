# Evaluation of the generic extraction (#25)

Synthetic Sessions in Romanian (written for tests, no real person), with what should be extracted
written down by a person, **separately from what the model answered**. The recordings only make the
evaluation repeatable without a model; they are never the expected result.

| Session | Domain | What it tests |
|---|---|---|
| `corporate_1` | Corporate | sprint planning: commitments by name, a reassignment ("someone should..." → Maria), a cancelled idea (PayPal), amounts |
| `corporate_2` | Corporate | client call: actions of the client, an action with no deadline, prices and quantities |
| `corporate_3` | Corporate | incident review: "I'll do it", a rejected proposal, figures |
| `others_1` | Others | a real-estate agent with a client, with the User's context (`user_context`) |
| `others_2` | Others | a teacher with a parent, with the User's context |
| `consult_1..3` | Medical (example) | the 3 consultations of `../llm`, kept as regressions; not a clinical validation |

Each folder:
- `transcript.json`: the Transcript with Speakers. For the consultations, `expected.json` points to `../llm`.
- `expected.json`:
  - the Proposed Actions that must be found: keywords, accepted owners, accepted dates;
  - the ideas that must not appear (`not_actions`);
  - the numbers that must survive the translation;
  - the known cases;
  - `verified_by`: who checked the file (null until a person did);
  - `notes`: every change to the expectations, with the reason.
- `translation.json`, `extract.neutral.json`, `extract.speakers.json`: the recorded calls and results.

`variance.json`: the Proposed Actions of extra runs on the same English Transcript, because temperature
0 still varies between runs. `report.json`: the scores. `thresholds.json`: the limits, confirmed before the final evaluation; `follow_ups` names the issue of each limit not met.

```bash
python tests/fixtures/eval/evaluate.py check        # no model; also in ./check.sh (tests/test_eval_scoring.py)
python tests/fixtures/eval/evaluate.py report       # the table
python tests/fixtures/eval/evaluate.py record       # GPU laptop with llama-server running (as ../llm/README.md)
python tests/fixtures/eval/evaluate.py variance 2   # GPU laptop: 2 more extract runs per variant
```

## Rules

- **Expected results are not changed to make the model pass.** A wrong expectation can be fixed, with
  a line in `notes` saying why. A model mistake stays in the report, and its known case stays OUTSTANDING
  until a prompt or model change solves it.
- CI checks that the recordings replay and give the same report. It does not fail when the model
  misses a threshold: that is what the report is for.
- What is counted, separately:
  - omissions;
  - invented actions;
  - wrong or missing owners;
  - wrong or missing dates;
  - cancelled ideas listed as actions;
  - numbers changed by the translation. Spelled-out numbers count as kept ("two hundred ten" = 210).

## Follow-up #40 verification — 2026-11-06

The adapter now checks an explicit "I will take care of the <object>" commitment against
both the action title and its quoted request. It assigns a name only when a separate,
explicitly addressed request and accepting reply establish that Speaker's name unambiguously.
Ambiguous identities, conditional commitments and unrelated actions retain the existing checks.
The generic prompt, human-verified expectations and thresholds are unchanged.

Fresh local GPU recording: Qwen3-8B Q4_K_M, llama.cpp `3ed7284`, RTX 4070 Laptop GPU;
5,051 MiB GPU memory observed (not a peak measurement). Recorded times total 84 seconds for
translation, 103 seconds for neutral extraction and 101 seconds for Speaker-label extraction.
All eight Sessions were re-recorded once; temperature 0 does not guarantee identical later runs.

The production neutral variant meets every confirmed Corporate/Others threshold: owner wrong
9.1%, date wrong 9.1%, omissions 9.1%, invented 4.3%, and no cancelled actions listed.
Three expected owners remain missing and are counted separately. Numbers changed: 0/31 across
Corporate/Others and Medical. All three named regression cases are solved in both variants.
The experimental Speaker-label variant still fails the owner threshold (22.7%); it is not the
production prompt. Medical remains an example, not clinical validation.

`bash check.sh` passes 443 tests (one skipped); `record.py --check` and `evaluate.py check` pass.
Raw model responses remain in the recordings; corrected owners are stored in the derived results.
