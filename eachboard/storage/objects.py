"""Immutable text objects verified against their raw-byte content IDs."""
import hmac

from .codec import decode_message, encode_message, is_message_id, message_id


class ObjectStorage:
    def message_path(self, cid, mid):
        mid = self.component(mid, 'message id')
        prefix = mid[1:3] if mid.startswith('m') else mid[:2]
        return self.conv_dir(cid) / 'messages' / prefix / mid

    def _read_object(self, path, mid, seq=None):
        raw = path.read_bytes()
        if not hmac.compare_digest(message_id(raw), mid):
            self.fail('message checksum mismatch', 'checksum_mismatch')
        data = decode_message(raw)
        if seq is not None and data['seq'] != seq:
            self.fail('message sequence does not match index', 'corrupt_data')
        return {'msg_id': mid, **data}

    def _scan_records(self, cid):
        parent = self.conv_dir(cid) / 'messages'
        result = []
        for shard in parent.iterdir():
            if shard.name.startswith('.'):
                continue
            if (not shard.is_dir() or len(shard.name) != 2
                    or any(ch not in '0123456789abcdef' for ch in shard.name)):
                self.fail('invalid message shard: ' + str(shard), 'corrupt_data')
            for obj in shard.iterdir():
                if obj.name.startswith('.'):
                    continue
                if not obj.is_file() or not is_message_id(obj.name) or obj.name[1:3] != shard.name:
                    self.fail('invalid message object path: ' + str(obj), 'corrupt_data')
                result.append(self._read_object(obj, obj.name))
        result.sort(key=lambda record: (record['seq'], record['msg_id']))
        if len({record['seq'] for record in result}) != len(result):
            self.fail('duplicate message sequence', 'corrupt_data')
        return result

    def load_message(self, cid, mid, seq=None):
        path = self.message_path(cid, mid)
        if not path.is_file():
            self.fail('message not found', 'message_not_found')
        return self._read_object(path, mid, seq)

    def publish_message(self, cid, meta, content):
        # Encode before creating the recovery marker; the ID covers all bytes.
        raw = encode_message(meta, content)
        mid = message_id(raw)
        # Caller holds the send lock. The marker bridges object/index publication.
        self.atomic_write(self.conv_dir(cid) / '.index-dirty', 'pending')
        self.atomic_write(self.message_path(cid, mid), raw)
        self.append_index(cid, meta['seq'], mid, self.sender_digest(meta['sender']),
                          meta.get('retracts') or '')
        (self.conv_dir(cid) / '.index-dirty').unlink()
        return mid
