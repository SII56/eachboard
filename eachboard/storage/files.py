"""File publication, locking, identifiers and hash helpers."""
import contextlib
import fcntl
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from ..errors import fail


def component(value, label="name"):
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        fail(f"invalid {label}: use 1-128 letters, digits, _ or -")
    return value


def identity(value, label="identity"):
    # Dots are allowed within identities, not in object/directory identifiers.
    if not value or not re.fullmatch(r"[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9_-])?", value):
        fail(f"invalid {label}: use 1-128 letters, digits, _, - or internal dots")
    return value


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def sender_digest(who):
    # Fixed-size index field; identifies the sender without storing a body.
    return hashlib.sha256(who.encode("utf-8")).digest()


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        binary = isinstance(content, bytes)
        options = {} if binary else {"encoding": "utf-8", "newline": ""}
        with os.fdopen(fd, "wb" if binary else "w", **options) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def lock(path):
    # Lock files are persistent: unlinking a lock could create two lock domains.
    with path.open("r" if path.exists() else "a+") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextlib.contextmanager
def publishing(parent, name):
    parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=parent))
    try:
        yield tmp
        os.rename(tmp, parent / name)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
