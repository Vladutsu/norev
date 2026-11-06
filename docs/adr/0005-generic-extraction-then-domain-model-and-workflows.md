# Two stages: the core extracts generic Proposed Actions, each Domain specialises them with its own model and Workflows

The base app is the same for every industry: it records, produces the Transcript and extracts **generic** Proposed Actions from it (what has to be done, by whom, by when, and the Transcript excerpt it came from) with a general-purpose local LLM. This is what the current pipeline already does. A Domain then takes the Transcript and those generic Proposed Actions and, with **its own specialised model** (e.g. a medical model), gives each one an Action Type and its Domain fields (e.g. medication and dose), drops what does not fit the Domain, and adds Domain Suggestions. Each Action Type has a **Workflow**: the ordered steps that run after the User approves the Proposed Action (e.g. *Schedule Visit*: build the invite, send it through the Google Calendar Connector, save the result to the Session). Every step that sends data out goes through the Central Processor with exactly the payload the User approved (ADR 0002).

We split it this way because the generic stage can be built and tested without any Domain, a new Domain only adds its model, Action Types and Workflows, and a Domain model only has to be good at its industry, not at finding actions in free conversation. Consequence: two LLM stages per Session (generic, then Domain), run one after the other with one model loaded at a time, as today. OPEN-QUESTIONS #5 now concerns the passes inside the Domain stage.

## Considered Options

- **One Domain-specific extraction straight from the Transcript**: rejected, because every Domain would re-implement action finding, and each Domain model would also need to be good at it.
- **Generic extraction only, no Domain model**: rejected, because Domain fields (medication, dose, visit type) and Domain Suggestions need Domain knowledge.
