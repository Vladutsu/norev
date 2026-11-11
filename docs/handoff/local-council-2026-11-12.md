# Local council transcript and minutes handoff

This branch builds on Tasca `main` at `a2a9084`. The changes cover Romanian
transcription hints, post-transcription correction, and evidence checks in the
council minutes. The official minutes were used only to score the output.

## Changes

- Whisper gets the council vocabulary as hotwords for Romanian transcription.
  The shared vocabulary includes Ionela Mihai, Cisnădia, and council phrases.
- Post-transcription correction flags unfamiliar words for the model to check
  in context. Model edits still go through the existing safeguards. Three
  exact council phrase substitutions can also run without a model suggestion.
- The minutes distinguish announced attendance from an estimate. The rule
  subtracts one only when the opening gives an expected headcount and names a
  councillor expected online, and later speech identifies that same person as
  having failed to join.
- The vote parser recovers procedural evidence, merges a duplicate agenda
  approval, and can anchor a late timestamp to the exact spoken project title.
  It rejects isolated numbers and partial word or number matches. A repeated
  vote keeps its own evidence and can remain unresolved.
- The prose prompt asks for local attribution from a self-introduction or an
  invitation followed by an actual intervention. It distinguishes expected
  attendance from confirmed presence and forbids treating silence as zero votes.

## Validation

The fully automatic run used the 286 timestamped turns in the supplied Word
transcript of the 27 April 2026 Sibiu extraordinary council meeting. The existing
session-analysis path ran locally with Qwen3-8B Q4_K_M and exported Word. No
manual edits, official PDF input, or fresh Whisper pass were part of this run.
The local output is `runtime/exports/sibiu-2026-04-27-automat.docx` outside the repo.

The automatic output matched seven of the ten selected factual checkpoints
explicitly: meeting type, municipality, chair, agenda approval, rejection of
projects 2 and 3, and adoption of the supplementary project. It scored eight
out of ten when including the attendance estimate of 19, which matches the
official minutes. This score measures the selected facts, not word accuracy,
full vote tallies, or speaker attribution.

The remaining two checkpoints were the public hearing vote and the final
repeat vote on project 1. The transcript provides three abstentions for each.
It does not clearly establish the complete official tallies of 16 in favor
and 3 abstentions, or 15 in favor, 1 against and 3 abstentions, respectively.

## Open issues

- The automatic export retains “Mariu Girigan” from the transcript and leaves
  Marius Ciopa unnamed, even though his name occurs in the lead-in to his
  intervention. Avoid assuming a name applies to every turn with the same
  speaker code.
- Some prose still repeats transcript wording or includes unverified details.
  The export is a draft for review.
- Whisper hotword accuracy was not measured on a fresh transcription of the
  meeting. The automatic benchmark reused the supplied transcript.
- Focused tests cover the council minutes and transcript correction. A prior
  full-suite run in this Windows environment hit four CLI failures. The
  underlying home-directory error was reproduced with the original code.
