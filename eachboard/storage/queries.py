"""Visibility, retraction projections and inbox queries."""



class MessageQueries:
    def visible(self, record):
        return self.is_admin or not record.get("to") or self.who == record["sender"] or self.who in record["to"]

    def _targets_locked(self, cid):
        """target message id -> retraction event id, built from the index alone.

        The caller must hold the conversation send lock. One pass over the index
        replaces any scan of message bodies, so the cost stays linear in the number
        of messages and independent of how many of them are displayed.
        """
        targets = {}
        for _seq, _digest, mid, target in self.index_entries(self.conv_dir(cid) / "index", 0):
            if target:
                targets.setdefault(target, mid)
        return targets

    def task_of(self, cid, record):
        """Task a record belongs to; a retraction notification follows its original.

        Notifications carry no task header, so a task filter would otherwise drop
        the news that a task message was withdrawn.
        """
        target = record.get("retracts")
        if target:
            return self.load_message(cid, target).get("task_id")
        return record.get("task_id")

    def project(self, cid, record, targets, force=False):
        """Display form of a record.

        A retracted message keeps its id, sequence, sender and recipients, but its
        body becomes a placeholder unless force is set.
        retraction_id is the retraction event, retracted_by is the identity that
        performed it, retracted_at is when that event was created.
        """
        out = dict(record)
        event_id = targets.get(record["msg_id"])
        if event_id is None:
            out["retracted"] = False
            return out
        event = self.load_message(cid, event_id)
        out["retracted"] = True
        out["retraction_id"] = event_id
        out["retracted_by"] = event["sender"]
        out["retracted_at"] = event["created"]
        if not force:
            out["content"] = "[retracted]"
        return out

    def messages(self, cid, mode="all", after_seq=0, limit=100, force=False, task_id=None):
        """Visible messages in sequence order, already projected for display.

        mode "all" returns history including the caller's own messages. "unseen"
        returns ordinary messages that have not been displayed plus retraction
        notifications that have not been displayed; a retracted original is never
        new work. "pending" returns ordinary messages that have not been
        acknowledged; retracted originals and retraction notifications are never
        pending work. Displaying a message does not acknowledge it. task_id keeps
        only messages of that task, including notifications that retract one.
        """
        if mode not in ("all", "unseen", "pending"):
            self.fail("invalid message filter", "invalid_request")
        result = []
        with self.lock(self.conv_dir(cid) / ".send.lock"):
            path = self.ensure_index_locked(cid)
            targets = self._targets_locked(cid)
            if mode == "unseen":
                skip = self.seen_messages(cid, self.who)
            elif mode == "pending":
                skip = self.acknowledged(cid, self.who)
            else:
                skip = set()
            mine = self.sender_digest(self.who)
            for seq, digest, mid, target in self.index_entries(path, after_seq):
                if mode != "all" and (digest == mine or mid in skip):
                    continue
                if mode == "unseen" and mid in targets:
                    continue
                if mode == "pending" and (target or mid in targets):
                    continue
                record = self.load_message(cid, mid, seq)
                if not self.visible(record):
                    continue
                if task_id is not None and self.task_of(cid, record) != task_id:
                    continue
                result.append(self.project(cid, record, targets, force))
                if len(result) >= limit:
                    break
        return result

    def counts(self, cid):
        """unseen and pending totals for the current identity, whole conversation.

        Records neither seen nor acknowledged state. Completed messages, the
        caller's own messages and retracted originals are skipped using the index,
        before any body is loaded. A retraction notification counts as new work but
        never as pending work. The two numbers are counted independently.
        """
        mine = self.sender_digest(self.who)
        unseen = 0
        pending = 0
        with self.lock(self.conv_dir(cid) / ".send.lock"):
            path = self.ensure_index_locked(cid)
            targets = self._targets_locked(cid)
            seen = self.seen_messages(cid, self.who)
            acked = self.acknowledged(cid, self.who)
            for seq, digest, mid, target in self.index_entries(path, 0):
                if digest == mine or mid in targets:
                    continue
                if target:
                    if mid in seen:
                        continue
                    record = self.load_message(cid, mid, seq)
                    if self.visible(record):
                        unseen += 1
                    continue
                if mid in seen and mid in acked:
                    continue
                record = self.load_message(cid, mid, seq)
                if not self.visible(record):
                    continue
                if mid not in seen:
                    unseen += 1
                if mid not in acked:
                    pending += 1
        return {"unseen": unseen, "pending": pending}

    def messages_latest(self, cid, limit=50, force=False, task_id=None):
        # Return the newest visible messages in ascending sequence order.
        result = []
        with self.lock(self.conv_dir(cid) / ".send.lock"):
            path = self.ensure_index_locked(cid)
            targets = self._targets_locked(cid)
            for seq, digest, mid, target in self.index_entries_reverse(path):
                record = self.load_message(cid, mid, seq)
                if self.visible(record) and (task_id is None or self.task_of(cid, record) == task_id):
                    result.append(self.project(cid, record, targets, force))
                if len(result) >= limit:
                    break
        result.reverse()
        return result

    def first_unseen_locked(self, cid):
        """First visible message from another identity that this identity has not seen.

        The caller must hold the conversation send lock; this helper never takes it.
        A retracted original no longer blocks, while an unseen retraction
        notification does. Bodies are loaded only until the first match is found.
        """
        path = self.ensure_index_locked(cid)
        targets = self._targets_locked(cid)
        known = self.seen_messages(cid, self.who)
        mine = self.sender_digest(self.who)
        for seq, digest, mid, target in self.index_entries(path, 0):
            if mid in known or digest == mine or mid in targets:
                continue
            record = self.load_message(cid, mid, seq)
            if self.visible(record):
                return record
        return None

    def get_messages_locked(self, cid, mids, force=False):
        """Resolve a batch against one retraction snapshot; caller holds the send lock."""
        targets = self._targets_locked(cid)
        result = []
        for mid in mids:
            record = self.load_message(cid, mid)
            if not self.visible(record):
                self.fail("message not found", "message_not_found")
            result.append(self.project(cid, record, targets, force))
        return result

    def get_message(self, cid, mid, force=False):
        with self.lock(self.conv_dir(cid) / ".send.lock"):
            self.ensure_index_locked(cid)
            return self.get_messages_locked(cid, [mid], force)[0]
