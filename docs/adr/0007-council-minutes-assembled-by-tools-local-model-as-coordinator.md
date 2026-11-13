# Council minutes are assembled by deterministic tools; the local model coordinates and only rewrites spoken turns

The 18 official 2026 minutes of the Sibiu local council show what the document actually is: a cleaned
near-verbatim record, not a summary. In the long meetings 60–70% of the word trigrams of the minutes occur in
the recording itself; every intervention carries its speaker label ("Dl consilier X:", "Dna primar …",
"Dl Secretar General …"), and every agenda item repeats one fixed block: the item number, its full title and
initiator, "Cine este pentru? Împotrivă? Abțineri?", the announced result, and "S-a adoptat Hotărârea … nr.
N/2026 …, cu 22 de voturi «pentru»". The titles come from the published agenda, and the vote tally and the
decision number come from the voting system and the decision register: in the room the chair often says only
"Unanimitate." Meanwhile, the summaries our model writes are exactly where it invents content (a "strategy for
the agricultural sector" debate that never happened), and detecting items from the Transcript alone missed 5
of the 24 projects of 30.04.2026 because Whisper glued two items into one segment.

So the minutes are assembled by a fixed pipeline of tools, each deterministic and testable on its own:
`agenda` (the published items, read before the meeting), `align_agenda` (where each known title is read in the
Transcript, also inside a longer segment), `identify_speakers` (diarization and the opt-in voice profiles),
`vote_evidence` (the announced result with the turn quoted), `hcl_register` (decision number and tally, from
the published register or entered by the User), `roster` (people, functions and their labels), `render`
(the fixed template) and `verify` (every name, number and result has evidence, otherwise "[de verificat]").
The local model has one writing task, `clean_turn`: rewrite one real spoken turn in written form without
adding content. It also coordinates where the fixed order does not fit (an extra agenda, a change of chair,
a question for the User) and applies the User's edits after the meeting ("adaugă că X a ieșit din sală") by
calling the same tools. The tools have a tool-call interface (MCP-style), but they stay inside the app: as in
ADR 0001 the app orchestrates and calls the local model, no external LLM drives it, and anything that leaves
the machine still goes through the Central Processor with the payload the User approved (ADR 0002).

Consequences: the 18 official minutes become the evaluation set (`compare_pv.py`): items found, results
right/unclear/wrong, speakers named, near-verbatim overlap; a change ships only if it does not make any of
them worse. The app needs two inputs it does not have yet, the agenda and the decision register; until then
it asks the User and leaves "[de verificat]". Free-form "Dezbateri" paragraphs disappear from the default
minutes. The council path proposes no actions for now (follow-ups came out as votes and agenda items).

## Considered Options

- **The model writes the minutes, tools only check them** (the current pipeline): rejected, the checks
  cannot remove invented debate and cannot add what the audio never says (tallies, decision numbers).
- **A free agent loop: the local model decides every tool call for the whole meeting**: rejected for
  generation, a 4–8B local model drifts over 50 items and the result is hard to reproduce or test; kept only
  for exceptions and post-meeting edits, where the loop is short.
- **No model at all, raw Transcript plus template**: rejected, the official minutes are cleaned (punctuation,
  false starts, misheard names), and the User would rewrite every turn by hand.
