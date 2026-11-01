"""Per-identity display and acknowledgement journals."""
import os


class JournalStorage:
    def state_dir(self, cid):
        return self.conv_dir(cid) / "state"

    def _journal_ids(self, raw):
        # Only complete newline-terminated records are committed.
        rows = raw.split(b"\n")[:-1]
        return {self.component(row.decode("ascii"), "message id") for row in rows if row}

    def _journal_read(self, path):
        return self._journal_ids(path.read_bytes()) if path.exists() else set()

    def _journal_append(self, path, lock_path, mids):
        with self.lock(lock_path):
            known = self._journal_read(path)
            pending = [self.component(mid, "message id") for mid in dict.fromkeys(mids) if mid not in known]
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

    def acknowledged(self, cid, who):
        who = self.identity(who)
        return self._journal_read(self.state_dir(cid) / (who + ".read"))

    def stamp_many(self, cid, mids, who):
        who = self.identity(who)
        mids = [self.component(mid, "message id") for mid in dict.fromkeys(mids)]
        seen = self.seen_messages(cid, who)
        for mid in mids:
            if mid not in seen:
                self.fail("message " + mid + " has not been displayed; run python3 -m eachboard read"
                     " or show " + mid + " before acknowledging it", "message_not_seen")
        directory = self.state_dir(cid)
        directory.mkdir(parents=True, exist_ok=True)
        self._journal_append(directory / (who + ".read"), directory / (who + ".lock"), mids)

    def stamp(self, cid, mid, who):
        self.stamp_many(cid, [mid], who)

    def has_stamp(self, cid, mid, who):
        return mid in self.acknowledged(cid, who)

    def seen_messages(self, cid, who):
        who = self.identity(who)
        return self._journal_read(self.state_dir(cid) / (who + ".seen"))

    def mark_seen_many(self, cid, mids, who):
        who = self.identity(who)
        directory = self.state_dir(cid)
        directory.mkdir(parents=True, exist_ok=True)
        self._journal_append(directory / (who + ".seen"), directory / (who + ".seen.lock"), mids)

    def mark_seen(self, cid, mid, who):
        self.mark_seen_many(cid, [mid], who)

    def has_seen(self, cid, mid, who):
        return mid in self.seen_messages(cid, who)
