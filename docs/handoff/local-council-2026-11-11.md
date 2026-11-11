# Handoff — Local council minutes (Consiliu local), 11.11.2026

State: everything below is on Tasca `main` (`github.com/David-Eti23/Tasca`). The council work came in
through PR #1 (`consiliu-local`, which also made Local council the only Domain and added two-step
Speaker grouping); the transcript correction and this handoff were added on top of it afterwards.
The old branch `feat/local-council-minutes` is no longer needed.

## What was added

1. **Local council Domain** (`domains/local_council/`): roles, follow-up action type, Romanian minutes
   (proces-verbal) generated from the spoken transcript, exported by the app as an editable Word file.
   The conversation exports as Word, text or JSON with timestamps and speaker ids. Exports never
   overwrite an edited file (an export with the same name and different content is refused).
2. **Diarization for any number of speakers** (`norev/adapters/diarization/sherpa.py`): sherpa-onnx on the
   CPU, one TitaNet embedding per transcript segment, average-linkage cosine clustering at 0.60.
   Picked with `NOREV_DIARIZER` (`sortformer` | `sherpa` | `none`; default: the first installed).
   `norev.cmd setup-models` downloads the models into `models\diarization`.
3. **Automatic transcript correction after the meeting** (`norev/usecases/transcript_autocorrect.py`):
   on the first analysis the large model reads the transcript in fragments with the Domain's
   `vocabulary.txt` (names and terms) and returns the lines it fixes. The code keeps, word by word, only
   punctuation/capitals, a non-Romanian word (wordfreq Zipf < 1) replaced by a real word spelled almost
   the same, or a misheard name replaced by one from the list. Numbers, negations, real words and
   paraphrases are never changed. The corrected text replaces the spoken transcript (the raw Whisper text
   is not kept); a Suggestion tells the User how many lines changed.
4. **Minutes by agenda**: header (meeting type, municipality, attendance, chair) from exact quotes;
   Deschiderea ședinței / Dezbateri / one section per item voted with its debate and, under it, the
   announced result and the exact announcement. Votes are read in one pass over every vote line of the
   meeting (never cut at a fragment boundary); a result is kept only if a quote said inside that item's
   time span states it, otherwise `[de verificat]`. Fragments never see result announcements.
   Prose fragments are ~4000 chars (fits an 8k context); a copied transcript is retried, then flagged.
   Only speaker Roles the User confirmed are written; speaker codes are removed from the prose.

Dependencies: `wordfreq==3.1.1` (`requirements.txt`, used by the transcript correction) and
`sherpa-onnx` (`requirements-diarization.txt`, installed and checked by `install`/`doctor`).
Diarization on `main` groups Speakers in two steps (see commit 6586ff2: long segments clustered finely,
close groups merged, short replies joined to the nearest Speaker), which replaced the one-cut
clustering described in point 2.

## How it was tested

- Unit suite on `main` with the correction: 847 tests, 1 skipped (`python -m unittest discover -s tests`).
- End to end on the laptop (RTX, Qwen3-8B Q4_K_M, 8192 ctx, Whisper large-v3-turbo) on the Sibiu
  council meeting of 27.04.2026 (YouTube `VFU0kWLjdjw`, 51 min). Harness outside the repo:
  `..\runtime\run_e2e.py` + `..\runtime\rerun.cmd` (logs `rerun*.log`, exports in `..\runtime\exports\`).
  Transcription 3.5 min, diarization ~1 min, correction + minutes ~21 min.
- Compared with the official minutes (sibiu.ro, Proces_Verbal_Sedinta_Consiliu_Local_27.04.2026.pdf):
  - Match: extraordinary meeting, 21 councillors, chair Zeno-Karl Pinter, the 3 projects + the extra item
    with their initiators, outcomes (P1 adopted, P2 rejected, P3 rejected, extra item unanimous), the
    repeat vote on P1, the order of the public speakers, the content of the speeches.
  - Wrong or missing in ours: attendance 20 (officially 19, Barac did not connect); opening attributes
    the thanks to the mayor (it was Pinter); the repeat vote is "neclar" and its quotes include
    non-announcements (officially 15/1/3); agenda vote and the procedural vote (16 for, 3 abstentions)
    are not recorded; vote counts for P2/P3 are not given; P1 "13 voturi pentru" is what was said, the
    official record says 15; speakers stay anonymous; two fragments are `[de verificat]`; leftover
    misheard words in the prose (Ciznădia, domnul Din, Abaraf, confețiunile).
  - Never in the audio (the clerk fills them in): start/end time, convocation order no., HCL numbers,
    who voted how.

## Known issues / next steps

1. Vote filter: keep only real announcements; include the agenda vote and procedural votes.
2. Attendance corrected later in the meeting (someone who does not connect).
3. Name speakers when the chair introduces them ("domnul Forir", "doamna Costan Teodora").
4. `vocabulary.txt`: add PSD councillor Ionela Mihai (replaced Florian Giubega) and inflected town
   names (Cisnădia). Each council replaces the list with its own.
5. Whisper VAD drops ~6 of 51 min, including a speaker's introduction (42:53–43:26): make it gentler.
6. Correction overwrites the raw Whisper text: consider keeping it for audit.
7. Speaker roles proposed by the model are mostly wrong (only the first 6000 chars are read); they stay
   proposals until the User confirms them.
