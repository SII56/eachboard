"""Init establishes the admin key; commands resolve identity from the key alone."""
import contextlib
import io
import json
from unittest.mock import patch
from eachboard import BoardContext
from eachboard.cli import main
from support import BoardTestCase


class KeyAuthTests(BoardTestCase):
    def invoke(self, workspace, *args, answers=(), env=None, code=0):
        output = io.StringIO()
        with patch('eachboard.commands.project.sys.stdin.isatty', return_value=True), \
             patch('eachboard.commands.project.getpass.getpass', side_effect=answers) as prompt, \
             contextlib.redirect_stdout(output):
            result = main(['--json', *args], context=BoardContext(env or self.env, workspace))
        self.assertEqual(result, code, output.getvalue())
        return json.loads(output.getvalue()), prompt.call_count

    def fresh(self):
        workspace = self.base / 'fresh'
        workspace.mkdir()
        return workspace, workspace / '.eachboard'

    def test_init_custom_key_confirmed_and_never_echoed(self):
        workspace, root = self.fresh()
        result, calls = self.invoke(workspace, 'init', answers=['custom-key', 'custom-key'])
        self.assertEqual(calls, 2)
        self.assertNotIn('custom-key', str(result))
        self.assertEqual((root / 'admin.hash').read_text(), BoardContext(self.env).store.sha('custom-key'))

    def test_enter_generates_key_and_returns_it_once(self):
        workspace, root = self.fresh()
        result, calls = self.invoke(workspace, 'init', answers=[''])
        self.assertEqual(calls, 1)
        key = result['admin_key']
        self.assertGreaterEqual(len(key), 32)
        env = dict(self.env, EACHBOARD_KEY=key)
        opened, calls = self.invoke(workspace, 'open', '--participants', 'a,b', env=env)
        self.assertEqual(calls, 0)
        self.assertNotIn(key, str(opened))
        self.assertEqual((root / 'admin.hash').read_text(), BoardContext(env).store.sha(key))

    def test_mismatch_creates_no_project(self):
        workspace, root = self.fresh()
        result, _ = self.invoke(workspace, 'init', answers=['one', 'two'], code=1)
        self.assertEqual(result['code'], 'invalid_request')
        self.assertFalse(root.exists())
        self.assertFalse((workspace / '.gitignore').exists())

    def test_open_never_prompts_even_in_terminal(self):
        result, calls = self.invoke(self.workspace, 'open', '--participants', 'a,b',
                                    env=self.admin_env())
        self.assertTrue(result['ok'])
        self.assertEqual(calls, 0)
        result, calls = self.invoke(self.workspace, 'open', '--participants', 'a,b', code=1)
        self.assertEqual(result['code'], 'missing_credentials')
        self.assertEqual(calls, 0)

    def test_who_is_ignored_and_cannot_impersonate(self):
        for claimed in ('admin', 'reviewer', '../invalid'):
            env = dict(self.identity('writer'), EACHBOARD_WHO=claimed)
            self.assertEqual(self.run_eachboard('status', env=env)['who'], 'writer')
            self.run_eachboard('open', '--participants', 'a,b', env=env, error='unauthorized')
        self.assertEqual(self.run_eachboard('doctor', env=self.identity('reviewer'))['identity']['who'], 'reviewer')

    def test_duplicate_key_mapping_fails_closed(self):
        from eachboard.storage import MessageStore
        store = MessageStore(self.root, None)
        meta = store.read_metadata(self.cid)
        meta['keys']['observer'] = meta['keys']['writer']
        store.write_metadata(store.conv_dir(self.cid), meta)
        self.run_eachboard('list', who='writer', error='invalid_data')

    def test_noninteractive_init_uses_injected_key_or_generates(self):
        workspace, root = self.fresh()
        self.cwd = workspace
        created = self.run_eachboard('init', env=self.env)
        self.assertIn('admin_key', created)
        self.run_eachboard('open', '--participants', 'a,b',
                           env=dict(self.env, EACHBOARD_KEY=created['admin_key']))

    def test_open_does_not_create_missing_admin_hash(self):
        (self.root / 'admin.hash').unlink()
        self.run_eachboard('open', '--participants', 'a,b', env=self.admin_env(), error='invalid_data')
        self.assertFalse((self.root / 'admin.hash').exists())
