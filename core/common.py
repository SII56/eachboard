"""File-backed immutable message objects with a derived index and acknowledgement logs."""
import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
import struct
from pathlib import Path


def discover(start=None):
    start = Path(start or Path.cwd()).resolve()
    for parent in (start, *start.parents):
        if (parent / ".eachboard" / "config.json").is_file():
            return parent / ".eachboard"
    return None


def _root_selection():
    # Only a discovered project auto-selects a root; there is no legacy fallback.
    raw = (os.environ.get("EACHBOARD_ROOT") or "").strip()
    found = discover()
    if raw:
        configured = Path(raw).expanduser().resolve()
        return configured, "EACHBOARD_ROOT environment"
    if found is not None:
        return found, "project discovery"
    return None, "not found"


ROOT, ROOT_SOURCE = _root_selection()
WORKSPACE = ROOT.parent if ROOT is not None and ROOT.name == ".eachboard" else None
CONV = os.environ.get("EACHBOARD_CONV")
KEY = os.environ.get("EACHBOARD_KEY")
WHO = os.environ.get("EACHBOARD_WHO")


class EachboardError(Exception):
    def __init__(self, message, code="invalid_request"):
        super().__init__(message)
        self.code = code


def fail(message, code="invalid_request"):
    raise EachboardError(message, code)


JSON_OUTPUT = False


def out(obj):
    if JSON_OUTPUT:
        text = json.dumps(obj, ensure_ascii=False)
    else:
        from core.display import render
        text = render(obj)
    print(text, flush=True)


def component(value, label="name"):
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        fail(f"invalid {label}: use 1-128 letters, digits, _ or -")
    return value


def identity(value, label="identity"):
    # Dots are allowed within identities, not in object/directory identifiers.
    if not value or not re.fullmatch(r"[A-Za-z0-9_-](?:[A-Za-z0-9_.-]{0,126}[A-Za-z0-9_-])?", value):
        fail(f"invalid {label}: use 1-128 letters, digits, _, - or internal dots")
    return value


def conv_dir(cid):
    return ROOT / "conversations" / component(cid, "conversation id")


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def sender_digest(who):
    # Fixed-size index field; identifies the sender without storing a body.
    return hashlib.sha256(who.encode("utf-8")).digest()


def load_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False))


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


def current_conv():
    global CONV
    if ROOT is None:
        fail("no .eachboard project found from " + str(Path.cwd())
             + "; run python3 eachboard.py init in the shared project, set EACHBOARD_ROOT, or run python3 eachboard.py doctor",
             "project_not_found")
    config_path = ROOT / "config.json"
    if not config_path.is_file():
        fail("eachboard is not initialized at " + str(ROOT)
             + "; run python3 eachboard.py init in the project directory", "not_initialized")
    if not CONV:
        try:
            CONV = load_json(config_path).get("conversation")
        except (OSError, ValueError) as exc:
            fail("cannot read " + str(config_path) + ": " + str(exc), "not_initialized")
    if not KEY or not WHO:
        fail("EACHBOARD_WHO and EACHBOARD_KEY must both be set in the invoking environment;"
             " run python3 eachboard.py doctor for the selected root and conversation", "missing_credentials")
    if not CONV:
        fail("no active conversation in " + str(ROOT)
             + "; set EACHBOARD_CONV or run python3 eachboard.py open; run python3 eachboard.py doctor for context",
             "missing_credentials")
    identity(WHO)
    source = "EACHBOARD_CONV" if (os.environ.get("EACHBOARD_CONV") or "").strip() else "config.json"
    path = conv_dir(CONV) / "meta.json"
    if not path.is_file():
        fail("conversation " + str(CONV) + " not found in " + str(ROOT)
             + " (selected by " + source + "); check EACHBOARD_CONV or run python3 eachboard.py doctor",
             "conversation_not_found")
    meta = load_json(path)
    if not hmac.compare_digest(sha(KEY), meta["keys"].get(WHO, "")):
        fail("key rejected for " + str(WHO) + " in conversation " + str(CONV)
             + " (" + str(ROOT) + ", selected by " + source + "); check EACHBOARD_KEY or run python3 eachboard.py doctor",
             "unauthorized")
    return CONV


def state_dir(cid):
    return conv_dir(cid) / "state"


def _journal_ids(raw):
    # Only complete newline-terminated records are committed.
    rows = raw.split(b"\n")[:-1]
    return {component(row.decode("ascii"), "message id") for row in rows if row}


def _journal_read(path):
    return _journal_ids(path.read_bytes()) if path.exists() else set()


