"""Terminal open uses an administrator password without requiring identity exports."""
import contextlib
import io
import json
from unittest.mock import patch

from eachboard import BoardContext
from eachboard.cli import main
from support import BoardTestCase


class InteractiveOpenTests(BoardTestCase):
    def invoke(self, env, workspace, *args, answers=(), code=0):
        output = io.StringIO()
        with patch('eachboard.commands.project.sys.stdin.isatty', return_value=True), \
                patch('eachboard.commands.project.getpass.getpass', side_effect=answers) as prompt, \
                contextlib.redirect_stdout(output):
            result = main(['--json', *args], context=BoardContext(env, workspace))
        self.assertEqual(result, code, output.getvalue())
        return json.loads(output.getvalue()), prompt.call_count

    def fresh(self):
        workspace = self.base / 'fresh-open'
        workspace.mkdir()
        self.invoke(self.env, workspace, 'init')
        return workspace, workspace / '.eachboard'

    def test_existing_project_needs_only_password_not_identity(self):
        opened, calls = self.invoke(self.env, self.workspace, 'open', '--participants', 'a,b',
                                    answers=[self.admin_password])
        self.assertEqual(calls, 1)
        self.assertEqual(set(opened['keys']), {'a', 'b'})

    def test_participant_environment_is_not_used_as_admin_password(self):
        opened, calls = self.invoke(self.identity('writer'), self.workspace, 'open',
                                    '--participants', 'a,b', answers=[self.admin_password])
        self.assertEqual(calls, 1)
        self.assertTrue(opened['ok'])

    def test_first_open_sets_password_after_confirmation(self):
        workspace, root = self.fresh()
        opened, calls = self.invoke(self.env, workspace, 'open', '--participants', 'a,b',
                                    answers=['new-admin-password', 'new-admin-password'])
        self.assertEqual(calls, 2)
        self.assertEqual((root / 'HEAD').read_text().strip(), opened['conv_id'])
        self.assertEqual(len((root / 'admin.hash').read_text()), 64)
        self.assertNotIn('new-admin-password', (root / 'admin.hash').read_text())

    def test_confirmation_mismatch_publishes_nothing(self):
        workspace, root = self.fresh()
        result, calls = self.invoke(self.env, workspace, 'open', '--participants', 'a,b',
                                   answers=['new-password', 'different-password'], code=1)
        self.assertEqual(calls, 2)
        self.assertEqual(result['code'], 'invalid_request')
        self.assertFalse((root / 'admin.hash').exists())
        self.assertEqual((root / 'HEAD').read_text(), '')
        self.assertEqual(list((root / 'conversations').iterdir()), [])

    def test_wrong_password_keeps_existing_head_and_hash(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result, calls = self.invoke(self.admin_env(), self.workspace, 'open', '--participants', 'a,b',
                                   answers=['incorrect'], code=1)
        self.assertEqual(calls, 1)
        self.assertEqual(result['code'], 'unauthorized')
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()})

    def test_empty_password_does_not_establish_admin(self):
        workspace, root = self.fresh()
        result, calls = self.invoke(self.env, workspace, 'open', '--participants', 'a,b',
                                   answers=[''], code=1)
        self.assertEqual(calls, 1)
        self.assertEqual(result['code'], 'invalid_request')
        self.assertFalse((root / 'admin.hash').exists())
