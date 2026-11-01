"""Messages command handlers; each receives an explicit context."""
import sys
import time


def cmd_send(ctx, args):
    cid = ctx.current_conv()
    ctx.require_participant()
    text = getattr(args, "text", None)
    if sum((text is not None, args.content is not None, args.stdin)) != 1:
        ctx.store.fail("use exactly one of TEXT, --content or --stdin")
    if args.stdin:
        # Bypass universal-newline conversion when stdin is a byte stream.
        stream = getattr(sys.stdin, 'buffer', None)
        content = stream.read().decode('utf-8') if stream is not None else sys.stdin.read()
    else:
        content = text if text is not None else args.content
    if not content or not content.strip():
        ctx.store.fail("content must not be empty")
    recipients = [ctx.store.identity(p.strip(), "recipient") for p in args.to.split(",")] if args.to else []
    participants = ctx.store.read_metadata(cid)["participants"]
    if any(p not in participants for p in recipients):
        ctx.store.fail("unknown recipient")
    if args.task_id:
        ctx.store.component(args.task_id, "task id")
    with ctx.store.lock(ctx.store.conv_dir(cid) / ".send.lock"):
        ctx.store.ensure_index_locked(cid)
        reply_record = None
        if args.reply_to:
            # Validate inside the send lock; get_message would lock again.
            reply_record = ctx.store.load_message(cid, args.reply_to)
            if not ctx.store.visible(reply_record):
                ctx.store.fail("message not found", "message_not_found")
        # Block before allocating a sequence or writing anything.
        unseen = ctx.store.first_unseen_locked(cid)
        if unseen is not None:
            ctx.store.fail("unread messages have not been displayed yet; run python3 -m eachboard read first"
                   " (first unseen: seq " + str(unseen["seq"]) + " from " + unseen["sender"]
                   + ", msg_id " + unseen["msg_id"] + ")", "unread_messages")
        seq = ctx.store.next_sequence(cid)
        meta = {"seq": seq, "sender": ctx.who, "created": time.time(), "to": recipients,
                "reply_to": args.reply_to, "task_id": args.task_id}
        mid = ctx.store.publish_message(cid, meta, content)
    ctx.out({"ok": True, "msg_id": mid, **meta})


def output_page(ctx, cid, msgs, after_seq=0, ack=False):
    """Flush a page before recording its displayed and optional processed IDs."""
    ctx.out({"ok": True, "count": len(msgs), "messages": msgs,
           "next_after_seq": msgs[-1]["seq"] if msgs else after_seq})
    if ctx.is_admin:
        return
    if msgs:
        # A placeholder for a retracted message was never actually displayed, so
        # it must not be recorded as seen. Retraction notifications are read-only
        # information: they are marked seen but never acknowledged.
        shown = [m["msg_id"] for m in msgs if not m.get("retracted")]
        if shown:
            ctx.store.mark_seen_many(cid, shown, ctx.who)
        if ack:
            ackable = [m["msg_id"] for m in msgs
                       if not m.get("retracts") and not m.get("retracted")]
            if ackable:
                ctx.store.stamp_many(cid, ackable, ctx.who)


def cmd_read(ctx, args):
    # Default view: what has not been shown yet. --pending switches to the
    # older question "what have I not finished", which includes messages that
    # were already displayed.
    cid = ctx.current_conv()
    if ctx.is_admin and (getattr(args, 'ack', False) or getattr(args, 'pending', False)):
        ctx.store.fail('administrator has no inbox or acknowledgements; use list or read without --pending/--ack',
                       'admin_read_only')
    mode = "pending" if getattr(args, "pending", False) else "unseen"
    if ctx.is_admin:
        mode = 'all'
    msgs = ctx.store.messages(cid, mode=mode, after_seq=args.after_seq, limit=args.limit)
    output_page(ctx, cid, msgs, args.after_seq, ack=getattr(args, "ack", False))


def cmd_list(ctx, args):
    cid = ctx.current_conv()
    if args.after_seq is None:
        # Default view: the newest messages, still shown in ascending order.
        msgs = ctx.store.messages_latest(cid, limit=args.limit)
        cursor = 0
    else:
        # Explicit cursor (including 0) keeps forward pagination semantics.
        msgs = ctx.store.messages(cid, mode="all", after_seq=args.after_seq, limit=args.limit)
        cursor = args.after_seq
    output_page(ctx, cid, msgs, cursor)