def _journal_append(path, lock_path, mids):
    with lock(lock_path):
        known = _journal_read(path)
        pending = [component(mid, "message id") for mid in dict.fromkeys(mids) if mid not in known]
        if not pending:
            return
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+b") as stream:
            raw = stream.read()
            end = raw.rfind(b"\n") + 1
            stream.seek(end)
            stream.truncate()
            stream.write(("\n".join(pending) + "\n").encode("ascii"))
            stream.flush()
            os.fsync(stream.fileno())


def acknowledged(cid, who):
    who = identity(who)
    return _journal_read(state_dir(cid) / (who + ".read"))


def stamp_many(cid, mids, who):
    who = identity(who)
    mids = [component(mid, "message id") for mid in dict.fromkeys(mids)]
    seen = seen_messages(cid, who)
    for mid in mids:
        if mid not in seen:
            fail("message " + mid + " has not been displayed; run python3 eachboard.py read"
                 " or show " + mid + " before acknowledging it", "message_not_seen")
    directory = state_dir(cid)
    directory.mkdir(parents=True, exist_ok=True)
    _journal_append(directory / (who + ".read"), directory / (who + ".lock"), mids)


def stamp(cid, mid, who):
    stamp_many(cid, [mid], who)


def has_stamp(cid, mid, who):
    return mid in acknowledged(cid, who)


def seen_messages(cid, who):
    who = identity(who)
    return _journal_read(state_dir(cid) / (who + ".seen"))


def mark_seen_many(cid, mids, who):
    who = identity(who)
    directory = state_dir(cid)
    directory.mkdir(parents=True, exist_ok=True)
    _journal_append(directory / (who + ".seen"), directory / (who + ".seen.lock"), mids)


def mark_seen(cid, mid, who):
    mark_seen_many(cid, [mid], who)


def has_seen(cid, mid, who):
    return mid in seen_messages(cid, who)


def project_path(value, must_exist=True):
    if WORKSPACE is None:
        fail("run python3 eachboard.py init in a shared project first", "project_required")
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else Path.cwd() / path).resolve()
    if not path.is_relative_to(WORKSPACE) or path.is_relative_to(ROOT):
        fail("file must be inside the shared project and outside .eachboard", "outside_workspace")
    if must_exist and not path.is_file():
        fail("file not found", "file_not_found")
    return path


# Fixed-size entries permit binary search by sequence without loading the index.
INDEX_MAGIC = b"EACHIDX2"
INDEX_HEADER_SIZE = len(INDEX_MAGIC) + 8
INDEX_EMPTY = INDEX_MAGIC + bytes(8)
# sequence, 32-byte SHA256 of the sender identity, NUL-padded message ID,
# NUL-padded retraction target (empty for ordinary messages and notifications
# only fill it, so a single index pass yields the target -> event mapping)
INDEX_RECORD = struct.Struct(">Q32s128s128s")


def message_path(cid, mid):
    mid = component(mid, "message id")
    prefix = mid[1:3] if mid.startswith("m") else mid[:2]
    return conv_dir(cid) / "messages" / prefix / (mid + ".json")


def _scan_records(cid):
    parent = conv_dir(cid) / "messages"
    result = []
    if not parent.exists():
        return result
    for path in parent.iterdir():
        if path.name.startswith(".") or not path.is_dir():
            continue
        if len(path.name) != 2 or any(ch not in "0123456789abcdef" for ch in path.name):
            continue
        for obj in path.glob("*.json"):
            if obj.name.startswith("."):
                continue
            data = load_json(obj)
            validate_object(data)
            if data.get("msg_id") != obj.stem or "content" not in data:
                fail("invalid message object: " + str(obj), "corrupt_data")
            result.append(data)
    result.sort(key=lambda r: (r["seq"], r["msg_id"]))
    if len({r["seq"] for r in result}) != len(result) or any(r["seq"] < 1 for r in result):
        fail("duplicate or invalid message sequence", "corrupt_data")
    return result


def _index_valid(path):
    if not path.exists():
        return False
    with path.open("rb") as stream:
        if stream.read(len(INDEX_MAGIC)) != INDEX_MAGIC:
            return False
        count = stream.read(8)
        return (len(count) == 8 and path.stat().st_size ==
                INDEX_HEADER_SIZE + struct.unpack(">Q", count)[0] * INDEX_RECORD.size)


def _rebuild_index(cid):
    path = conv_dir(cid) / "index"
    entries = _scan_records(cid)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-index-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(INDEX_MAGIC + struct.pack(">Q", len(entries)))
            for r in entries:
                stream.write(INDEX_RECORD.pack(r["seq"], sender_digest(r["sender"]),
                                               r["msg_id"].encode("ascii"),
                                               (r.get("retracts") or "").encode("ascii")))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        (path.parent / ".index-dirty").unlink(missing_ok=True)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def ensure_index_locked(cid):
    path = conv_dir(cid) / "index"
    if (path.parent / ".index-dirty").exists() or not _index_valid(path):
        _rebuild_index(cid)
    return path


