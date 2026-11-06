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
