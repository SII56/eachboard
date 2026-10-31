import getpass
import hashlib
import hmac
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

from core import common as c


def cmd_init(args):
    workspace = Path(args.path).expanduser().resolve()
    if not workspace.is_dir():
        c.fail("project directory not found")
    root = workspace / ".eachboard"
    if (root / "config.json").exists():
        c.fail("eachboard already initialized", "already_exists")
    with c.publishing(workspace, ".eachboard") as d:
        c.write_json(d / "config.json", {"version": 1, "conversation": None})
        for name in ("conversations", "attachments", "objects"):
            (d / name).mkdir(mode=0o700)
    ignore = workspace / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    if ".eachboard/" not in text.splitlines():
        c.atomic_write(ignore, text + ("\n" if text and not text.endswith("\n") else "") + ".eachboard/\n", mode=0o644)
    c.out({"ok": True, "root": str(root), "workspace": str(workspace)})


def cmd_open(args):
    if c.ROOT is None:
        c.fail("no .eachboard project found from " + str(Path.cwd())
               + "; run python3 eachboard.py init in the project directory", "project_not_found")
    if not (c.ROOT / "config.json").is_file():
        c.fail("eachboard is not initialized at " + str(c.ROOT)
               + "; run python3 eachboard.py init in the project directory", "not_initialized")
    participants = [c.identity(p.strip(), "participant") for p in args.participants.split(",")]
    if len(set(participants)) != len(participants):
        c.fail("duplicate participants")
    pwd = os.environ.get("EACHBOARD_ADMIN_CODE")
    if pwd is None:
        if not sys.stdin.isatty():
            c.fail("set EACHBOARD_ADMIN_CODE for noninteractive open", "admin_code_required")
        pwd = getpass.getpass("admin code: ")
    if not pwd:
        c.fail("admin code must not be empty")
    c.ROOT.mkdir(parents=True, exist_ok=True)
    with c.lock(c.ROOT / ".admin.lock"):
        admin = c.ROOT / "admin.hash"
        if not admin.exists():
            if "EACHBOARD_ADMIN_CODE" not in os.environ and getpass.getpass("confirm: ") != pwd:
                c.fail("mismatch")
            c.atomic_write(admin, c.sha(pwd))
        if not hmac.compare_digest(c.sha(pwd), admin.read_text()):
            c.fail("bad admin code", "unauthorized")
    cid = secrets.token_hex(8)
    keys = {p: secrets.token_urlsafe(24) for p in participants}
    with c.publishing(c.ROOT / "conversations", cid) as d:
        (d / "messages").mkdir()
        (d / "state").mkdir()
        (d / "index").write_bytes(c.INDEX_EMPTY)
        (d / ".send.lock").touch(mode=0o600)
        c.write_json(d / "meta.json", {"participants": participants,
                     "keys": {p: c.sha(k) for p, k in keys.items()}, "created": time.time()})
    if (c.ROOT / "config.json").is_file():
        config = c.load_json(c.ROOT / "config.json")
        config["conversation"] = cid
        c.write_json(c.ROOT / "config.json", config)
    c.out({"ok": True, "conv_id": cid, "keys": keys})


def cmd_status(args):
    cid = c.current_conv()
    totals = c.counts(cid)
    c.out({"ok": True, "conv_id": cid, "who": c.WHO, "root": str(c.ROOT),
           "participants": c.load_json(c.conv_dir(cid) / "meta.json")["participants"],
           "ack_required": True, "unseen": totals["unseen"], "pending": totals["pending"],
           "workspace": str(c.WORKSPACE) if c.WORKSPACE else None})


def _env_flag(name):
    value = os.environ.get(name)
    return "set" if value and value.strip() else "unset"


