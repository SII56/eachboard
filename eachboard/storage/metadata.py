"""Strict, versioned text metadata for a conversation (never plaintext keys)."""
import math
import re
from .files import identity
from ..errors import EachboardError, fail


def decode_metadata(raw):
    try:
        lines = raw.decode('ascii').split('\n')
        if (len(lines) < 5 or lines[0] != 'eachboard-conversation 1'
                or not lines[1].startswith('created ') or lines[2] != '' or lines[-1] != ''):
            raise ValueError('invalid header or missing final newline')
        timestamp = lines[1][8:]
        if not re.fullmatch(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?', timestamp):
            raise ValueError('invalid creation time')
        created = float(timestamp)
        if not math.isfinite(created) or created < 0:
            raise ValueError('invalid creation time')
        keys = {}
        for line in lines[3:-1]:
            fields = line.split(' ')
            if len(fields) != 3 or fields[0] != 'participant':
                raise ValueError('invalid participant record')
            who, digest = fields[1:]
            identity(who, 'participant')
            if who == 'admin' or who in keys or not re.fullmatch('[0-9a-f]{64}', digest):
                raise ValueError('reserved/duplicate participant or invalid key hash')
            keys[who] = digest
        if not keys:
            raise ValueError('missing participants')
        return {'created': created, 'participants': list(keys), 'keys': keys}
    except (ValueError, EachboardError) as exc:
        fail('invalid conversation metadata: ' + str(exc), 'invalid_data')


def encode_metadata(meta):
    text = 'eachboard-conversation 1\ncreated ' + repr(meta['created']) + '\n\n'
    text += ''.join('participant ' + who + ' ' + meta['keys'][who] + '\n'
                    for who in meta['participants'])
    raw = text.encode('ascii')
    decode_metadata(raw)
    return raw


class MetadataStorage:
    def read_metadata(self, cid):
        return decode_metadata((self.conv_dir(cid) / 'meta').read_bytes())

    def write_metadata(self, directory, meta):
        self.atomic_write(directory / 'meta', encode_metadata(meta))
