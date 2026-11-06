"""The store key on Windows (#63): sealed with DPAPI, migrated once, backed up only on purpose.

  create_protected_key   a new random key, written only as a DPAPI blob
  protect_existing       a plain key file (or a backup) sealed into the DPAPI blob, after checking
                         that it opens the existing store; the plain file is never touched here
  export_backup          the raw key to a place the User chose, outside norev's folder

Every write is exclusive (never overwrites) and is read back and checked before it counts.
"""
import os
from pathlib import Path

from norev.adapters.store import dpapi
from norev.adapters.store.encrypted import KEY_BYTES, EncryptedStore, StoreKeyError, read_key_file


def _write_new(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def _seal(key: bytes, protected: Path) -> None:
    _write_new(protected, dpapi.protect(key))
    try:
        if read_key_file(protected) != key:
            raise StoreKeyError("the sealed key does not read back")
    except Exception:
        protected.unlink()
        raise


def create_protected_key(protected: Path) -> None:
    _seal(os.urandom(KEY_BYTES), protected)


def protect_existing(source: Path, protected: Path, store: Path) -> None:
    """Seal the key in `source`; if a store exists, the key must open it (else nothing is written)."""
    if protected.exists():
        raise FileExistsError(protected)
    key = read_key_file(source)
    if store.is_file():
        EncryptedStore(store, key).close()  # StoreKeyError if this key does not open it; read-only check
    _seal(key, protected)


def export_backup(protected: Path, destination: Path, norev_root: Path) -> None:
    destination = Path(destination)
    if destination.resolve().is_relative_to(Path(norev_root).resolve()):
        raise ValueError("the backup must be outside norev's folder, ideally on a USB stick")
    key = read_key_file(protected)
    _write_new(destination, key)
    if destination.read_bytes() != key:
        destination.unlink()
        raise OSError("the backup does not read back")
