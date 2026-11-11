# Others contexts (#21)

Two **synthetic** User contexts for the Others Domain, with **handwritten** model answers (not
recorded from a model; recording real ones is #25 on the GPU laptop). Each file:

- `context`: what the User typed about the conversation;
- `instructions_answer`: the answer of the instructions call (`domains/others/prompts/instructions.md`);
- `domain_answer`: the answer of the Domain stage for the shared test Transcript, whose generic
  Proposed Action has id `s1-1`.

Tests check that different contexts give different instructions and the same Proposed Action schema.
