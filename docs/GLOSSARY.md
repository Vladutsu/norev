# norev

A local assistant that records a conversation, transcribes it with speaker attribution, and turns it into Proposed Actions a human approves or rejects before anything leaves the machine. The core is industry-agnostic; each industry plugs in as a Domain.

## Language

### Configuration

**Domain**:
The industry the app is configured for (e.g. medical, journalism), bundling its speaker Roles, Action Types and extraction instructions. The core never contains industry-specific knowledge.
_Avoid_: Vertical, plugin, profile, mode

**Action Type**:
A kind of action a Domain allows to be proposed, such as *Schedule Visit* (Programare vizită) or *Medication Reminder* (Luat pastilă).
_Avoid_: Task type, command, intent

### Conversation

**Session**:
One recorded conversation from start to stop, with everything derived from it. In the medical Domain this is a consultation; in journalism, an interview.
_Avoid_: Meeting, recording, consultation (as a core term)

**User**:
The single professional who operates the app and records Sessions (e.g. the family doctor, the journalist). Also a Speaker in every Session.
_Avoid_: Operator, account, doctor (as a core term)

**Contact**:
The person the User talks with in a Session, chosen or created before recording starts and kept in a local address book (e.g. the Patient, the Interviewee).
_Avoid_: Client, subject, patient (as a core term)

**Live Transcript**:
The provisional text shown while a Session is being recorded, with approximate Speaker attribution. Only for feedback; nothing is extracted from it.
_Avoid_: Draft transcript, preview

**Transcript**:
The authoritative text of a Session, produced after recording stops from the whole audio, attributed to Speakers. Proposed Actions and Suggestions are extracted only from it.
_Avoid_: Transcription, minutes, final transcript

**Speaker**:
A distinct voice detected in a Session, before it is known who it is.
_Avoid_: Participant, user

**Role**:
What a Speaker is in the conversation, as defined by the Domain (e.g. Doctor, Patient, Caregiver; Interviewer, Interviewee). Inferred from content and correctable by the user.
_Avoid_: Speaker type, persona

### Outcomes

**Proposed Action**:
A concrete action extracted from a Transcript, together with the Transcript excerpt it came from, waiting for the User's decision; the User may edit its details before approving. Moves through *Proposed → Approved / Rejected → Executed / Failed*.
_Avoid_: Task, suggestion, recommendation, todo

**Connector**:
The integration that executes an approved Proposed Action, either external (e.g. Google Calendar, a publishing platform) or local (e.g. saving a document). Each Action Type has one Connector.
_Avoid_: Integration, plugin, adapter, executor

**Suggestion**:
Read-only advice derived from a Transcript (e.g. a possible inconsistency or a recommendation) that the user only reads; it is never approved or executed.
_Avoid_: Insight, observation, recommendation, alert

**Central Processor**:
The single local gate every piece of Session data passes through, including every model call, and the only place from which data may leave the machine.
_Avoid_: Gateway, broker, security layer
