"""Atomic, validated persistence for ``state.json`` plus a job lock.

Two separate concerns (PLAN "Hardening" / "components"):
  - :func:`write_state` / :func:`read_state` — the state store is validated on write AND on read,
    and committed crash-safely: temp -> validate -> fsync -> atomic ``os.replace``. An invalid
    record raises before any bytes hit disk, and a failure mid-rename leaves the previous file
    intact (never a partial file).
  - :func:`state_lock` — an advisory ``flock`` on a sidecar ``.lock`` so the scheduler's overlapping
    jobs can't write concurrently. Independent of the atomic write itself.
"""
from __future__ import annotations

import fcntl
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union

from text_triage.state.schema import State, validate_state

__all__ = ["write_state", "read_state", "state_lock", "StateLockedError"]


class StateLockedError(RuntimeError):
    """Raised when the state lock is already held by another holder."""


def write_state(
    state: Union[State, dict],
    path: Union[str, Path],
    *,
    law: Optional[set[str]] = None,
) -> None:
    """Validate ``state`` and atomically write it to ``path``.

    ``state`` may be a :class:`State` or a raw dict; either is re-validated (with ``law``, if given)
    so the tag-law and every cross-field rule are enforced at write time. On success the file at
    ``path`` is replaced atomically; on any failure the previous file (if any) is untouched and no
    temp file is left behind.
    """
    path = Path(path)
    data = state.model_dump() if isinstance(state, State) else state
    validated = validate_state(data, law=law)
    text = validated.model_dump_json(indent=2)

    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)  # atomic on POSIX; overwrites
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_state(path: Union[str, Path], *, law: Optional[set[str]] = None) -> State:
    """Load and validate ``state.json`` (validate-on-read). Raises on a corrupt/invalid file."""
    path = Path(path)
    import json

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return validate_state(data, law=law)


@contextmanager
def state_lock(path: Union[str, Path]) -> Iterator[None]:
    """Acquire an exclusive, non-blocking ``flock`` on ``<path>.lock``.

    Raises :class:`StateLockedError` if another holder (any process, including this one via a
    second open file description) already holds it. The lock file persists; only the advisory lock
    is released on exit.
    """
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    f = open(lock_path, "w")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise StateLockedError(f"state is locked: {lock_path}") from e
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()