def index_entries(path, after_seq=0):
    with path.open("rb") as stream:
        count = (path.stat().st_size - INDEX_HEADER_SIZE) // INDEX_RECORD.size
        low, high = 0, count
        while low < high:
            middle = (low + high) // 2
            stream.seek(INDEX_HEADER_SIZE + middle * INDEX_RECORD.size)
            seq = INDEX_RECORD.unpack(stream.read(INDEX_RECORD.size))[0]
            if seq <= after_seq:
                low = middle + 1
            else:
                high = middle
        stream.seek(INDEX_HEADER_SIZE + low * INDEX_RECORD.size)
        for _ in range(low, count):
            seq, digest, mid, target = INDEX_RECORD.unpack(stream.read(INDEX_RECORD.size))
            yield (seq, digest, mid.rstrip(b"\0").decode("ascii"),
                   target.rstrip(b"\0").decode("ascii"))


def append_index(cid, seq, mid, digest, target=""):
    with (conv_dir(cid) / "index").open("r+b") as stream:
        stream.seek(0, os.SEEK_END)
        count = (stream.tell() - INDEX_HEADER_SIZE) // INDEX_RECORD.size
        stream.write(INDEX_RECORD.pack(seq, digest, mid.encode("ascii"),
                                       target.encode("ascii")))
        stream.seek(len(INDEX_MAGIC))
        stream.write(struct.pack(">Q", count + 1))
        stream.flush()
        os.fsync(stream.fileno())


def load_message(cid, mid, seq=None):
    path = message_path(cid, mid)
    if not path.is_file():
        fail("message not found", "message_not_found")
    data = load_json(path)
    validate_object(data)
    if "seq" not in data:
        if seq is None:
            seq = next((s for s, _, m, _t in index_entries(conv_dir(cid) / "index") if m == mid), None)
        data["seq"] = seq
    if data.get("msg_id") != mid:
        fail("message id mismatch", "corrupt_data")
    return {"msg_id": mid, "seq": data["seq"], "sender": data["sender"],
            "created": data["created"], "to": data.get("to", []),
            "reply_to": data.get("reply_to"), "task_id": data.get("task_id"),
            "attachments": data.get("attachments", []), "content": data["content"],
            "retracts": data.get("retracts")}


def visible(record):
    return not record.get("to") or WHO == record["sender"] or WHO in record["to"]


def _targets_locked(cid):
    """target message id -> retraction event id, built from the index alone.

    The caller must hold the conversation send lock. One pass over the index
    replaces any scan of message bodies, so the cost stays linear in the number
    of messages and independent of how many of them are displayed.
    """
    targets = {}
    for _seq, _digest, mid, target in index_entries(conv_dir(cid) / "index", 0):
        if target:
            targets.setdefault(target, mid)
    return targets


def project(cid, record, targets, force=False):
    """Display form of a record.

    A retracted message keeps its id, sequence, sender and recipients, but its
    body and attachment list become a placeholder unless force is set.
    retraction_id is the retraction event, retracted_by is the identity that
    performed it, retracted_at is when that event was created.
    """
    out = dict(record)
    event_id = targets.get(record["msg_id"])
    if event_id is None:
        out["retracted"] = False
        return out
    event = load_message(cid, event_id)
    out["retracted"] = True
    out["retraction_id"] = event_id
    out["retracted_by"] = event["sender"]
    out["retracted_at"] = event["created"]
    if not force:
        out["content"] = "[retracted]"
        out["attachments"] = []
    return out


def messages(cid, mode="all", after_seq=0, limit=100, force=False):
    """Visible messages in sequence order, already projected for display.

    mode "all" returns history including the caller's own messages. "unseen"
    returns ordinary messages that have not been displayed plus retraction
    notifications that have not been displayed; a retracted original is never
    new work. "pending" returns ordinary messages that have not been
    acknowledged; retracted originals and retraction notifications are never
    pending work. Displaying a message does not acknowledge it.
    """
    if mode not in ("all", "unseen", "pending"):
        fail("invalid message filter", "invalid_request")
    result = []
    with lock(conv_dir(cid) / ".send.lock"):
        path = ensure_index_locked(cid)
        targets = _targets_locked(cid)
        if mode == "unseen":
            skip = seen_messages(cid, WHO)
        elif mode == "pending":
            skip = acknowledged(cid, WHO)
        else:
            skip = set()
        mine = sender_digest(WHO)
        for seq, digest, mid, target in index_entries(path, after_seq):
            if mode != "all" and (digest == mine or mid in skip):
                continue
            if mode == "unseen" and mid in targets:
                continue
            if mode == "pending" and (target or mid in targets):
                continue
            record = load_message(cid, mid, seq)
            if not visible(record):
                continue
            result.append(project(cid, record, targets, force))
            if len(result) >= limit:
                break
    return result