def cmd_doctor(args):
    # Diagnostics must work without credentials, without a project, and never print secrets.
    env_root = (os.environ.get("EACHBOARD_ROOT") or "").strip()
    env_conv = (os.environ.get("EACHBOARD_CONV") or "").strip()
    discovered = c.discover()
    warnings = []
    root_source = c.ROOT_SOURCE
    if env_root:
        configured = Path(env_root).expanduser().resolve()
        if discovered is not None and configured != discovered:
            warnings.append("EACHBOARD_ROOT overrides the project eachboard at " + str(discovered)
                            + "; unset EACHBOARD_ROOT to use the project")
    if c.ROOT is None:
        warnings.append("no .eachboard project found from " + str(Path.cwd())
                        + "; run python3 eachboard.py init in the shared project or set EACHBOARD_ROOT")

    config_path = c.ROOT / "config.json" if c.ROOT is not None else None
    config_conv = None
    if config_path is not None and config_path.is_file():
        try:
            config_conv = c.load_json(config_path).get("conversation")
        except (OSError, ValueError, KeyError) as exc:
            warnings.append("cannot read " + str(config_path) + ": " + str(exc))

    cid = env_conv or config_conv
    conv_source = "EACHBOARD_CONV environment" if env_conv else ("config.json" if config_conv else None)
    if env_conv and config_conv and env_conv != config_conv:
        warnings.append("EACHBOARD_CONV overrides the project conversation " + str(config_conv))

    conv_exists = False
    participants = None
    meta_path = None
    if cid and c.ROOT is not None:
        try:
            c.component(str(cid), "conversation id")
            meta_path = c.ROOT / "conversations" / str(cid) / "meta.json"
        except c.EachboardError:
            warnings.append("invalid conversation id in environment or config: " + str(cid))
        if meta_path is not None and meta_path.is_file():
            conv_exists = True
            try:
                participants = c.load_json(meta_path).get("participants")
            except (OSError, ValueError) as exc:
                warnings.append("cannot read conversation metadata: " + str(exc))
        elif meta_path is not None:
            warnings.append("selected conversation not found: " + str(cid))
    elif cid:
        warnings.append("cannot inspect conversation " + str(cid) + " without a project root")
    else:
        warnings.append("no active conversation; run python3 eachboard.py open or set EACHBOARD_CONV")

    have_who, have_key = bool(c.WHO), bool(c.KEY)
    authorized = None
    if not have_who or not have_key:
        warnings.append("EACHBOARD_WHO and EACHBOARD_KEY are not both set; authentication skipped")
    elif conv_exists:
        try:
            keys = c.load_json(meta_path).get("keys", {})
            authorized = hmac.compare_digest(c.sha(c.KEY), keys.get(c.WHO, ""))
        except (OSError, ValueError, TypeError) as exc:
            authorized = False
            warnings.append("cannot verify credentials: " + str(exc))
        if authorized is False:
            warnings.append("credentials rejected for " + str(c.WHO) + " in " + str(cid))

    c.out({
        "ok": True,
        "root": str(c.ROOT) if c.ROOT is not None else None,
        "root_source": root_source,
        "workspace": str(c.WORKSPACE) if c.WORKSPACE is not None else None,
        "project": str(discovered) if discovered is not None else None,
        "environment": {
            "EACHBOARD_ROOT": _env_flag("EACHBOARD_ROOT"),
            "EACHBOARD_CONV": _env_flag("EACHBOARD_CONV"),
            "EACHBOARD_WHO": _env_flag("EACHBOARD_WHO"),
            "EACHBOARD_KEY": _env_flag("EACHBOARD_KEY"),
        },
        "conversation": {"id": cid, "source": conv_source, "exists": conv_exists,
                         "config": config_conv},
        "identity": {"who": c.WHO, "key_present": have_key, "authorized": authorized},
        "participants": participants,
        "warnings": warnings,
    })


def cmd_send(args):
    cid = c.current_conv()
    text = getattr(args, "text", None)
    if sum((text is not None, args.content is not None, args.stdin)) != 1:
        c.fail("use exactly one of TEXT, --content or --stdin")
    content = sys.stdin.read() if args.stdin else (text if text is not None else args.content)
    if not content or not content.strip():
        c.fail("content must not be empty")
    recipients = [c.identity(p.strip(), "recipient") for p in args.to.split(",")] if args.to else []
    participants = c.load_json(c.conv_dir(cid) / "meta.json")["participants"]
    if any(p not in participants for p in recipients):
        c.fail("unknown recipient")
    if args.task_id:
        c.component(args.task_id, "task id")
    attachments = [attachment(cid, a)[0] for a in args.attachment]
    with c.lock(c.conv_dir(cid) / ".send.lock"):
        c.ensure_index_locked(cid)
        reply_record = None
        if args.reply_to:
            # Validate inside the send lock; get_message would lock again.
            reply_record = c.load_message(cid, args.reply_to)
            if not c.visible(reply_record):
                c.fail("message not found", "message_not_found")
        # Block before allocating a sequence or writing anything.
        unseen = c.first_unseen_locked(cid)
        if unseen is not None:
            c.fail("unread messages have not been displayed yet; run python3 eachboard.py read first"
                   " (first unseen: seq " + str(unseen["seq"]) + " from " + unseen["sender"]
                   + ", msg_id " + unseen["msg_id"] + ")", "unread_messages")
        seq = c.next_sequence(cid)
        meta = {"seq": seq, "sender": c.WHO, "created": time.time(), "to": recipients,
                "reply_to": args.reply_to, "task_id": args.task_id, "attachments": attachments}
        mid = c.publish_message(cid, meta, content)
    c.out({"ok": True, "msg_id": mid, **meta})


