"""Terminal decoration must not change message text or machine-readable output."""
import contextlib
import io
import json
import re
import tempfile
import unittest

from eachboard import BoardContext
from eachboard.display import render, use_color


ANSI = re.compile(r'\x1b\[[0-9;]*m')


class Terminal(io.StringIO):
    def isatty(self):
        return True


class DisplayTests(unittest.TestCase):
    def message(self, **changes):
        value = {'msg_id': 'm-example', 'seq': 10, 'sender': 'alice', 'created': 1.0,
                 'to': ['bob'], 'reply_to': None, 'task_id': 'review',
                 'content': 'Line one\n\tUnseen: 100 — Error: literal text 🤖'}
        return dict(value, **changes)

    def test_colored_views_preserve_plain_text(self):
        examples = [
            {'ok': False, 'code': 'unread_messages', 'error': 'Read new messages first'},
            {'ok': True, 'unseen': 2, 'pending': 3, 'who': 'alice'},
            {'ok': True, 'messages': [self.message()], 'next_after_seq': 10},
            {'ok': True, 'message': self.message()},
            {'ok': True, 'messages': [], 'next_after_seq': 0},
            {'ok': True, 'acked': ['m-example']},
            {'ok': True, 'msg_id': 'm-example', 'not_displayed': ['carol'], 'displayed': ['bob'],
             'acked_by': ['alice'], 'pending': ['bob', 'carol']},
            {'ok': True, 'msg_id': 'm-example', 'acked_by': [], 'pending': [],
             'ack_required': False, 'retracted': True},
            {'ok': True, 'conversations': [{'conv_id': 'example'}]},
            {'ok': True, 'conversations': []},
            {'ok': True, 'seq': 10, 'msg_id': 'm-example', 'warnings': ['Check the inbox']},
            {'ok': True, 'keys': {'alice': 'example-key'}, 'ack_required': True},
            {'ok': True, 'identity': {'authorized': True}, 'ack_required': False},
        ]
        for value in examples:
            with self.subTest(value=value):
                plain = render(value)
                colored = render(value, color=True)
                self.assertIn('\x1b[', colored)
                self.assertEqual(ANSI.sub('', colored), plain)
                self.assertNotIn('\x1b[', plain)

    def test_message_body_is_not_decorated(self):
        message = self.message()
        colored = render({'message': message}, color=True)
        self.assertIn('\x1b[', colored)
        self.assertTrue(colored.endswith(message['content']))
        self.assertEqual(colored.split('\n')[-2:], message['content'].split('\n'))

    def test_message_header_uses_the_same_muted_palette_for_related_fields(self):
        message = self.message(sender='alice', to=['alice', 'bob'], reply_to='m-parent')
        output = render({'message': message}, color=True)
        header, context = output.splitlines()[:2]
        self.assertIn('\x1b[36malice\x1b[0m:  [#10 · ', header)
        self.assertIn('\x1b[33mm-example\x1b[0m]', header)
        self.assertIn('To: \x1b[36malice\x1b[0m, \x1b[36mbob\x1b[0m', context)
        self.assertIn('Reply to: \x1b[33mm-parent\x1b[0m', context)
        self.assertIn('Task: \x1b[35mreview\x1b[0m', context)
        self.assertNotIn('\x1b[1;', header + context)
        self.assertEqual(ANSI.sub('', output), render({'message': message}))

    def test_retracted_status_is_highlighted_but_forced_original_is_unchanged(self):
        message = self.message(retracted=True, retracted_by='alice',
                               retraction_id='m-event', retracted_at=2.0)
        output = render({'message': message}, color=True)
        self.assertIn('\x1b[1;33mStatus: retracted', output)
        self.assertTrue(output.endswith(message['content']))
        self.assertEqual(ANSI.sub('', output), render({'message': message}))

    def test_positive_and_zero_counts_have_distinct_emphasis(self):
        positive = render({'unseen': 2, 'pending': 3}, color=True)
        empty = render({'unseen': 0, 'pending': 0}, color=True)
        self.assertIn('\x1b[1;33m2', positive)
        self.assertIn('\x1b[36m3', positive)
        self.assertIn('\x1b[2m0', empty)

    def test_color_requires_a_terminal_and_respects_opt_outs(self):
        self.assertTrue(use_color(Terminal(), {'TERM': 'xterm-256color'}))
        self.assertTrue(use_color(Terminal(), {}))
        for env in ({'NO_COLOR': '1'}, {'NO_COLOR': ''}, {'TERM': 'dumb'}):
            with self.subTest(env=env):
                self.assertFalse(use_color(Terminal(), env))
        self.assertFalse(use_color(io.StringIO(), {'TERM': 'xterm'}))

    def test_context_auto_color_and_json_plain_output(self):
        with tempfile.TemporaryDirectory(prefix='eachboard-display-') as directory:
            ctx = BoardContext(environ={'TERM': 'xterm'}, cwd=directory)
            output = Terminal()
            with contextlib.redirect_stdout(output):
                ctx.out({'who': 'alice', 'unseen': 1})
            self.assertIn('\x1b[', output.getvalue())
            ctx.json_output = True
            output = Terminal()
            with contextlib.redirect_stdout(output):
                ctx.out({'who': 'alice', 'unseen': 1})
            self.assertNotIn('\x1b[', output.getvalue())
            self.assertEqual(json.loads(output.getvalue()), {'who': 'alice', 'unseen': 1})

    def test_context_pipe_and_no_color_remain_plain(self):
        with tempfile.TemporaryDirectory(prefix='eachboard-display-') as directory:
            for env, output in [({'TERM': 'xterm'}, io.StringIO()),
                                ({'NO_COLOR': '1'}, Terminal()),
                                ({'TERM': 'dumb'}, Terminal())]:
                with self.subTest(env=env):
                    ctx = BoardContext(environ=env, cwd=directory)
                    with contextlib.redirect_stdout(output):
                        ctx.out({'unseen': 1, 'pending': 2})
                    self.assertNotIn('\x1b[', output.getvalue())
