"""Recoverable ordered index and sequence allocation."""
import os
import tempfile
import struct


INDEX_MAGIC = b"EACHIDX3"
INDEX_HEADER_SIZE = len(INDEX_MAGIC) + 8
INDEX_EMPTY = INDEX_MAGIC + bytes(8)
INDEX_RECORD = struct.Struct(">Q32s128s128s")


class IndexStorage:
    def _index_valid(self, path):
        if not path.exists():
            return False
        with path.open("rb") as stream:
            if stream.read(len(INDEX_MAGIC)) != INDEX_MAGIC:
                return False
            count = stream.read(8)
            return (len(count) == 8 and path.stat().st_size ==
                    INDEX_HEADER_SIZE + struct.unpack(">Q", count)[0] * INDEX_RECORD.size)

    def _rebuild_index(self, cid):
        path = self.conv_dir(cid) / "index"
        entries = self._scan_records(cid)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-index-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(INDEX_MAGIC + struct.pack(">Q", len(entries)))
                for r in entries:
                    stream.write(INDEX_RECORD.pack(r["seq"], self.sender_digest(r["sender"]),
                                                   r["msg_id"].encode("ascii"),
                                                   (r.get("retracts") or "").encode("ascii")))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            (path.parent / ".index-dirty").unlink(missing_ok=True)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def ensure_index_locked(self, cid):
        path = self.conv_dir(cid) / "index"
        if (path.parent / ".index-dirty").exists() or not self._index_valid(path):
            self._rebuild_index(cid)
        return path

    def index_entries(self, path, after_seq=0):
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

    def append_index(self, cid, seq, mid, digest, target=""):
        with (self.conv_dir(cid) / "index").open("r+b") as stream:
            stream.seek(0, os.SEEK_END)
            count = (stream.tell() - INDEX_HEADER_SIZE) // INDEX_RECORD.size
            stream.write(INDEX_RECORD.pack(seq, digest, mid.encode("ascii"),
                                           target.encode("ascii")))
            stream.seek(len(INDEX_MAGIC))
            stream.write(struct.pack(">Q", count + 1))
            stream.flush()
            os.fsync(stream.fileno())

    def index_entries_reverse(self, path):
        with path.open("rb") as stream:
            count = (path.stat().st_size - INDEX_HEADER_SIZE) // INDEX_RECORD.size
            for index in range(count - 1, -1, -1):
                stream.seek(INDEX_HEADER_SIZE + index * INDEX_RECORD.size)
                seq, digest, mid, target = INDEX_RECORD.unpack(stream.read(INDEX_RECORD.size))
                yield (seq, digest, mid.rstrip(b"\0").decode("ascii"),
                       target.rstrip(b"\0").decode("ascii"))

    def next_sequence(self, cid):
        path = self.conv_dir(cid) / "sequence"
        previous = int(path.read_text()) if path.exists() else 0
        # The index tail also prevents reusing a sequence after a stale counter restore.
        with (self.conv_dir(cid) / "index").open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() > INDEX_HEADER_SIZE:
                stream.seek(-INDEX_RECORD.size, os.SEEK_END)
                previous = max(previous, INDEX_RECORD.unpack(stream.read(INDEX_RECORD.size))[0])
        seq = previous + 1
        self.atomic_write(path, str(seq))
        return seq