def output_page(cid, msgs, after_seq=0, ack=False):
    """Flush a page before recording its displayed and optional processed IDs."""
    c.out({"ok": True, "count": len(msgs), "messages": msgs,
           "next_after_seq": msgs[-1]["seq"] if msgs else after_seq})
    if msgs:
        # A placeholder for a retracted message was never actually displayed, so
        # it must not be recorded as seen. Retraction notifications are read-only
        # information: they are marked seen but never acknowledged.
        shown = [m["msg_id"] for m in msgs if not m.get("retracted")]
        if shown:
            c.mark_seen_many(cid, shown, c.WHO)
        if ack:
            ackable = [m["msg_id"] for m in msgs
                       if not m.get("retracts") and not m.get("retracted")]
            if ackable:
                c.stamp_many(cid, ackable, c.WHO)


def cmd_read(args):
    # Default view: what has not been shown yet. --pending switches to the
    # older question "what have I not finished", which includes messages that
    # were already displayed.
    cid = c.current_conv()
    mode = "pending" if getattr(args, "pending", False) else "unseen"
    msgs = c.messages(cid, mode=mode, after_seq=args.after_seq, limit=args.limit)
    output_page(cid, msgs, args.after_seq, ack=getattr(args, "ack", False))


def cmd_list(args):
    cid = c.current_conv()
    if args.after_seq is None:
        # Default view: the newest messages, still shown in ascending order.
        msgs = c.messages_latest(cid, limit=args.limit)
        cursor = 0
    else:
        # Explicit cursor (including 0) keeps forward pagination semantics.
        msgs = c.messages(cid, mode="all", after_seq=args.after_seq, limit=args.limit)
        cursor = args.after_seq
    output_page(cid, msgs, cursor)


def cmd_ack(args):
    cid = c.current_conv()
    # Validate the entire batch before recording any confirmations.
    ids = list(dict.fromkeys(args.msg_ids))
    with c.lock(c.conv_dir(cid) / ".send.lock"):
        c.ensure_index_locked(cid)
        records = c.get_messages_locked(cid, ids)
        for record in records:
            if record.get("retracts") or record.get("retracted"):
                c.fail("message " + record["msg_id"] + " is retracted or a retraction notification;"
                       " acknowledgement is not required", "ack_not_required")
        c.stamp_many(cid, ids, c.WHO)
    c.out({"ok": True, "acked": ids})


def cmd_show(args):
    cid = c.current_conv()
    force = getattr(args, "force", False)
    message = c.get_message(cid, args.msg_id, force=force)
    c.out({"ok": True, "message": message})
    # A placeholder is not a display of the original body, so it does not mark
    # the message as seen; --force does show the original and may record it.
    if not message.get("retracted") or force:
        c.mark_seen_many(cid, [message["msg_id"]], c.WHO)


def cmd_retract(args):
    cid = c.current_conv()
    mid = c.component(args.msg_id, "message id")
    with c.lock(c.conv_dir(cid) / ".send.lock"):
        c.ensure_index_locked(cid)
        targets = c._targets_locked(cid)
        record = c.load_message(cid, mid)
        if not c.visible(record):
            c.fail("message not found", "message_not_found")
        if record["sender"] != c.WHO:
            c.fail("only the sender can retract message " + mid, "not_sender")
        if record.get("retracts"):
            c.fail("a retraction notification cannot be retracted",
                   "retraction_not_retractable")
        existing = targets.get(mid)
        if existing is not None:
            # Idempotent: report the event that already exists, consume nothing.
            event = c.load_message(cid, existing)
            c.out({"ok": True, "msg_id": existing, "retracts": mid,
                   "target_seq": record["seq"], "created": event["created"],
                   "already": True})
            return
        seq = c.next_sequence(cid)
        meta = {"seq": seq, "sender": c.WHO, "created": time.time(),
                "to": record.get("to", []), "reply_to": None, "task_id": None,
                "attachments": [], "retracts": mid}
        content = c.WHO + " recalled a message #" + str(record["seq"])
        event_id = c.publish_message(cid, meta, content)
    c.out({"ok": True, "msg_id": event_id, "retracts": mid,
           "target_seq": record["seq"], "created": meta["created"], "already": False})


