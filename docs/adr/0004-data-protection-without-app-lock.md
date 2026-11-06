# Data protection: encrypted at rest and audited, but no app lock

The Central Processor encrypts everything it stores (Transcripts, Contacts, audio), keeps an audit log of every outbound send (what, where, when, approved by whom), and is the only path for model calls. Keeping the raw audio after the Transcript is produced is a User setting rather than always or never. We deliberately chose **not** to require a password or PIN to open the app: it is single-user on the User's own machine, and the friction was not worth it for now. Consequence: anyone logged into that OS account can open the app; encryption protects against a copied disk, not a shared login.

Remote access (e.g. exposing the app through an ngrok tunnel) is out of scope. If it is added, it must bring authentication with it, which reopens this decision.
