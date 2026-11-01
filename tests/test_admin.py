"""Project administration is authenticated and never consumes agent messages."""
from support import BoardTestCase


class AdminTests(BoardTestCase):
    def admin(self, *args, **kwargs):
        return self.run_eachboard(*args, env=self.admin_env(EACHBOARD_CONV=self.cid), **kwargs)

    def state(self):
        return {p.relative_to(self.root): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file()}

    def test_all_conversations_without_head_and_stale_override(self):
        second = self.run_eachboard('open', '--participants', 'other,peer')
        (self.root / 'HEAD').write_text('invalid\nhead\n')
        env = self.admin_env(EACHBOARD_CONV='nonexistent')
        rows = self.run_eachboard('convs', env=env)['conversations']
        self.assertEqual({row['conv_id'] for row in rows}, {self.cid, second['conv_id']})
        self.assertEqual(next(row for row in rows if row['conv_id'] == second['conv_id'])['participants'],
                         ['other', 'peer'])
        own = self.run_eachboard('convs', who='writer')['conversations']
        self.assertEqual(own, [{'conv_id': self.cid}])

    def test_admin_can_switch_to_conversation_without_membership(self):
        second = self.run_eachboard('open', '--participants', 'other,peer')
        env = dict(self.env, EACHBOARD_CONV=second['conv_id'], EACHBOARD_WHO='other',
                   EACHBOARD_KEY=second['keys']['other'])
        sent = self.run_eachboard('send', 'private second conversation', '--to', 'peer', env=env)
        admin = self.admin_env(EACHBOARD_CONV=second['conv_id'])
        self.assertEqual(self.run_eachboard('show', sent['msg_id'], env=admin)['message']['content'],
                         'private second conversation')
        self.run_eachboard('status', env=dict(self.identity('writer'), EACHBOARD_CONV=second['conv_id']),
                           error='unauthorized')

    def test_admin_inspection_does_not_consume_or_ack_messages(self):
        sent = self.run_eachboard('send', 'private', '--to', 'reviewer', who='writer')
        before = self.state()
        for command in (('list',), ('read',), ('show', sent['msg_id']),
                        ('receipts', sent['msg_id']), ('status',), ('convs',), ('doctor',)):
            with self.subTest(command=command):
                self.admin(*command)
                self.assertEqual(self.state(), before)
        self.assertEqual(self.admin('list')['messages'][0]['content'], 'private')
        self.assertEqual(self.admin('read')['count'], 1)
        self.assertEqual(self.admin('read')['count'], 1)
        self.assertFalse(self.admin('status')['ack_required'])
        self.assertEqual(self.run_eachboard('status', who='reviewer')['unseen'], 1)
        self.run_eachboard('send', 'still blocked', who='reviewer', error='unread_messages')
        self.run_eachboard('show', sent['msg_id'], who='observer', error='message_not_found')
        self.assertEqual(self.run_eachboard('list', who='observer')['count'], 0)
        self.assertEqual(self.admin('receipts', sent['msg_id'])['pending'], ['reviewer'])

    def test_admin_cannot_mutate_messages_or_use_agent_inbox(self):
        sent = self.send('work')
        before = self.state()
        for command in (('send', 'admin reply'), ('ack', sent['msg_id']),
                        ('retract', sent['msg_id']), ('read', '--ack'),
                        ('read', '--pending'), ('wait', '--timeout', '0')):
            with self.subTest(command=command):
                self.admin(*command, error='admin_read_only')
                self.assertEqual(self.state(), before)

    def test_admin_sees_retraction_placeholder_and_force_original(self):
        sent = self.run_eachboard('send', 'withdrawn private text', '--to', 'reviewer', who='writer')
        self.run_eachboard('retract', sent['msg_id'], who='writer')
        before = self.state()
        self.assertEqual(self.admin('show', sent['msg_id'])['message']['content'], '[retracted]')
        original = self.admin('show', sent['msg_id'], '--force')['message']
        self.assertEqual(original['content'], 'withdrawn private text')
        self.assertTrue(original['retracted'])
        self.assertEqual(self.state(), before)

    def test_wrong_admin_password_cannot_read_or_enumerate(self):
        sent = self.send('private')
        before = self.state()
        bad = dict(self.admin_env(EACHBOARD_CONV=self.cid), EACHBOARD_KEY='wrong-password')
        for command in (('convs',), ('status',), ('list',), ('read',), ('show', sent['msg_id']),
                        ('receipts', sent['msg_id']), ('open', '--participants', 'new,peer')):
            with self.subTest(command=command):
                self.run_eachboard(*command, env=bad, error='unauthorized')
                self.assertEqual(self.state(), before)
        report = self.run_eachboard('doctor', env=bad)
        self.assertFalse(report['identity']['authorized'])
        self.assertNotIn('wrong-password', str(report))

    def test_admin_is_reserved_and_participant_cannot_open(self):
        before = self.state()
        self.run_eachboard('open', '--participants', 'admin,writer', error='invalid_request')
        self.run_eachboard('open', '--participants', 'new,peer', who='writer', error='missing_credentials')
        self.assertEqual(self.state(), before)

    def test_first_open_establishes_injected_password_without_json_or_plaintext(self):
        workspace = self.base / 'fresh'
        workspace.mkdir()
        self.cwd = workspace
        self.run_eachboard('init')
        root = workspace / '.eachboard'
        # The obsolete admin variable alone is not authentication.
        old = dict(self.env, EACHBOARD_ADMIN_CODE='obsolete')
        self.run_eachboard('open', '--participants', 'a,b', env=old, error='missing_credentials')
        self.assertFalse((root / 'admin.hash').exists())
        opened = self.run_eachboard('open', '--participants', 'a,b')
        self.assertEqual((root / 'HEAD').read_text().strip(), opened['conv_id'])
        self.assertEqual(len((root / 'admin.hash').read_text()), 64)
        self.assertEqual(list(root.rglob('*.json')), [])
        meta = root / 'conversations' / opened['conv_id'] / 'meta'
        self.assertTrue(meta.read_text().startswith('eachboard-conversation 1\ncreated '))
        for p in root.rglob('*'):
            if p.is_file():
                raw = p.read_bytes()
                self.assertNotIn(self.admin_password.encode(), raw)
                for key in opened['keys'].values():
                    self.assertNotIn(key.encode(), raw)

    def test_missing_password_and_unestablished_admin_fail_without_creating_credentials(self):
        missing = self.admin_env()
        missing.pop('EACHBOARD_KEY')
        self.run_eachboard('convs', env=missing, error='missing_credentials')
        workspace = self.base / 'empty'
        workspace.mkdir()
        self.cwd = workspace
        self.run_eachboard('init')
        self.run_eachboard('convs', env=self.admin_env(), error='unauthorized')
        self.assertFalse((workspace / '.eachboard/admin.hash').exists())
