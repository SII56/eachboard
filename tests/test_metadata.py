"""Conversation metadata must be strict text and fail closed when damaged."""
import unittest

from eachboard.errors import EachboardError
from eachboard.storage.metadata import decode_metadata, encode_metadata
from support import BoardTestCase


class MetadataTests(unittest.TestCase):
    def test_round_trip_preserves_participant_order(self):
        meta = {'created': 1.25, 'participants': ['agent.v1', 'peer'],
                'keys': {'agent.v1': 'a' * 64, 'peer': 'b' * 64}}
        self.assertEqual(decode_metadata(encode_metadata(meta)), meta)

    def test_corruption_never_becomes_broadcast_or_unprotected_auth(self):
        raw = b'eachboard-conversation 1\ncreated 1.25\n\nparticipant alice ' + b'a' * 64 + b'\n'
        cases = [b'{}', b'', raw[:-1], raw + b'extra\n',
                 raw.replace(b'conversation 1', b'conversation 2'),
                 raw.replace(b'1.25', b'nan'), raw.replace(b'1.25', b'inf'),
                 raw.replace(b'1.25', b'-1'), raw.replace(b'alice', b'admin'),
                 raw.replace(b'1.25', b' 1.25'), raw.replace(b'1.25', b'1_000'),
                 raw.replace(b'alice', b'../alice'), raw.replace(b'alice', b'al\x00ice'),
                 raw.replace(b'alice', b'\xff'), raw.replace(b'a' * 64, b'bad'),
                 raw + b'participant alice ' + b'b' * 64 + b'\n',
                 b'eachboard-conversation 1\ncreated 1.25\n\n']
        for value in cases:
            with self.subTest(raw=value), self.assertRaises(EachboardError) as caught:
                decode_metadata(value)
            self.assertEqual(caught.exception.code, 'invalid_data')


class MetadataIntegrationTests(BoardTestCase):
    def test_corrupt_metadata_is_diagnosed_and_cannot_authenticate(self):
        meta = self._conversation_path('meta')
        meta.write_bytes(b'corrupt metadata\n')
        self.run_eachboard('list', who='writer', error='invalid_data')
        self.run_eachboard('list', env=self.admin_env(EACHBOARD_CONV=self.cid), error='invalid_data')
        report = self.run_eachboard('doctor', who='writer')
        self.assertFalse(report['identity']['authorized'])
        self.assertTrue(any('metadata' in warning for warning in report['warnings']))
