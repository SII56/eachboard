"""In-process sessions must not share credentials, project state or output mode."""
import contextlib
import io
import json
import os
import subprocess
import sys
from unittest.mock import patch

from eachboard import BoardContext
from eachboard.cli import main
from support import APP, BoardTestCase


class ContextTests(BoardTestCase):
    def invoke(self, ctx, *args, code=0):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(['--json', *args], context=ctx)
        self.assertEqual(result, code, output.getvalue())
        return json.loads(output.getvalue())

    def test_two_identities_in_one_process_are_isolated(self):
        writer = BoardContext(self.identity('writer'), self.workspace)
        reviewer = BoardContext(self.identity('reviewer'), self.workspace)
        observer = BoardContext(self.identity('observer'), self.workspace)
        sent = self.invoke(writer, 'send', 'one process', '--to', 'reviewer')
        self.assertEqual(self.invoke(reviewer, 'status')['unseen'], 1)
        self.assertEqual(self.invoke(observer, 'status')['unseen'], 0)
        self.invoke(reviewer, 'read')
        self.assertEqual(self.invoke(reviewer, 'status')['pending'], 1)
        self.invoke(reviewer, 'ack', sent['msg_id'])
        self.assertEqual(self.invoke(reviewer, 'status')['pending'], 0)
        self.assertEqual(self.invoke(writer, 'status')['who'], 'writer')
        self.assertEqual(self.invoke(observer, 'status')['unseen'], 0)

    def test_administrator_visibility_does_not_leak_between_contexts_or_failed_auth(self):
        sent = self.run_eachboard('send', 'private', '--to', 'reviewer', who='writer')
        administrator = BoardContext(self.admin_env(EACHBOARD_CONV=self.cid), self.workspace)
        observer = BoardContext(self.identity('observer'), self.workspace)
        self.assertEqual(self.invoke(administrator, 'show', sent['msg_id'])['message']['content'], 'private')
        self.assertEqual(self.invoke(observer, 'list')['count'], 0)
        self.assertFalse(observer.store.is_admin)
        administrator.key = 'incorrect'
        self.assertEqual(self.invoke(administrator, 'list', code=1)['code'], 'unauthorized')
        self.assertFalse(administrator.store.is_admin)

    def test_context_keeps_environment_snapshot(self):
        env = self.identity('reviewer')
        ctx = BoardContext(env, self.workspace)
        env['EACHBOARD_KEY'] = 'changed'
        env['EACHBOARD_WHO'] = 'observer'
        with patch.dict(os.environ, {'EACHBOARD_KEY': 'unrelated', 'EACHBOARD_WHO': 'writer'}):
            report = self.invoke(ctx, 'status')
        self.assertEqual(report['who'], 'reviewer')
        self.assertEqual(report['conv_id'], self.cid)

    def test_context_follows_head_unless_conversation_is_explicit(self):
        env = self.identity('writer')
        env.pop('EACHBOARD_CONV')
        following = BoardContext(env, self.workspace)
        pinned = BoardContext(self.identity('writer'), self.workspace)
        self.assertEqual(self.invoke(following, 'status')['conv_id'], self.cid)
        opened = self.run_eachboard('open', '--participants', 'writer,reviewer')
        # HEAD now selects a conversation with a different key. Reusing the
        # context must not silently retain the previously selected conversation.
        self.assertEqual(self.invoke(following, 'status', code=1)['code'], 'unauthorized')
        self.assertEqual(self.invoke(pinned, 'status')['conv_id'], self.cid)
        self.assertEqual((self.root / 'HEAD').read_text().strip(), opened['conv_id'])
        (self.root / 'HEAD').write_text(self.cid + '\n')
        self.assertEqual(self.invoke(following, 'status')['conv_id'], self.cid)

    def test_two_projects_in_one_process_are_isolated(self):
        alternate = self.base / 'alternate'
        alternate.mkdir()
        # Relative init paths use the explicit execution directory.
        self.invoke(BoardContext(self.env, alternate), 'init')
        opened = self.invoke(BoardContext(self.admin_env(), alternate), 'open', '--participants', 'writer,reviewer')
        env = dict(self.env, EACHBOARD_WHO='writer', EACHBOARD_KEY=opened['keys']['writer'])
        other = BoardContext(env, alternate)
        original = BoardContext(self.identity('writer'), self.workspace)
        self.invoke(original, 'send', 'original')
        self.assertEqual(self.invoke(other, 'list')['count'], 0)
        self.assertEqual(self.invoke(other, 'status')['conv_id'], opened['conv_id'])
        self.assertEqual(self.invoke(original, 'status')['conv_id'], self.cid)

    def test_relative_root_uses_execution_directory(self):
        env = dict(self.identity('writer'), EACHBOARD_ROOT='.eachboard')
        ctx = BoardContext(env, self.workspace)
        self.assertEqual(self.invoke(ctx, 'status')['root'], str(self.root.resolve()))
        self.assertEqual(self.invoke(ctx, 'doctor')['warnings'], [])

    def test_cli_output_mode_does_not_leak_between_invocations(self):
        ctx = BoardContext(self.identity('writer'), self.workspace)
        self.assertEqual(self.invoke(ctx, 'status')['who'], 'writer')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(['status'], context=ctx), 0)
        self.assertIn('Identity: writer', output.getvalue())
        self.assertFalse(ctx.json_output)
        parsed = self.invoke(ctx, 'unread', code=1)
        self.assertEqual(parsed['code'], 'invalid_arguments')

    def test_import_does_not_discover_project_or_read_credentials(self):
        program = '''from pathlib import Path
from unittest.mock import patch
with patch.object(Path, 'cwd', side_effect=AssertionError('import accessed cwd')):
    import eachboard
    import eachboard.cli
print('imported')
'''
        result = subprocess.run([sys.executable, '-B', '-c', program], cwd=APP,
                                env=self.env, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'imported')
