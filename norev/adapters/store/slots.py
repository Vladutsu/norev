"""Key slots (ADR 0006): the store's data key, encrypted once per User password and per Invitation code.

  slots = KeySlots(path)
  slots.add("user", email, password, key)        a User's slot (replaces theirs)
  key = slots.unlock("user", email, password)    SignInFailed for an unknown email or a wrong password
  slots.add("invitation", email, code, key, expires=...)   InvitationExpired once it is past

The file sits next to the store, outside it, because it is what opens it. It holds no email and no
key in clear: a slot is named by a hash of its kind and email, and holds the data key sealed with
AES-256-GCM under a key derived from the secret with scrypt; the slot name is the associated data,
so a slot copied under another name does not open. Every write replaces the whole file atomically.
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KINDS = ("user", "invitation")
SCRYPT_N = 2 ** 15  # ~0.1 s and 32 MiB per try: cheap for one sign-in, slow for guessing
SCRYPT_R, SCRYPT_P = 8, 1
_NONCE_BYTES, _SALT_BYTES = 12, 16


class SignInFailed(Exception):
    """No slot for this email, or the secret does not open it. Deliberately the same error for both."""


class InvitationExpired(Exception):
    """The Invitation's code would open it, but its time is past."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def slot_id(kind: str, email: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown slot kind {kind!r}")
    return f"{kind}:" + hashlib.sha256(f"norev-slot\0{email.strip().lower()}".encode()).hexdigest()[:32]


def _derive(secret: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(secret.encode(), salt=salt, n=n, r=r, p=p, maxmem=256 * n * r, dklen=32)


class KeySlots:
    def __init__(self, path: Path, *, n: int = SCRYPT_N) -> None:
        self.path = Path(path)
        self.n = n

    def __repr__(self) -> str:
        return f"KeySlots({self.path.name})"

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("slots"), dict):
            raise ValueError(f"{self.path} is not a norev key slots file")
        return data["slots"]

    def _save(self, slots: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "slots": slots}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def empty(self) -> bool:
        """No User slot yet: the next sign-up is the first one and creates the Team."""
        return not any(name.startswith("user:") for name in self._load())

    def has(self, kind: str, email: str) -> bool:
        return slot_id(kind, email) in self._load()

    def add(self, kind: str, email: str, secret: str, key: bytes, *, expires: datetime | None = None) -> None:
        if not isinstance(secret, str) or not secret:
            raise ValueError("a slot needs a secret")
        name = slot_id(kind, email)
        salt, nonce = os.urandom(_SALT_BYTES), os.urandom(_NONCE_BYTES)
        sealed = AESGCM(_derive(secret, salt, self.n, SCRYPT_R, SCRYPT_P)).encrypt(nonce, key, name.encode())
        slots = self._load()
        slots[name] = {"salt": _b64(salt), "n": self.n, "r": SCRYPT_R, "p": SCRYPT_P, "nonce": _b64(nonce),
                       "sealed": _b64(sealed), "expires": expires.astimezone(timezone.utc).isoformat() if expires else None}
        self._save(slots)
        if self.unlock(kind, email, secret, now=None) != key:  # read back before it counts
            raise OSError("the key slot does not read back")

    def unlock(self, kind: str, email: str, secret: str, *, now: datetime | None = None) -> bytes:
        """The data key. `now` checks an Invitation's expiry (pass None only to skip it on purpose)."""
        name = slot_id(kind, email)
        slot = self._load().get(name)
        if slot is None or not isinstance(secret, str):
            raise SignInFailed()
        try:
            derived = _derive(secret, base64.b64decode(slot["salt"]), slot["n"], slot["r"], slot["p"])
            key = AESGCM(derived).decrypt(base64.b64decode(slot["nonce"]), base64.b64decode(slot["sealed"]), name.encode())
        except InvalidTag:
            raise SignInFailed() from None
        if now is not None and slot["expires"] and now >= datetime.fromisoformat(slot["expires"]):
            raise InvitationExpired()
        return key

    def remove(self, kind: str, email: str) -> None:
        slots = self._load()
        if slots.pop(slot_id(kind, email), None) is not None:
            self._save(slots)
