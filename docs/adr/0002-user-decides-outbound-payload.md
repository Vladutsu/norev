# The user decides, per action, exactly what data leaves the machine

When a Proposed Action reaches an external service (e.g. Google Calendar), the Central Processor shows the user the exact payload that would be sent (title, description, attendees), and the user edits it before approving. There is no automatic redaction rule per Action Type. We chose explicit human control over automatic minimisation because what is acceptable to share varies from case to case (e.g. whether a diagnosis may appear in a patient's invite), and a fixed rule would be either too leaky or too restrictive. Consequence: nothing is sent without an approval of that exact payload, and the approval screen is a security boundary, not just UX.

## Considered Options

- **Fixed per-Action-Type minimisation (initials, no diagnosis)**: rejected as too rigid.
- **Send everything**: rejected.
