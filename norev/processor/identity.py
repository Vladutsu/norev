"""The local identity written as `approved_by` in every AuditEvent (#22).

ADR 0004: one User per install, no login, no app lock. So the identity is not a credential and
proves nothing; it only says, in the audit log, which local account approved the send.
Contract:
- `id` is 1..128 printable characters with no surrounding whitespace (no newlines, so it cannot
  forge audit lines);
- it is the same for every send of an install, and it is fixed when the Central Processor is built,
  never taken from a Domain, a model answer or the payload.
"""
import getpass
from dataclasses import dataclass

MAX_LENGTH = 128


@dataclass(frozen=True)
class LocalUser:
    id: str

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id or self.id != self.id.strip():
            raise ValueError("LocalUser.id must be non-empty with no surrounding whitespace")
        if len(self.id) > MAX_LENGTH or not self.id.isprintable():
            raise ValueError(f"LocalUser.id must be at most {MAX_LENGTH} printable characters")

    @classmethod
    def from_os(cls) -> "LocalUser":
        """The OS account running the app, e.g. "local:ana"."""
        return cls(f"local:{getpass.getuser().strip()}")