def cmd_receipts(args):
    cid = c.current_conv()
    record = c.get_message(cid, args.msg_id)
    participants = c.load_json(c.conv_dir(cid) / "meta.json")["participants"]
    recipients = record.get("to") or participants
    acked = [who for who in recipients if who != record["sender"] and c.has_stamp(cid, args.msg_id, who)]
    ack_required = not record.get("retracts") and not record.get("retracted")
    pending = [who for who in recipients if who != record["sender"] and who not in acked] if ack_required else []
    c.out({"ok": True, "msg_id": args.msg_id, "acked_by": acked, "pending": pending,
           "ack_required": ack_required, "retracted": record.get("retracted", False),
           "retracts": record.get("retracts")})


def cmd_convs(args):
    # Enumerate only the authenticated conversation; no cross-conversation listing.
    cid = c.current_conv()
    c.out({"ok": True, "conversations": [{"conv_id": cid}]})


def cmd_wait(args):
    cid = c.current_conv()
    deadline = time.monotonic() + args.timeout
    while True:
        # Only messages this identity has not been shown wake the wait; older
        # pending work must not return immediately. Unless explicitly filtered,
        # include unseen messages that arrived before the call as well.
        msgs = c.messages(cid, mode="unseen", after_seq=args.after_seq, limit=args.limit)
        if msgs:
            output_page(cid, msgs, args.after_seq, ack=args.ack)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            output_page(cid, [], args.after_seq)
            return
        time.sleep(min(0.2, remaining))


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_upload(args):
    cid = c.current_conv()
    src = c.project_path(args.file)
    digest = file_hash(src)
    aid = "a" + secrets.token_hex(16)
    meta = {"attachment_id": aid, "name": src.name, "sender": c.WHO,
            "created": time.time(), "path": src.relative_to(c.WORKSPACE).as_posix(),
            "size": src.stat().st_size, "sha256": digest,
            "kind": "snapshot" if args.snapshot else "reference"}
    if args.snapshot:
        obj = c.ROOT / "objects" / digest
        obj.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=obj.parent)
        os.close(fd)
        tmp = Path(tmp)
        try:
            shutil.copyfile(src, tmp)
            if file_hash(tmp) != digest:
                c.fail("file changed during snapshot", "file_changed")
            try:
                os.link(tmp, obj)
            except FileExistsError:
                if file_hash(obj) != digest:
                    c.fail("stored object checksum mismatch", "checksum_mismatch")
        finally:
            tmp.unlink(missing_ok=True)
    elif file_hash(src) != digest or src.stat().st_size != meta["size"]:
        c.fail("file changed during registration", "file_changed")
    with c.publishing(c.ROOT / "attachments" / cid, aid) as d:
        c.write_json(d / "meta.json", meta)
    c.out({"ok": True, **meta})


def attachment(cid, name):
    # Never interpret user input as a filesystem path or glob expression.
    if not name or Path(name).name != name or name in (".", "..") or "\\" in name:
        c.fail("invalid attachment name")
    entry = c.ROOT / "attachments" / cid / name / "meta.json"
    if entry.is_file():
        meta = c.load_json(entry)
        if meta["kind"] == "snapshot":
            src = c.ROOT / "objects" / meta["sha256"]
        else:
            src = c.project_path(str(c.WORKSPACE / meta["path"]))
        return meta, src
    c.fail("attachment not found", "attachment_not_found")


def cmd_download(args):
    cid = c.current_conv()
    meta, src = attachment(cid, args.name)
    dest = c.project_path(args.dest, must_exist=False)
    if dest.exists() and not args.force:
        c.fail("destination exists; use --force", "already_exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".eachboard-download-", dir=dest.parent)
    os.close(fd)
    tmp = Path(tmp)
    try:
        shutil.copyfile(src, tmp)
        actual = file_hash(tmp)
        if meta.get("sha256") and actual != meta["sha256"]:
            c.fail("attachment checksum mismatch", "checksum_mismatch")
        if args.force:
            os.replace(tmp, dest)
        else:
            # Atomic no-clobber publication, even if another downloader races us.
            os.link(tmp, dest)
        c.out({"ok": True, "path": str(dest), "sha256": actual, "size": dest.stat().st_size})
    finally:
        tmp.unlink(missing_ok=True)
