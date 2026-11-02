import concurrent.futures
import hashlib
import json
import subprocess
import sys
import time

from support import APP, BoardTestCase


class MessageTests(BoardTestCase):
    def test_receive_ack_and_replay(self):
        sent = self.send("中文\nemoji 🤖\n$HOME `literal` \\")
        first = self.run_eachboard("wait", "--timeout", "0", who="reviewer")["messages"]
        # The wait displayed the message, so a plain read has nothing new.
        self.assertEqual(self.run_eachboard("read", who="reviewer")["count"], 0)
        again = self.run_eachboard("read", "--pending", who="reviewer")["messages"]
        self.assertEqual(first, again)
        self.assertEqual(first[0]["sender"], "writer")
        self.assertEqual(first[0]["content"], "中文\nemoji 🤖\n$HOME `literal` \\")
        self.assertEqual(first[0]["seq"], 1)
        for _ in range(2):
            self.run_eachboard("ack", sent["msg_id"], who="reviewer")
        status = self.run_eachboard("status", who="reviewer")
        self.assertEqual((status["unseen"], status["pending"]), (0, 0))
        self.assertEqual(len(self.run_eachboard("list", "--json", who="reviewer")["messages"]), 1)

    def test_sender_not_in_own_inbox(self):
        self.send()
        status = self.run_eachboard("status", who="writer")
        self.assertEqual((status["unseen"], status["pending"]), (0, 0))

    def test_short_send_and_conflicting_content(self):
        sent = self.run_eachboard('send', 'hello 🤖', '--to', 'reviewer', who='writer')
        self.assertEqual(self.run_eachboard('show', sent['msg_id'], who='reviewer')['message']['content'], 'hello 🤖')
        self.run_eachboard('send', 'one', '--content', 'two', who='writer', error='invalid_request')
        self.run_eachboard('send', who='writer', error='invalid_request')

    def test_default_text_and_json_opt_in(self):
        def text(*args, who='reviewer', success=True):
            p = subprocess.run([sys.executable, '-m', 'eachboard', *args],
                               env=self.identity(who), cwd=APP, capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0 if success else 1)
            self.assertNotIn('Traceback', p.stderr)
            return p.stdout
        self.assertIn('No messages', text('wait', '--timeout', '0'))
        for name in ('list', 'read', 'wait'):
            # Each command consumes "new", so send a fresh message per command.
            sent = self.send('多行内容\n中文 🤖')
            output = text(name, '--timeout', '0') if name == 'wait' else text(name)
            self.assertIn('多行内容\n中文 🤖', output)
            self.assertIn(sent['msg_id'], output)
            self.assertIn('writer:', output)
        self.assertEqual(self.run_eachboard('status', who='reviewer')['unseen'], 0)
        self.assertIn('多行内容', text('show', sent['msg_id']))
        self.assertIn('Acknowledged', text('ack', sent['msg_id']))
        self.assertIn('No messages', text('read'))
        self.assertIn('Acknowledged: reviewer', text('receipts', sent['msg_id'], who='writer'))
        self.assertIn('Identity: reviewer', text('status'))
        self.assertIn('Conversation: ', text('convs'))
        self.assertIn('Message sent', text('send', '--content', 'reply'))
        self.assertIn('Error: ', text('send', '--content', ' ', success=False))
        for args in [('status', '--json'), ('--json', 'status')]:
            self.assertTrue(json.loads(text(*args))['ok'])

    def test_text_creation_commands(self):
        env = self.admin_env(EACHBOARD_ROOT=str(self.root))
        def text(*args):
            p = subprocess.run([sys.executable, '-m', 'eachboard', *args], env=env,
                               cwd=APP, capture_output=True, text=True, check=True, timeout=20)
            return p.stdout
        created = text('open', '--participants', 'one,two')
        self.assertIn('Conversation: ', created)
        self.assertIn('Participant keys', created)
        self.assertIn('one: ', created)
        workspace = self.root / 'new-project'
        workspace.mkdir()
        self.assertIn('Workspace: ', text('init', str(workspace)))

    def test_shared_relative_paths_are_plain_message_text(self):
        directory = self.workspace / 'outputs'
        directory.mkdir()
        report = directory / 'draft report.txt'
        report.write_text('shared content is never imported into eachboard')
        body = 'Please review ./outputs/draft report.txt\nThen edit ./outputs/final.txt'
        sent = self.send(body)
        received = self.run_eachboard('read', who='reviewer')['messages'][0]
        self.assertEqual(received['content'], body)
        self.assertNotIn('attachments', sent)
        self.assertNotIn('attachments', received)
        self.assertFalse((self.root / 'attachments').exists())
        self.assertFalse((self.root / 'objects').exists())
        stored = list(self._conversation_path('messages').glob('*/m*'))
        self.assertEqual(len(stored), 1)
        self.assertNotIn(report.read_text(), stored[0].read_text())
        report.write_text('updated in the shared workspace')
        self.assertEqual(self.run_eachboard('show', sent['msg_id'], who='reviewer')['message']['content'], body)

    def test_removed_file_transfer_commands_and_options_are_rejected(self):
        for args in [('upload', 'report.txt'), ('download', 'old-id', 'out.txt'),
                     ('send', 'report', '--attachment', 'old-id')]:
            with self.subTest(args=args):
                self.run_eachboard(*args, who='writer', error='invalid_arguments')
        self.assertEqual(self.run_eachboard('list', who='writer')['count'], 0)
        help_text = subprocess.run([sys.executable, '-B', '-m', 'eachboard', '--help'],
                                   cwd=APP, env=self.env, capture_output=True,
                                   text=True, check=True).stdout
        self.assertNotIn('upload', help_text)
        self.assertNotIn('download', help_text)

    def test_message_ids_cover_the_exact_file_bytes(self):
        body = 'First line\n\t"quoted" text and 中文 🤖\nLast line without newline'
        sent = self.send(body)
        mid = sent['msg_id']
        path = self._conversation_path('messages') / mid[1:3] / mid
        raw = path.read_bytes()
        self.assertTrue(raw.startswith(b'eachboard-message 1\n'))
        self.assertEqual(raw.split(b'\n\n', 1)[1], body.encode('utf-8'))
        self.assertEqual(mid, 'm' + hashlib.sha256(raw).hexdigest())
        self.assertNotIn(mid.encode(), raw)
        self.assertEqual(path.suffix, '')
        self.assertEqual(self.run_eachboard('read', who='reviewer')['messages'][0]['content'], body)

    def test_stdin_preserves_crlf_tabs_nul_and_final_newline(self):
        body = 'First\r\n\t中文 🤖\x00second\rthird\n'
        sent = self.run_eachboard('send', '--stdin', who='writer', stdin=body)
        mid = sent['msg_id']
        raw = (self._conversation_path('messages') / mid[1:3] / mid).read_bytes()
        self.assertEqual(raw.split(b'\n\n', 1)[1], body.encode('utf-8'))
        shown = self.run_eachboard('read', who='reviewer')['messages'][0]
        self.assertEqual(shown['content'], body)

    def test_recovery_rejects_json_or_misplaced_objects_in_a_new_project(self):
        sent = self.send('valid')
        shard = self._conversation_path('messages') / sent['msg_id'][1:3]
        unexpected = shard / (sent['msg_id'] + '.json')
        unexpected.write_text('{"content":"not a text message object"}')
        self._conversation_path('index').unlink()
        self.run_eachboard('list', who='writer', error='corrupt_data')
        unexpected.unlink()
        self.assertEqual(self.run_eachboard('list', who='writer')['count'], 1)

    def test_show_and_receipts(self):
        sent = self.send("reference me")
        msg = self.run_eachboard("show", sent["msg_id"], who="reviewer")["message"]
        self.assertEqual(msg["content"], "reference me")
        self.run_eachboard("ack", sent["msg_id"], who="reviewer")
        receipts = self.run_eachboard("receipts", sent["msg_id"], who="writer")
        self.assertEqual(receipts["acked_by"], ["reviewer"])
        self.assertEqual(receipts["pending"], ["observer"])

    def test_directed_reply_and_task(self):
        sent = self.run_eachboard("send", "--content", "task", "--to", "reviewer", "--task-id", "t-1", who="writer")
        self.assertEqual(self.run_eachboard("list", "--json", who="observer")["count"], 0)
        self.run_eachboard("ack", sent["msg_id"], who="observer", error="message_not_found")
        self.run_eachboard("read", who="reviewer")
        reply = self.run_eachboard("send", "--content", "done", "--to", "writer", "--reply-to", sent["msg_id"],
                               "--task-id", "t-1", who="reviewer")
        self.assertEqual(reply["reply_to"], sent["msg_id"])
        self.assertEqual(self.run_eachboard("read", who="writer")["messages"][0]["task_id"], "t-1")

    def test_uninitialized_directory_diagnostics(self):
        empty = self.base / "empty"
        empty.mkdir()
        self.cwd = empty
        result = self.run_eachboard("doctor", env=dict(self.env))
        self.assertIsNone(result["root"])
        self.assertTrue(any("python3 -m eachboard init" in w for w in result["warnings"]))
        self.run_eachboard("status", env=dict(self.env), error="project_not_found")

    def test_error_messages_include_context(self):
        env = dict(self.identity("writer"), EACHBOARD_CONV="missing")
        p = subprocess.run([sys.executable, "-m", "eachboard", "status"], env=env,
                           cwd=self.workspace, capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("missing", p.stdout)
        self.assertIn(str(self.root), p.stdout)
        self.assertIn("EACHBOARD_CONV", p.stdout)
        self.assertNotIn(self.keys["writer"], p.stdout)

    def test_read_skips_own_bodies_before_loading(self):
        sent_ids = []
        for n in range(20):
            sent_ids.append(self.run_eachboard("send", "--content", "mine-" + str(n), who="reviewer")["msg_id"])
        self.assertEqual(self.run_eachboard("read", who="writer")["count"], 20)
        self.run_eachboard("ack", *sent_ids, who="writer")
        self.run_eachboard("send", "--content", "peer-message", who="writer")
        program = '''from eachboard import commands
from types import SimpleNamespace
from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
loaded = []
original = c.store.load_message
def counted(*args, **kwargs):
    loaded.append(args[1])
    return original(*args, **kwargs)
c.store.load_message = counted
commands.cmd_read(c, SimpleNamespace(after_seq=0, limit=100, ack=False, pending=False))
print("LOADED=" + str(len(loaded)))
'''
        out = subprocess.run([sys.executable, "-c", program], cwd=APP,
                             env=self.identity("reviewer"), capture_output=True, text=True, check=True)
        self.assertIn("LOADED=1", out.stdout)

    def test_unknown_index_header_rebuilds(self):
        self.send("one")
        self.send("two")
        index = self.root / "conversations" / self.cid / "index"
        index.write_bytes(b"INVALID!" + bytes(8))
        msgs = self.run_eachboard("list", who="reviewer")["messages"]
        self.assertEqual([m["content"] for m in msgs], ["one", "two"])
        self.assertTrue(index.read_bytes().startswith(b"EACHIDX3"))

    def test_read_ack_confirms_page_once(self):
        self.send("one")
        self.send("two")
        program = '''from eachboard import commands
from types import SimpleNamespace
from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
calls = []
original = c.store.stamp_many
def counted(cid, mids, who):
    calls.append(list(mids))
    original(cid, mids, who)
c.store.stamp_many = counted
commands.cmd_read(c, SimpleNamespace(after_seq=0, limit=100, ack=True, pending=False))
print("CALLS=" + str(len(calls)) + " IDS=" + str(len(calls[0]) if calls else 0))
'''
        out = subprocess.run([sys.executable, "-c", program], cwd=APP,
                             env=self.identity("reviewer"), capture_output=True, text=True, check=True)
        self.assertIn("CALLS=1 IDS=2", out.stdout)

    def test_send_blocked_when_unseen_without_side_effects(self):
        sent = self.send("pending")
        sequence = self._conversation_path("sequence")
        before_seq = sequence.read_bytes()
        before_files = sorted(str(p) for p in self._conversation_path("messages").rglob("m*"))
        result = self.run_eachboard("send", "--content", "blocked", who="reviewer", error="unread_messages")
        self.assertIn("seq 1", result["error"])
        self.assertIn("writer", result["error"])
        self.assertIn(sent["msg_id"], result["error"])
        self.assertIn("read", result["error"])
        self.assertNotIn(self.keys["writer"], result["error"])
        self.assertEqual(sequence.read_bytes(), before_seq)
        self.assertEqual(sorted(str(p) for p in self._conversation_path("messages").rglob("m*")), before_files)
        self.assertFalse(self._conversation_path("state").joinpath("reviewer.seen").exists())

    def test_retry_without_read_stays_blocked(self):
        self.send("pending")
        for _ in range(2):
            self.run_eachboard("send", "--content", "retry", who="reviewer", error="unread_messages")
        self.assertEqual(self.run_eachboard("status", who="reviewer")["unseen"], 1)

    def test_read_allows_send_without_ack(self):
        sent = self.send("pending")
        self.run_eachboard("read", who="reviewer")
        result = self.run_eachboard("send", "--content", "now allowed", who="reviewer")
        self.assertNotIn("warnings", result)
        status = self.run_eachboard("status", who="reviewer")
        self.assertEqual((status["unseen"], status["pending"]), (0, 1))
        self.assertTrue(self._conversation_path("state").joinpath("reviewer.seen").exists())

    def test_new_message_after_read_blocks_again(self):
        self.send("one")
        self.run_eachboard("read", who="reviewer")
        self.run_eachboard("send", "--content", "allowed", who="reviewer")
        self.run_eachboard("read", who="writer")
        self.send("two")
        self.run_eachboard("send", "--content", "blocked", who="reviewer", error="unread_messages")

    def test_ack_without_read_does_not_unlock_send(self):
        sent = self.send("pending")
        error = self.run_eachboard("ack", sent["msg_id"], who="reviewer", error="message_not_seen")
        self.assertIn(sent['msg_id'], error['error'])
        self.assertIn('read', error['error'])
        self.assertFalse(self._conversation_path('state/reviewer.read').exists())
        status = self.run_eachboard('status', who='reviewer')
        self.assertEqual((status['unseen'], status['pending']), (1, 1))
        self.run_eachboard("send", "--content", "blocked", who="reviewer", error="unread_messages")
        self.run_eachboard('read', who='reviewer')
        self.run_eachboard('ack', sent['msg_id'], who='reviewer')
        self.assertEqual(self.run_eachboard('status', who='reviewer')['pending'], 0)

    def test_reply_cannot_bypass_unseen(self):
        sent = self.run_eachboard("send", "--content", "task", "--to", "reviewer", who="writer")
        self.run_eachboard("send", "--content", "reply", "--reply-to", sent["msg_id"],
                           who="reviewer", error="unread_messages")
        self.run_eachboard("read", who="reviewer")
        reply = self.run_eachboard("send", "--content", "reply", "--reply-to", sent["msg_id"], who="reviewer")
        self.assertEqual(reply["reply_to"], sent["msg_id"])
        status = self.run_eachboard("status", who="reviewer")
        self.assertEqual((status["unseen"], status["pending"]), (0, 1))

    def test_text_error_tells_user_to_read(self):
        self.send("pending")
        p = subprocess.run([sys.executable, "-m", "eachboard", "send", "--content", "blocked"],
                           env=self.identity("reviewer"), cwd=self.cwd,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Error: [unread_messages]", p.stdout)
        self.assertIn("read", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_wait_marks_seen(self):
        self.send("pending")
        self.run_eachboard("wait", "--timeout", "0", who="reviewer")
        self.run_eachboard("send", "--content", "allowed", who="reviewer")

    def test_empty_display_does_not_create_seen(self):
        self.run_eachboard("read", who="reviewer")
        self.assertFalse(self._conversation_path("state").joinpath("reviewer.seen").exists())
        self.run_eachboard("send", "--content", "ok", who="reviewer")

    def test_list_marks_only_displayed_page(self):
        for n in range(4):
            self.send(str(n))
        self.run_eachboard("list", "--after-seq", "0", "--limit", "1", who="reviewer")
        self.run_eachboard("send", "--content", "blocked", who="reviewer", error="unread_messages")
        self.run_eachboard("list", "--after-seq", "1", "--limit", "1", who="reviewer")
        self.run_eachboard("send", "--content", "blocked", who="reviewer", error="unread_messages")
        self.run_eachboard("list", "--after-seq", "0", "--limit", "10", who="reviewer")
        self.run_eachboard("send", "--content", "allowed", who="reviewer")

    def test_show_marks_only_that_message(self):
        first = self.send("one")
        second = self.send("two")
        self.run_eachboard("show", second["msg_id"], who="reviewer")
        self.run_eachboard("send", "--content", "blocked", who="reviewer", error="unread_messages")
        self.run_eachboard("show", first["msg_id"], who="reviewer")
        self.run_eachboard("send", "--content", "allowed", who="reviewer")

    def test_seen_journal_is_idempotent_and_repairs_partial_tail(self):
        first = self.send("one")
        self.run_eachboard("read", who="reviewer")
        self.run_eachboard("read", who="reviewer")
        path = self._conversation_path("state").joinpath("reviewer.seen")
        self.assertEqual(path.read_text().splitlines(), [first["msg_id"]])
        second = self.send("two")
        with path.open("ab") as stream:
            stream.write(second["msg_id"][:12].encode())
        self.run_eachboard("read", who="reviewer")
        self.assertEqual(path.read_text().splitlines(), [first["msg_id"], second["msg_id"]])

    def test_seen_state_is_per_identity(self):
        self.send("broadcast")
        self.run_eachboard("read", who="reviewer")
        self.run_eachboard("send", "--content", "allowed", who="reviewer")
        self.run_eachboard("send", "--content", "blocked", who="observer", error="unread_messages")

    def test_invisible_directed_message_does_not_block(self):
        self.run_eachboard("send", "--content", "for observer", "--to", "observer", who="writer")
        result = self.run_eachboard("send", "--content", "ok", "--to", "writer", who="reviewer")
        self.assertNotIn("warnings", result)
        self.assertEqual(self.run_eachboard("status", who="observer")["unseen"], 1)

    def test_reply_to_nonexistent_keeps_message_not_found(self):
        self.run_eachboard("send", "--content", "x", "--reply-to", "mdeadbeef", who="reviewer", error="message_not_found")

    def test_pagination_does_not_skip(self):
        for n in range(5):
            self.send(str(n))
        a = self.run_eachboard("list", "--json", "--after-seq", "0", "--limit", "2", who="reviewer")
        b = self.run_eachboard("list", "--json", "--limit", "2", "--after-seq", str(a["next_after_seq"]), who="reviewer")
        d = self.run_eachboard("list", "--json", "--after-seq", str(b["next_after_seq"]), who="reviewer")
        self.assertEqual([m["content"] for batch in (a, b, d) for m in batch["messages"]], list("01234"))

    def test_list_default_is_recent_ascending(self):
        for n in range(60):
            self.send(str(n))
        latest = self.run_eachboard("list", "--json", who="reviewer")
        self.assertEqual([m["content"] for m in latest["messages"]],
                         [str(n) for n in range(10, 60)])
        self.assertEqual(latest["next_after_seq"], 60)
        limited = self.run_eachboard("list", "--json", "--limit", "5", who="reviewer")
        self.assertEqual([m["content"] for m in limited["messages"]],
                         [str(n) for n in range(55, 60)])
        # An explicit 0 still means "paginate forward from the beginning".
        head = self.run_eachboard("list", "--json", "--after-seq", "0", "--limit", "2", who="reviewer")
        self.assertEqual([m["content"] for m in head["messages"]], ["0", "1"])
        tail = self.run_eachboard("list", "--json", "--after-seq", "57", who="reviewer")
        self.assertEqual([m["content"] for m in tail["messages"]], ["57", "58", "59"])

    def test_concurrent_publish_and_receive(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(self.send, "payload-" + str(i) + "x" * 2048) for i in range(24)]
            while not all(f.done() for f in futures):
                # Poll as a third identity: list marks what it displays as seen,
                # so polling as reviewer would consume the messages the wait
                # below is supposed to return.
                msgs = self.run_eachboard("list", "--json", who="observer")["messages"]
                self.assertTrue(all(m["content"].endswith("x" * 2048) for m in msgs))
            sent = [f.result() for f in futures]
        got = self.run_eachboard("wait", "--timeout", "0", who="reviewer")["messages"]
        self.assertEqual([m["seq"] for m in got], list(range(1, 25)))
        self.assertEqual({m["msg_id"] for m in got}, {m["msg_id"] for m in sent})

    def test_crash_before_publish_is_invisible(self):
        program = '''from eachboard import commands
from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
from types import SimpleNamespace
import os
original = c.store.atomic_write
def crash(path, *args, **kwargs):
    if path.parent.parent.name == 'messages': os._exit(9)
    original(path, *args, **kwargs)
c.store.atomic_write = crash
commands.cmd_send(c, SimpleNamespace(stdin=False, content='partial', to=None, reply_to=None, task_id=None))
'''
        p = subprocess.run([sys.executable, "-c", program], cwd=APP, env=self.identity("writer"))
        self.assertEqual(p.returncode, 9)
        self.assertEqual(self.run_eachboard("list", "--json", who="reviewer")["count"], 0)
        sent = self.send("after crash")
        self.assertEqual(sent["seq"], 2)
        self.assertEqual(self.run_eachboard("read", who="reviewer")["messages"][0]["content"], "after crash")

    def test_output_failure_does_not_mark_seen_or_ack(self):
        sent = self.send()
        calls = [
            "commands.cmd_read(c, SimpleNamespace(after_seq=0, limit=100, ack=True, pending=False))",
            "commands.cmd_read(c, SimpleNamespace(after_seq=0, limit=100, ack=True, pending=True))",
            "commands.cmd_list(c, SimpleNamespace(after_seq=None, limit=100))",
            "commands.cmd_list(c, SimpleNamespace(after_seq=0, limit=100))",
            "commands.cmd_show(c, SimpleNamespace(msg_id=" + repr(sent['msg_id']) + "))",
            "commands.cmd_wait(c, SimpleNamespace(timeout=0, limit=100, after_seq=0, ack=True))",
        ]
        program = '''from eachboard import commands
from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
from types import SimpleNamespace
def broken(obj): raise BrokenPipeError()
c.out = broken
try: {call}
except BrokenPipeError: pass
'''
        for call in calls:
            with self.subTest(command=call):
                subprocess.run([sys.executable, "-B", "-c", program.format(call=call)],
                               cwd=APP, env=self.identity("reviewer"), check=True)
                self.assertFalse(self._conversation_path('state/reviewer.seen').exists())
                self.assertFalse(self._conversation_path('state/reviewer.read').exists())
                result = self.run_eachboard("status", who="reviewer")
                self.assertEqual((result['unseen'], result['pending']), (1, 1))

    def test_single_objects_and_idempotent_concurrent_ack_journal(self):
        sent = [self.send(str(i)) for i in range(4)]
        base = self.root / 'conversations' / self.cid
        for message in sent:
            mid = message['msg_id']
            path = base / 'messages' / mid[1:3] / mid
            self.assertTrue(path.is_file())
            self.assertFalse((base / 'messages' / mid).exists())
        self.assertEqual(len(list((base / 'messages').glob('*/m*'))), 4)
        ids = [m['msg_id'] for m in sent]
        self.run_eachboard('read', who='reviewer')
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(self.run_eachboard, 'ack', mid, who='reviewer') for mid in ids * 3]
            for future in futures:
                future.result()
        log = base / 'state' / 'reviewer.read'
        self.assertEqual(set(log.read_text().splitlines()), set(ids))
        self.assertEqual(len(log.read_text().splitlines()), 4)
        self.assertEqual(len(list((base / 'state').glob('*.read'))), 1)

    def test_partial_ack_tail_is_ignored_and_repaired(self):
        one, two = self.send('one'), self.send('two')
        self.run_eachboard('read', who='reviewer')
        self.run_eachboard('ack', one['msg_id'], who='reviewer')
        path = self.root / 'conversations' / self.cid / 'state' / 'reviewer.read'
        with path.open('ab') as stream:
            stream.write(two['msg_id'][:12].encode())
        self.assertEqual(self.run_eachboard('read', '--pending', who='reviewer')['messages'][0]['msg_id'], two['msg_id'])
        self.run_eachboard('ack', two['msg_id'], who='reviewer')
        self.assertEqual(path.read_text().splitlines(), [one['msg_id'], two['msg_id']])

    def test_crash_between_object_and_index_is_recovered(self):
        program = '''from eachboard import commands
from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
from types import SimpleNamespace
import os
def crash(*args): os._exit(9)
c.store.append_index = crash
commands.cmd_send(c, SimpleNamespace(stdin=False, content='committed object', to=None, reply_to=None, task_id=None))
'''
        p = subprocess.run([sys.executable, '-c', program], cwd=APP, env=self.identity('writer'))
        self.assertEqual(p.returncode, 9)
        msgs = self.run_eachboard('read', who='reviewer')['messages']
        self.assertEqual([m['content'] for m in msgs], ['committed object'])
        self.assertEqual(self.send('next')['seq'], 2)
        self.assertFalse((self.root / 'conversations' / self.cid / '.index-dirty').exists())

    def test_missing_and_truncated_index_are_rebuilt(self):
        sent = [self.send(str(i)) for i in range(3)]
        index = self.root / 'conversations' / self.cid / 'index'
        index.unlink()
        self.assertEqual([m['msg_id'] for m in self.run_eachboard('list', who='writer')['messages']], [m['msg_id'] for m in sent])
        index.write_bytes(index.read_bytes()[:-3])
        self.assertEqual(self.run_eachboard('list', who='writer')['count'], 3)
        raw = index.read_bytes()
        entry_size = (len(raw) - 16) // 3
        index.write_bytes(raw[:-entry_size])
        self.assertEqual(self.run_eachboard('list', who='writer')['count'], 3)

    def test_object_hash_detects_changes(self):
        sent = self.send('original')
        mid = sent['msg_id']
        path = self.root / 'conversations' / self.cid / 'messages' / mid[1:3] / mid
        path.write_bytes(path.read_bytes().replace(b'original', b'modified'))
        self.run_eachboard('show', mid, who='writer', error='checksum_mismatch')
        self.run_eachboard('read', who='reviewer', error='checksum_mismatch')

    def test_stale_sequence_counter_does_not_reuse_ids(self):
        self.send('one')
        self.send('two')
        (self.root / 'conversations' / self.cid / 'sequence').write_text('0')
        self.assertEqual(self.send('three')['seq'], 3)

    def test_index_queries_do_not_scan_the_object_tree(self):
        sent = [self.send(str(i)) for i in range(6)]
        program = '''from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
import json
cid = c.current_conv()
def forbidden(*args): raise AssertionError('unexpected full object scan')
c.store._scan_records = forbidden
loaded = []
original = c.store.load_message
def counted(*args, **kwargs):
    loaded.append(args[1])
    return original(*args, **kwargs)
c.store.load_message = counted
msgs = c.store.messages(cid, after_seq=4, limit=1)
assert len(msgs) == 1 and len(loaded) == 1
loaded.clear()
c.store.get_message(cid, msgs[0]['msg_id'])
assert len(loaded) == 1
print('bounded reads verified')
'''
        subprocess.run([sys.executable, '-c', program], cwd=APP, env=self.identity('reviewer'), check=True, capture_output=True)


    def test_credentials_require_environment_not_saved_profiles(self):
        self.run_eachboard('status', error='missing_credentials')
        # Persisted files and obsolete environment variables must never become credentials.
        directory = self.root / 'profiles'
        directory.mkdir()
        (directory / 'writer.json').write_text(json.dumps({'who': 'writer', 'conv_id': self.cid, 'key': self.keys['writer']}))
        env = dict(self.env, EACHBOARD_PROFILE_ROOT=str(directory))
        self.run_eachboard('status', env=env, error='missing_credentials')
        self.run_eachboard('--profile', 'writer', 'status', error='invalid_arguments')
        self.run_eachboard('profile', 'writer', who='writer', error='invalid_arguments')
        self.run_eachboard('profiles', error='invalid_arguments')

    def test_empty_wait_respects_timeout(self):
        start = time.monotonic()
        self.assertEqual(self.run_eachboard("wait", "--timeout", "1", who="reviewer")["messages"], [])
        self.assertLess(time.monotonic() - start, 1.8)

    def test_explicit_auto_ack(self):
        sent = self.send()
        self.assertEqual(self.run_eachboard("read", "--ack", who="reviewer")["count"], 1)
        self.assertEqual(self.run_eachboard("read", who="reviewer")["count"], 0)
        seen = (self.root / "conversations" / self.cid / "state" / "reviewer.seen").read_text().splitlines()
        self.assertEqual(seen, [sent["msg_id"]])

    def test_bad_inputs_and_identity(self):
        self.send("  ", error="invalid_request")
        self.run_eachboard("send", "--stdin", who="writer", stdin="", error="invalid_request")
        self.run_eachboard("list", "--json", "--limit", "0", who="writer", error="invalid_arguments")
        self.run_eachboard("wait", "--timeout", "-1", who="writer", error="invalid_arguments")
        self.run_eachboard("send", "--content", "hi", "--to", "nobody", who="writer", error="invalid_request")
        self.run_eachboard("open", "--participants", "writer,writer", error="invalid_request")
        self.run_eachboard("open", "--participants", "../writer", error="invalid_request")
        bad = dict(self.identity("writer"), EACHBOARD_KEY="wrong-key")
        self.run_eachboard("list", "--json", env=bad, error="unauthorized")
        bad = dict(self.identity("writer"), EACHBOARD_CONV="missing")
        self.run_eachboard("list", "--json", env=bad, error="conversation_not_found")
        self.run_eachboard("show", "../admin.hash", who="writer", error="invalid_request")

    def test_dotted_identity_end_to_end(self):
        opened = self.run_eachboard('open', '--participants', 'writer-v1,reviewer.v1')
        self.cid, self.keys = opened['conv_id'], opened['keys']
        sent = self.run_eachboard('send', 'versioned identity', '--to', 'reviewer.v1', who='writer-v1')
        received = self.run_eachboard('read', who='reviewer.v1')['messages']
        self.assertEqual(received[0]['msg_id'], sent['msg_id'])
        self.run_eachboard('ack', sent['msg_id'], who='reviewer.v1')
        self.assertEqual(self.run_eachboard('read', who='reviewer.v1')['count'], 0)
        self.assertEqual(self.run_eachboard('receipts', sent['msg_id'], who='writer-v1')['acked_by'], ['reviewer.v1'])
        for name in ('.', '..', '../agent', 'agent/name', 'agent\\name', '.hidden', 'agent.'):
            self.run_eachboard('open', '--participants', 'writer-v1,' + name, error='invalid_request')
        self.run_eachboard('show', 'm..', who='writer-v1', error='invalid_request')

    def test_stdin_and_noninteractive_admin(self):
        self.run_eachboard("send", "--stdin", who="writer", stdin="-literal\n中文")
        self.assertEqual(self.run_eachboard("read", who="reviewer")["messages"][0]["content"], "-literal\n中文")
        env = dict(self.env)
        self.run_eachboard("open", "--participants", "writer", env=env, stdin="", error="missing_credentials")
