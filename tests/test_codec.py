"""Message file format, byte preservation and strict decoding."""
import hashlib
import unittest

from eachboard.errors import EachboardError
from eachboard.storage.codec import (
    HEADER_FIELDS, MAX_HEADER_BYTES, decode_message, encode_message, message_id,
)


class CodecTests(unittest.TestCase):
    def meta(self, **changes):
        value = {'seq': 24, 'sender': 'alice', 'to': ['bob', 'charlie'], 'created': 1.25,
                 'task_id': 'review', 'reply_to': None, 'retracts': None}
        return dict(value, **changes)

    def reject(self, operation, code='corrupt_data'):
        with self.assertRaises(EachboardError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_body_bytes_are_preserved_without_json_escaping(self):
        bodies = ['', 'no final newline', '\n\nleading and trailing blank lines\n\n',
                  'CRLF\r\nthen CR\rthen LF\n', '\t"quotes" and \\backslashes\\',
                  '中文 🤖 e\u0301\nsecond line', 'body with NUL\x00end',
                  'seq 999\nsender not-the-sender\n\nmore body']
        for body in bodies:
            with self.subTest(body=body):
                raw = encode_message(self.meta(), body)
                self.assertEqual(raw.split(b'\n\n', 1)[1], body.encode('utf-8'))
                self.assertEqual(decode_message(raw), {**self.meta(), 'content': body})

    def test_empty_headers_and_retraction_reference_round_trip(self):
        target = 'm' + 'a' * 64
        for meta in (self.meta(to=[], task_id=None), self.meta(retracts=target, reply_to=target)):
            with self.subTest(meta=meta):
                self.assertEqual(decode_message(encode_message(meta, 'notice')), {**meta, 'content': 'notice'})

    def test_id_hashes_the_entire_raw_object_and_is_not_embedded(self):
        raw = encode_message(self.meta(), 'payload')
        mid = message_id(raw)
        self.assertEqual(mid, 'm' + hashlib.sha256(raw).hexdigest())
        self.assertNotIn(mid.encode(), raw)
        self.assertNotEqual(mid, message_id(encode_message(self.meta(seq=25), 'payload')))
        self.assertNotEqual(mid, message_id(encode_message(self.meta(), 'payload\n')))

    def test_every_header_is_required_and_ordered(self):
        raw = encode_message(self.meta(), 'payload')
        header, body = raw.split(b'\n\n', 1)
        lines = header.split(b'\n')
        self.assertEqual(len(lines), len(HEADER_FIELDS) + 1)
        for number in range(1, len(lines)):
            with self.subTest(missing=number):
                missing = b'\n'.join(lines[:number] + lines[number+1:]) + b'\n\n' + body
                self.reject(lambda: decode_message(missing))
        swapped = lines[:]
        swapped[2], swapped[3] = swapped[3], swapped[2]
        self.reject(lambda: decode_message(b'\n'.join(swapped) + b'\n\n' + body))
        self.reject(lambda: decode_message(header + b'\nseq 24\n\npayload'))

    def test_malformed_headers_and_body_are_rejected(self):
        raw = encode_message(self.meta(), 'payload')
        changes = [
            (b'seq 24', b'seq 0'), (b'seq 24', b'seq 024'),
            (b'seq 24', b'seq 18446744073709551616'),
            (b'sender alice', b'sender ../alice'), (b'sender alice', b'sender al\x00ice'),
            (b'sender alice', b'sender \xff'), (b'to bob,charlie', b'to bob,,charlie'),
            (b'created 1.25', b'created nan'), (b'created 1.25', b'created inf'),
            (b'created 1.25', b'created 1_000'),
            (b'task review', b'task  review'), (b'task review', b'task review\r'),
            (b'reply-to', b'reply-to mshort'), (b'retracts', b'retracts ../target'),
            (b'payload', b'\xff'),
        ]
        for before, after in changes:
            with self.subTest(after=after):
                self.reject(lambda: decode_message(raw.replace(before, after)))

    def test_unknown_versions_json_and_oversized_headers_are_rejected(self):
        raw = encode_message(self.meta(), 'payload')
        self.reject(lambda: decode_message(raw.replace(b'eachboard-message 1', b'eachboard-message 2')),
                    'unsupported_format')
        self.reject(lambda: decode_message(b'{"content":"old JSON"}\n\n'), 'unsupported_format')
        self.reject(lambda: decode_message(b'eachboard-message 1\n' + b'a' * MAX_HEADER_BYTES))
        self.reject(lambda: encode_message(self.meta(to=['b' * 128] * 600), 'payload'))

    def test_invalid_metadata_cannot_be_published(self):
        for changes in ({'seq': True}, {'seq': 0}, {'seq': 2**64}, {'created': float('nan')},
                        {'created': 10**1000}, {'sender': 'bad\nname'}, {'to': 'bob'},
                        {'to': ['bob,alice']}, {'task_id': []}, {'reply_to': 'mshort'},
                        {'attachments': []}):
            with self.subTest(changes=changes):
                self.reject(lambda: encode_message(self.meta(**changes), 'payload'))
        self.reject(lambda: encode_message(self.meta(), b'not text'))
        self.reject(lambda: encode_message(self.meta(), '\ud800'))