def counts(cid):
    """unseen and pending totals for the current identity, whole conversation.

    Records neither seen nor acknowledged state. Completed messages, the
    caller's own messages and retracted originals are skipped using the index,
    before any body is loaded. A retraction notification counts as new work but
    never as pending work. The two numbers are counted independently.
    """
    mine = sender_digest(WHO)
    unseen = 0
    pending = 0
    with lock(conv_dir(cid) / ".send.lock"):
        path = ensure_index_locked(cid)
        targets = _targets_locked(cid)
        seen = seen_messages(cid, WHO)
        acked = acknowledged(cid, WHO)
        for seq, digest, mid, target in index_entries(path, 0):
            if digest == mine or mid in targets:
                continue
            if target:
                if mid in seen:
                    continue
                record = load_message(cid, mid, seq)
                if visible(record):
                    unseen += 1
                continue
            if mid in seen and mid in acked:
                continue
            record = load_message(cid, mid, seq)
            if not visible(record):
                continue
            if mid not in seen:
                unseen += 1
            if mid not in acked:
                pending += 1
    return {"unseen": unseen, "pending": pending}


def index_entries_reverse(path):
    with path.open("rb") as stream:
        count = (path.stat().st_size - INDEX_HEADER_SIZE) // INDEX_RECORD.size
        for index in range(count - 1, -1, -1):
            stream.seek(INDEX_HEADER_SIZE + index * INDEX_RECORD.size)
            seq, digest, mid, target = INDEX_RECORD.unpack(stream.read(INDEX_RECORD.size))
            yield (seq, digest, mid.rstrip(b"\0").decode("ascii"),
                   target.rstrip(b"\0").decode("ascii"))


def messages_latest(cid, limit=50, force=False):
    # Return the newest visible messages in ascending sequence order.
    result = []
    with lock(conv_dir(cid) / ".send.lock"):
        path = ensure_index_locked(cid)
        targets = _targets_locked(cid)
        for seq, digest, mid, target in index_entries_reverse(path):
            record = load_message(cid, mid, seq)
            if visible(record):
                result.append(project(cid, record, targets, force))
            if len(result) >= limit:
                break
    result.reverse()
    return result


def first_unseen_locked(cid):
    """First visible message from another identity that this identity has not seen.

    The caller must hold the conversation send lock; this helper never takes it.
    A retracted original no longer blocks, while an unseen retraction
    notification does. Bodies are loaded only until the first match is found.
    """
    path = ensure_index_locked(cid)
    targets = _targets_locked(cid)
    known = seen_messages(cid, WHO)
    mine = sender_digest(WHO)
    for seq, digest, mid, target in index_entries(path, 0):
        if mid in known or digest == mine or mid in targets:
            continue
        record = load_message(cid, mid, seq)
        if visible(record):
            return record
    return None


def get_messages_locked(cid, mids, force=False):
    """Resolve a batch against one retraction snapshot; caller holds the send lock."""
    targets = _targets_locked(cid)
    result = []
    for mid in mids:
        record = load_message(cid, mid)
        if not visible(record):
            fail("message not found", "message_not_found")
        result.append(project(cid, record, targets, force))
    return result


def get_message(cid, mid, force=False):
    with lock(conv_dir(cid) / ".send.lock"):
        ensure_index_locked(cid)
        return get_messages_locked(cid, [mid], force)[0]


def records(cid):
    # Compatibility helper; optimized callers use the index or a direct ID lookup.
    return _scan_records(cid)


def next_sequence(cid):
    path = conv_dir(cid) / "sequence"
    previous = int(path.read_text()) if path.exists() else 0
    # The index tail also prevents reusing a sequence after a stale counter restore.
    with (conv_dir(cid) / "index").open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() > INDEX_HEADER_SIZE:
            stream.seek(-INDEX_RECORD.size, os.SEEK_END)
            previous = max(previous, INDEX_RECORD.unpack(stream.read(INDEX_RECORD.size))[0])
    seq = previous + 1
    atomic_write(path, str(seq))
    return seq


def validate_object(data):
    payload = {key: value for key, value in data.items() if key != "msg_id"}
    expected = "m" + sha(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if not hmac.compare_digest(expected, data.get("msg_id", "")):
        fail("message checksum mismatch", "checksum_mismatch")


def publish_message(cid, meta, content):
    # Caller holds the send lock. A dirty marker bridges the two-file commit.
    atomic_write(conv_dir(cid) / ".index-dirty", "pending")
    payload = {**meta, "content": content}
    mid = "m" + sha(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    write_json(message_path(cid, mid), {"msg_id": mid, **payload})
    append_index(cid, meta["seq"], mid, sender_digest(meta["sender"]),
                 meta.get("retracts") or "")
    (conv_dir(cid) / ".index-dirty").unlink()
    return mid