def cmd_ack(ctx, args):
    cid = ctx.current_conv()
    ctx.require_participant()
    # Validate the entire batch before recording any confirmations.
    ids = list(dict.fromkeys(args.msg_ids))
    with ctx.store.lock(ctx.store.conv_dir(cid) / ".send.lock"):
        ctx.store.ensure_index_locked(cid)
        records = ctx.store.get_messages_locked(cid, ids)
        for record in records:
            if record.get("retracts") or record.get("retracted"):
                ctx.store.fail("message " + record["msg_id"] + " is retracted or a retraction notification;"
                       " acknowledgement is not required", "ack_not_required")
        ctx.store.stamp_many(cid, ids, ctx.who)
    ctx.out({"ok": True, "acked": ids})


def cmd_show(ctx, args):
    cid = ctx.current_conv()
    force = getattr(args, "force", False)
    message = ctx.store.get_message(cid, args.msg_id, force=force)
    ctx.out({"ok": True, "message": message})
    # A placeholder is not a display of the original body, so it does not mark
    # the message as seen; --force does show the original and may record it.
    if not ctx.is_admin and (not message.get("retracted") or force):
        ctx.store.mark_seen_many(cid, [message["msg_id"]], ctx.who)


def cmd_retract(ctx, args):
    cid = ctx.current_conv()
    ctx.require_participant()
    mid = ctx.store.component(args.msg_id, "message id")
    with ctx.store.lock(ctx.store.conv_dir(cid) / ".send.lock"):
        ctx.store.ensure_index_locked(cid)
        targets = ctx.store._targets_locked(cid)
        record = ctx.store.load_message(cid, mid)
        if not ctx.store.visible(record):
            ctx.store.fail("message not found", "message_not_found")
        if record["sender"] != ctx.who:
            ctx.store.fail("only the sender can retract message " + mid, "not_sender")
        if record.get("retracts"):
            ctx.store.fail("a retraction notification cannot be retracted",
                   "retraction_not_retractable")
        existing = targets.get(mid)
        if existing is not None:
            # Idempotent: report the event that already exists, consume nothing.
            event = ctx.store.load_message(cid, existing)
            ctx.out({"ok": True, "msg_id": existing, "retracts": mid,
                   "target_seq": record["seq"], "created": event["created"],
                   "already": True})
            return
        seq = ctx.store.next_sequence(cid)
        meta = {"seq": seq, "sender": ctx.who, "created": time.time(),
                "to": record.get("to", []), "reply_to": None, "task_id": None,
                "retracts": mid}
        content = ctx.who + " retracted message #" + str(record["seq"])
        event_id = ctx.store.publish_message(cid, meta, content)
    ctx.out({"ok": True, "msg_id": event_id, "retracts": mid,
           "target_seq": record["seq"], "created": meta["created"], "already": False})


def cmd_receipts(ctx, args):
    cid = ctx.current_conv()
    record = ctx.store.get_message(cid, args.msg_id)
    participants = ctx.store.read_metadata(cid)["participants"]
    recipients = record.get("to") or participants
    acked = [who for who in recipients if who != record["sender"] and ctx.store.has_stamp(cid, args.msg_id, who)]
    ack_required = not record.get("retracts") and not record.get("retracted")
    pending = [who for who in recipients if who != record["sender"] and who not in acked] if ack_required else []
    ctx.out({"ok": True, "msg_id": args.msg_id, "acked_by": acked, "pending": pending,
           "ack_required": ack_required, "retracted": record.get("retracted", False),
           "retracts": record.get("retracts")})


def cmd_wait(ctx, args):
    cid = ctx.current_conv()
    ctx.require_participant()
    deadline = time.monotonic() + args.timeout
    while True:
        # Only messages this identity has not been shown wake the wait; older
        # pending work must not return immediately. Unless explicitly filtered,
        # include unseen messages that arrived before the call as well.
        msgs = ctx.store.messages(cid, mode="unseen", after_seq=args.after_seq, limit=args.limit)
        if msgs:
            output_page(ctx, cid, msgs, args.after_seq, ack=args.ack)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            output_page(ctx, cid, [], args.after_seq)
            return
        time.sleep(min(0.2, remaining))
