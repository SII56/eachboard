"""Versioned UTF-8 message objects: fixed ASCII headers, a blank line, raw text.

The object ID is the SHA-256 of these exact bytes, not an embedded field. Header
order is fixed; empty optional fields have only their name. Body whitespace and
line endings are never normalized by the storage codec.
"""
import hashlib
import math
import re

from ..errors import EachboardError, fail
from .files import component, identity

MESSAGE_MAGIC = b'eachboard-message 1'
MAX_HEADER_BYTES = 65536
HEADER_FIELDS = ('seq', 'sender', 'to', 'created', 'task', 'reply-to', 'retracts')
META_FIELDS = {'seq', 'sender', 'to', 'created', 'task_id', 'reply_to', 'retracts'}


def is_message_id(value):
    return isinstance(value, str) and re.fullmatch(r'm[0-9a-f]{64}', value) is not None


def message_id(raw):
    return 'm' + hashlib.sha256(raw).hexdigest()


def invalid(reason):
    fail('invalid message object: ' + reason, 'corrupt_data')


def validate_meta(meta):
    if not isinstance(meta, dict) or set(meta) - META_FIELDS:
        invalid('unknown metadata fields')
    if not {'seq', 'sender', 'to', 'created'} <= set(meta):
        invalid('missing required metadata')
    if type(meta['seq']) is not int or not 0 < meta['seq'] < 2**64:
        invalid('sequence must be a positive 64-bit integer')
    created = meta['created']
    try:
        finite = type(created) in (int, float) and math.isfinite(created)
    except OverflowError:
        finite = False
    if not finite:
        invalid('creation time must be finite')
    if not isinstance(meta['to'], list):
        invalid('recipients must be a list')
    if meta.get('task_id') is not None and not isinstance(meta['task_id'], str):
        invalid('task identifier must be text')
    try:
        identity(meta['sender'], 'sender')
        for recipient in meta['to']:
            identity(recipient, 'recipient')
        if meta.get('task_id'):
            component(meta['task_id'], 'task id')
    except (EachboardError, TypeError):
        invalid('invalid sender, recipient or task identifier')
    for key in ('reply_to', 'retracts'):
        if meta.get(key) is not None and not is_message_id(meta[key]):
            invalid('invalid ' + key + ' reference')


def encode_message(meta, content):
    validate_meta(meta)
    if not isinstance(content, str):
        invalid('body must be text')
    values = (str(meta['seq']), meta['sender'], ','.join(meta['to']),
              repr(float(meta['created'])), meta.get('task_id') or '',
              meta.get('reply_to') or '', meta.get('retracts') or '')
    lines = [MESSAGE_MAGIC]
    for name, value in zip(HEADER_FIELDS, values):
        lines.append((name + (' ' + value if value else '')).encode('ascii'))
    header = b'\n'.join(lines)
    if len(header) > MAX_HEADER_BYTES:
        invalid('header exceeds the supported size')
    try:
        return header + b'\n\n' + content.encode('utf-8')
    except UnicodeEncodeError:
        invalid('body is not valid UTF-8 text')


def decode_message(raw):
    boundary = raw.find(b'\n\n', 0, MAX_HEADER_BYTES + 2)
    if boundary < 0:
        invalid('missing header terminator or oversized header')
    lines = raw[:boundary].split(b'\n')
    if lines[0] != MESSAGE_MAGIC:
        fail('unsupported message object format', 'unsupported_format')
    if len(lines) != len(HEADER_FIELDS) + 1:
        invalid('missing, duplicate or extra header fields')
    fields = {}
    for expected, line in zip(HEADER_FIELDS, lines[1:]):
        try:
            name, separator, value = line.decode('ascii').partition(' ')
        except UnicodeDecodeError:
            invalid('headers must be ASCII')
        if name != expected or '\x00' in value or '\r' in value:
            invalid('invalid or out-of-order header field')
        if separator and (not value or value != value.strip()):
            invalid('noncanonical header whitespace')
        fields[name] = value
    if not re.fullmatch(r'[1-9][0-9]{0,19}', fields['seq']):
        invalid('invalid sequence')
    if not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:e[+-]?[0-9]+)?', fields['created']):
        invalid('invalid creation time')
    try:
        created = float(fields['created'])
    except ValueError:
        invalid('invalid creation time')
    meta = {'seq': int(fields['seq']), 'sender': fields['sender'],
            'to': fields['to'].split(',') if fields['to'] else [], 'created': created,
            'task_id': fields['task'] or None, 'reply_to': fields['reply-to'] or None,
            'retracts': fields['retracts'] or None}
    validate_meta(meta)
    try:
        content = raw[boundary + 2:].decode('utf-8')
    except UnicodeDecodeError:
        invalid('body is not UTF-8')
    return {**meta, 'content': content}
