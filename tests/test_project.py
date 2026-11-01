import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from support import APP, BoardTestCase


class ProjectTests(BoardTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='eachboard-project-')
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / 'shared'
        self.workspace.mkdir()
        self.cwd = self.workspace
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('EACHBOARD_')}
        self.admin_password = 'test'
        self.env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(APP))
        self.run_eachboard('init')
        self.root = self.workspace / '.eachboard'
        opened = self.run_eachboard('open', '--participants', 'writer,reviewer,observer')
        self.cid, self.keys = opened['conv_id'], opened['keys']

    def test_old_namespace_is_not_used(self):
        isolated = Path(self.tmp.name) / 'old-project'
        old_root = isolated / '.board'
        old_root.mkdir(parents=True)
        marker = old_root / 'config.json'
        marker.write_text('{"conversation": "old-test"}')
        self.cwd = isolated
        env = dict(self.env, BOARD_ROOT=str(old_root), BOARD_WHO='writer',
                   BOARD_KEY=self.keys['writer'], BOARD_CONV=self.cid)
        report = self.run_eachboard('doctor', env=env)
        self.assertIsNone(report['root'])
        self.assertFalse((isolated / '.eachboard').exists())
        self.assertEqual(marker.read_text(), '{"conversation": "old-test"}')
        self.cwd = self.workspace
        self.run_eachboard('status', env=env, error='missing_credentials')

    def test_module_entry_help(self):
        result = subprocess.run([sys.executable, '-m', 'eachboard', '--help'],
                                env=self.env, cwd=self.workspace,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertIn('python3 -m eachboard', result.stdout)
        self.assertFalse((self.workspace / '.board').exists())

    def test_old_projects_are_refused_without_writes_or_migration(self):
        config_path = self.root / 'config.json'
        config = {'version': 1, 'conversation': self.cid}
        config_path.write_text(json.dumps(config))
        (self.root / 'FORMAT').unlink()
        (self.root / 'HEAD').unlink()
        # Deliberately stale cache: refusal must happen before any recovery.
        (self.root / 'conversations' / self.cid / 'index').write_bytes(b'EACHIDX2' + bytes(8))
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        calls = [('status',), ('read',), ('list',), ('wait', '--timeout', '0'),
                 ('send', 'new message'), ('show', 'mdeadbeef'), ('ack', 'mdeadbeef'),
                 ('retract', 'mdeadbeef'), ('receipts', 'mdeadbeef'), ('convs',),
                 ('open', '--participants', 'writer,reviewer')]
        for args in calls:
            with self.subTest(command=args):
                self.run_eachboard(*args, who='writer', error='unsupported_format')
        report = self.run_eachboard('doctor', who='writer')
        self.assertFalse(report['storage_supported'])
        self.assertIn('unsupported project storage format', report['warnings'][0])
        self.run_eachboard('init', error='already_exists')
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_new_projects_declare_format_without_config_json(self):
        self.assertEqual((self.root / 'FORMAT').read_bytes(), b'eachboard 2\n')
        self.assertEqual((self.root / 'HEAD').read_text(), self.cid + '\n')
        self.assertFalse((self.root / 'config.json').exists())
        for value in (b'', b'eachboard 1\n', b'eachboard 2', b'eachboard 2\nextra'):
            with self.subTest(marker=value):
                (self.root / 'FORMAT').write_bytes(value)
                self.run_eachboard('status', who='writer', error='unsupported_format')

    def test_head_selects_the_latest_opened_conversation(self):
        env = dict(self.env, EACHBOARD_WHO='writer', EACHBOARD_KEY=self.keys['writer'])
        initial = self.run_eachboard('status', env=env)
        self.assertEqual((initial['conv_id'], initial['conversation_source']), (self.cid, 'HEAD'))
        opened = self.run_eachboard('open', '--participants', 'writer,reviewer')
        self.assertEqual((self.root / 'HEAD').read_text(), opened['conv_id'] + '\n')
        env['EACHBOARD_KEY'] = opened['keys']['writer']
        self.assertEqual(self.run_eachboard('status', env=env)['conv_id'], opened['conv_id'])
        self.assertEqual(self.run_eachboard('status', who='writer')['conv_id'], self.cid)
        self.assertFalse((self.root / 'config.json').exists())

    def test_empty_or_missing_head_means_no_active_conversation(self):
        env = dict(self.env, EACHBOARD_WHO='writer', EACHBOARD_KEY=self.keys['writer'])
        head = self.root / 'HEAD'
        for value in (b'', b'\n', None):
            with self.subTest(head=value):
                if value is None:
                    head.unlink()
                else:
                    head.write_bytes(value)
                result = self.run_eachboard('status', env=env, error='missing_credentials')
                self.assertIn('no active conversation', result['error'])
                report = self.run_eachboard('doctor', env=env)
                self.assertIsNone(report['conversation']['id'])
                self.assertIsNone(report['conversation']['head'])
                self.assertTrue(report['storage_supported'])
        opened = self.run_eachboard('open', '--participants', 'writer,reviewer')
        self.assertEqual(head.read_text(), opened['conv_id'] + '\n')

    def test_invalid_head_is_diagnosed_and_environment_override_still_works(self):
        env = dict(self.env, EACHBOARD_WHO='writer', EACHBOARD_KEY=self.keys['writer'])
        for value in (b'../escape\n', b'one\ntwo\n', b'\xff', b'\x00', b'a' * 130):
            with self.subTest(head=value):
                (self.root / 'HEAD').write_bytes(value)
                self.run_eachboard('status', env=env, error='invalid_head')
                selected = self.run_eachboard('status', who='writer')
                self.assertEqual(selected['conv_id'], self.cid)
                self.assertEqual(selected['conversation_source'], 'EACHBOARD_CONV environment')
                report = self.run_eachboard('doctor', env=self.identity('writer'))
                self.assertTrue(report['identity']['authorized'])
                self.assertTrue(any('invalid HEAD' in warning for warning in report['warnings']))
                self.assertEqual((self.root / 'HEAD').read_bytes(), value)

    def test_unsupported_nested_project_does_not_fall_back_to_parent(self):
        nested = self.workspace / 'nested'
        legacy = nested / '.eachboard'
        legacy.mkdir(parents=True)
        marker = legacy / 'config.json'
        marker.write_text('{"version":1,"conversation":null}')
        self.cwd = nested
        env = dict(self.env, EACHBOARD_WHO='writer', EACHBOARD_KEY=self.keys['writer'])
        self.run_eachboard('status', env=env, error='unsupported_format')
        report = self.run_eachboard('doctor', env=env)
        self.assertEqual(report['root'], str(legacy.resolve()))
        self.assertFalse(report['storage_supported'])
        self.assertEqual(list(legacy.iterdir()), [marker])

    def test_failed_head_replacement_keeps_the_previous_pointer(self):
        from eachboard import BoardContext
        ctx = BoardContext(self.identity('writer'), self.workspace)
        before = (self.root / 'HEAD').read_bytes()
        with patch('eachboard.storage.files.os.replace', side_effect=OSError('simulated failure')):
            with self.assertRaises(OSError):
                ctx.store.write_head('another')
        self.assertEqual((self.root / 'HEAD').read_bytes(), before)
        self.assertEqual(list(self.root.glob('.tmp-*')), [])

    def test_parallel_open_publishes_complete_head_pointers(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.run_eachboard, 'open', '--participants', 'writer,reviewer')
                       for _ in range(4)]
            while not all(future.done() for future in futures):
                raw = (self.root / 'HEAD').read_text()
                self.assertRegex(raw, r'^[0-9a-f]{16}\n$')
                self.assertTrue((self.root / 'conversations' / raw.strip() / 'meta').is_file())
                time.sleep(0.005)
            opened = [future.result() for future in futures]
        head = (self.root / 'HEAD').read_text().strip()
        self.assertIn(head, {item['conv_id'] for item in opened})
        self.assertEqual((self.root / 'FORMAT').read_bytes(), b'eachboard 2\n')

    def test_parent_discovery_with_session_environment(self):
        nested = self.workspace / 'nested' / 'child'
        nested.mkdir(parents=True)
        self.cwd = nested
        env = dict(self.env, EACHBOARD_ROOT='',
                   EACHBOARD_WHO='writer', EACHBOARD_KEY=self.keys['writer'])
        result = self.run_eachboard('status', env=env)
        self.assertEqual(result['root'], str(self.root.resolve()))
        self.assertEqual(result['conv_id'], self.cid)
        self.assertFalse((self.root / 'profiles').exists())
        self.assertIn('.eachboard/', (self.workspace / '.gitignore').read_text())
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        # No plaintext participant key is saved by initialization or conversation creation.
        for path in self.root.rglob('*'):
            if path.is_file():
                for key in self.keys.values():
                    self.assertNotIn(key.encode(), path.read_bytes())
        self.run_eachboard('status', env=self.env, error='missing_credentials')


    def test_doctor_reports_sources_and_hides_secrets(self):
        env = dict(self.env, EACHBOARD_WHO='writer', EACHBOARD_KEY=self.keys['writer'])
        report = self.run_eachboard('doctor', env=env)
        self.assertEqual(report['root'], str(self.root.resolve()))
        self.assertEqual(report['root_source'], 'project discovery')
        self.assertEqual(report['conversation']['id'], self.cid)
        self.assertEqual(report['conversation']['source'], 'HEAD')
        self.assertEqual(report['conversation']['head'], self.cid)
        self.assertTrue(report['conversation']['exists'])
        self.assertTrue(report['identity']['authorized'])
        self.assertEqual(report['warnings'], [])
        text = subprocess.run([sys.executable, '-m', 'eachboard', 'doctor'], env=env,
                              cwd=self.workspace, capture_output=True, text=True, timeout=20).stdout
        self.assertIn('Root source', text)
        self.assertNotIn(self.keys['writer'], text)
        self.assertNotIn(self.keys['reviewer'], text)
        partial = self.run_eachboard('doctor', env=dict(self.env))
        self.assertFalse(partial['identity']['key_present'])
        self.assertTrue(any('EACHBOARD_WHO' in w for w in partial['warnings']))
        wrong = self.run_eachboard('doctor', env=dict(env, EACHBOARD_KEY='wrong'))
        self.assertFalse(wrong['identity']['authorized'])
        self.assertTrue(any('rejected' in w for w in wrong['warnings']))

    def test_doctor_flags_environment_conflicts(self):
        alternate = Path(self.tmp.name) / 'alternate'
        alternate.mkdir()
        self.run_eachboard('init', str(alternate))
        override_root = dict(self.env, EACHBOARD_ROOT=str(alternate / '.eachboard'))
        report = self.run_eachboard('doctor', env=override_root)
        self.assertEqual(report['root'], str((alternate / '.eachboard').resolve()))
        self.assertEqual(report['root_source'], 'EACHBOARD_ROOT environment')
        self.assertTrue(any('EACHBOARD_ROOT overrides' in w for w in report['warnings']))
        self.run_eachboard('open', '--participants', 'writer,reviewer')
        override = dict(self.env, EACHBOARD_WHO='writer', EACHBOARD_KEY=self.keys['writer'],
                        EACHBOARD_CONV=self.cid)
        report = self.run_eachboard('doctor', env=override)
        self.assertEqual(report['conversation']['id'], self.cid)
        self.assertEqual(report['conversation']['source'], 'EACHBOARD_CONV environment')
        self.assertTrue(any('EACHBOARD_CONV overrides' in w for w in report['warnings']))

    def test_separate_ack_state_uses_identity_journal(self):
        sent = self.run_eachboard('send', '--content', 'hello', who='writer')
        self.run_eachboard('read', who='reviewer')
        self.run_eachboard('ack', sent['msg_id'], who='reviewer')
        self.assertEqual((self.root / 'conversations' / self.cid / 'state' / 'reviewer.read').read_text().splitlines(), [sent['msg_id']])
        self.assertFalse((self.root / 'conversations' / self.cid / 'messages' / sent['msg_id'] / 'read').exists())

    def test_status_counts_are_side_effect_free(self):
        self.send("one")
        self.run_eachboard("send", "--content", "two", "--to", "observer", who="writer")
        first = self.run_eachboard("status", who="reviewer")
        self.assertEqual((first["unseen"], first["pending"]), (1, 1))
        second = self.run_eachboard("status", who="reviewer")
        self.assertEqual((second["unseen"], second["pending"]), (1, 1))
        state = self._conversation_path("state")
        self.assertFalse(state.joinpath("reviewer.seen").exists())
        self.assertFalse(state.joinpath("reviewer.read").exists())
        # Counting is not limited by a page size and ignores other people's mail.
        for n in range(4):
            self.send("bulk-" + str(n))
        self.assertEqual(self.run_eachboard("status", who="reviewer")["unseen"], 5)

    def test_status_tracks_unseen_pending_and_completed(self):
        first = self.send("one")
        self.run_eachboard("read", who="reviewer")
        self.run_eachboard("ack", first["msg_id"], who="reviewer")
        second = self.send("two")
        status = self.run_eachboard("status", who="reviewer")
        self.assertEqual((status['unseen'], status['pending']), (1, 1))
        self.run_eachboard('read', who='reviewer')
        status = self.run_eachboard('status', who='reviewer')
        self.assertEqual((status['unseen'], status['pending']), (0, 1))
        self.run_eachboard('ack', second['msg_id'], who='reviewer')
        status = self.run_eachboard('status', who='reviewer')
        self.assertEqual((status['unseen'], status['pending']), (0, 0))

    def test_status_skips_completed_bodies(self):
        completed = [self.send('done-' + str(i)) for i in range(3)]
        self.run_eachboard('read', who='reviewer')
        self.run_eachboard('ack', *[m['msg_id'] for m in completed], who='reviewer')
        program = '''import json
from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
cid = c.current_conv()
original = c.store.load_message
loaded = []
def counted(*args, **kwargs):
    loaded.append(args[1])
    return original(*args, **kwargs)
c.store.load_message = counted
print(json.dumps({'counts': c.store.counts(cid), 'loaded': loaded}))
'''
        def probe():
            result = subprocess.run([sys.executable, '-B', '-c', program], cwd=APP,
                                    env=self.identity('reviewer'), capture_output=True,
                                    text=True, check=True)
            return json.loads(result.stdout)

        result = probe()
        self.assertEqual(result, {'counts': {'unseen': 0, 'pending': 0}, 'loaded': []})
        fresh = self.send('new')
        result = probe()
        self.assertEqual(result['counts'], {'unseen': 1, 'pending': 1})
        self.assertEqual(result['loaded'], [fresh['msg_id']])
        self.run_eachboard('read', who='reviewer')
        self.assertEqual(probe()['counts'], {'unseen': 0, 'pending': 1})

    def test_read_default_is_new_only_and_does_not_replay(self):
        sent = self.send("one")
        first = self.run_eachboard("read", who="reviewer")
        self.assertEqual([m["msg_id"] for m in first["messages"]], [sent["msg_id"]])
        self.assertEqual(self.run_eachboard("read", who="reviewer")["count"], 0)
        self.assertEqual(self.run_eachboard("read", "--pending", who="reviewer")["count"], 1)
        self.run_eachboard("ack", sent["msg_id"], who="reviewer")
        self.assertEqual(self.run_eachboard("read", "--pending", who="reviewer")["count"], 0)

    def test_batch_ack_with_unseen_message_writes_nothing(self):
        first = self.send('one')
        self.run_eachboard('read', who='reviewer')
        second = self.send('two')
        self.run_eachboard('ack', first['msg_id'], second['msg_id'],
                           who='reviewer', error='message_not_seen')
        self.assertFalse(self._conversation_path('state/reviewer.read').exists())
        self.run_eachboard('read', who='reviewer')
        self.run_eachboard('ack', first['msg_id'], second['msg_id'], who='reviewer')
        self.assertEqual(self.run_eachboard('status', who='reviewer')['pending'], 0)

    def test_ack_requires_this_identity_to_have_seen_the_message(self):
        sent = self.send('broadcast')
        self.run_eachboard('read', who='reviewer')
        self.run_eachboard('ack', sent['msg_id'], who='observer', error='message_not_seen')
        self.run_eachboard('show', sent['msg_id'], who='observer')
        self.run_eachboard('ack', sent['msg_id'], who='observer')
        self.run_eachboard('ack', sent['msg_id'], who='observer')
        self.assertEqual(self._conversation_path('state/observer.read').read_text().splitlines(),
                         [sent['msg_id']])

    def test_ack_state_writer_cannot_bypass_seen_requirement(self):
        sent = self.send('unseen')
        program = '''from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
try:
    c.store.stamp_many(c.current_conv(), [MID], c.who)
except EachboardError as error:
    print(error.code)
else:
    raise AssertionError('unseen acknowledgement accepted')
'''.replace('MID', repr(sent['msg_id']))
        result = subprocess.run([sys.executable, '-B', '-c', program], cwd=APP,
                                env=self.identity('reviewer'), capture_output=True,
                                text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'message_not_seen')
        self.assertFalse(self._conversation_path('state/reviewer.read').exists())

    def test_wait_ack_records_seen_before_acknowledgement(self):
        sent = self.send('waiting')
        self.run_eachboard('wait', '--timeout', '0', '--ack', who='reviewer')
        for name in ('reviewer.seen', 'reviewer.read'):
            self.assertEqual(self._conversation_path('state/' + name).read_text().splitlines(),
                             [sent['msg_id']])

    def test_read_limit_marks_only_the_displayed_page(self):
        for n in range(4):
            self.send(str(n))
        page = self.run_eachboard("read", "--limit", "2", who="reviewer")
        self.assertEqual([m["content"] for m in page["messages"]], ["0", "1"])
        self.run_eachboard("send", "--content", "blocked", who="reviewer", error="unread_messages")
        rest = self.run_eachboard("read", "--limit", "10", who="reviewer")
        self.assertEqual([m["content"] for m in rest["messages"]], ["2", "3"])
        self.run_eachboard("send", "--content", "allowed", who="reviewer")

    def test_wait_ignores_old_pending(self):
        sent = self.send("one")
        self.run_eachboard("read", who="reviewer")
        # Seen but not acknowledged: waiting must not return it immediately.
        result = self.run_eachboard("wait", "--timeout", "0", who="reviewer")
        self.assertEqual(result["count"], 0)
        self.assertEqual(self.run_eachboard("status", who="reviewer")["pending"], 1)
        self.run_eachboard("ack", sent["msg_id"], who="reviewer")

    def test_wait_returns_message_arriving_during_the_wait(self):
        self.run_eachboard("read", who="reviewer")
        def delayed_send():
            time.sleep(0.5)
            return self.send("late")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            sent = pool.submit(delayed_send)
            got = self.run_eachboard("wait", "--timeout", "10", who="reviewer")
            # Propagate a sender failure to this test, rather than just logging
            # an exception from a timer thread and obscuring the original error.
            sent.result(timeout=20)
        self.assertEqual([m["content"] for m in got["messages"]], ["late"])

    def test_read_cursor_does_not_skip_earlier_unseen_messages(self):
        first = self.send('first')
        second = self.send('second')
        page = self.run_eachboard('read', '--after-seq', '1', who='reviewer')
        self.assertEqual([m['msg_id'] for m in page['messages']], [second['msg_id']])
        self.assertEqual(self.run_eachboard('status', who='reviewer')['unseen'], 1)
        remaining = self.run_eachboard('read', who='reviewer')
        self.assertEqual([m['msg_id'] for m in remaining['messages']], [first['msg_id']])

    def test_wait_timeout_leaves_state_unchanged(self):
        self.run_eachboard("wait", "--timeout", "0", who="reviewer")
        self.assertFalse(self._conversation_path("state").joinpath("reviewer.seen").exists())

    def test_removed_aliases_are_rejected(self):
        for name in ("unread", "peek"):
            self.run_eachboard(name, who="reviewer", error="invalid_arguments")

    def test_status_text_output_shows_both_counters(self):
        self.send("one")
        p = subprocess.run([sys.executable, "-m", "eachboard", "status"],
                           env=self.identity("reviewer"), cwd=self.cwd,
                           capture_output=True, text=True, check=True)
        self.assertIn("Unseen: 1", p.stdout)
        self.assertIn("Pending: 1", p.stdout)
