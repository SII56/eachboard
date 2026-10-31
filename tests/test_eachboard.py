import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

APP = Path(__file__).resolve().parents[1]


class EachboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="eachboard-tests-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.workspace = self.base / "shared"
        self.workspace.mkdir()
        self.cwd = self.workspace
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("EACHBOARD_")}
        self.env.update(EACHBOARD_ADMIN_CODE="test-admin", PYTHONDONTWRITEBYTECODE="1")
        self.run_eachboard("init")
        self.root = self.workspace / ".eachboard"
        opened = self.run_eachboard("open", "--participants", "writer,reviewer,observer")
        self.cid, self.keys = opened["conv_id"], opened["keys"]

    def identity(self, who):
        return dict(self.env, EACHBOARD_ROOT=str(self.root), EACHBOARD_CONV=self.cid,
                    EACHBOARD_WHO=who, EACHBOARD_KEY=self.keys[who])

    def run_eachboard(self, *args, who=None, error=None, env=None, stdin=None):
        p = subprocess.run([sys.executable, str(APP / "eachboard.py"), "--json", *args],
                           env=env or (self.identity(who) if who else self.env),
                           input=stdin, capture_output=True, text=True, timeout=20, cwd=getattr(self, 'cwd', APP))
        self.assertNotIn("Traceback", p.stderr)
        result = json.loads(p.stdout)
        if error:
            self.assertEqual(p.returncode, 1, result)
            self.assertEqual(result["code"], error, result)
        else:
            self.assertEqual(p.returncode, 0, result)
            self.assertTrue(result["ok"])
        return result

    def send(self, content="hello", **kwargs):
        return self.run_eachboard("send", "--content", content, who="writer", **kwargs)

    def test_receive_ack_and_replay(self):
        sent = self.send("中文\nemoji 🤖\n$HOME `literal` \\")
        first = self.run_eachboard("wait", "--timeout", "0", who="reviewer")["messages"]
        again = self.run_eachboard("read", who="reviewer")["messages"]
        self.assertEqual(first, again)
        self.assertEqual(first[0]["sender"], "writer")
        self.assertEqual(first[0]["content"], "中文\nemoji 🤖\n$HOME `literal` \\")
        self.assertEqual(first[0]["seq"], 1)
        for _ in range(2):
            self.run_eachboard("ack", sent["msg_id"], who="reviewer")
        self.assertEqual(self.run_eachboard("unread", who="reviewer")["count"], 0)
        self.assertEqual(len(self.run_eachboard("list", "--json", who="reviewer")["messages"]), 1)

    def test_sender_not_in_own_inbox(self):
        self.send()
        self.assertEqual(self.run_eachboard("unread", who="writer")["count"], 0)

    def test_short_send_and_conflicting_content(self):
        sent = self.run_eachboard('send', 'hello 🤖', '--to', 'reviewer', who='writer')
        self.assertEqual(self.run_eachboard('show', sent['msg_id'], who='reviewer')['message']['content'], 'hello 🤖')
        self.run_eachboard('send', 'one', '--content', 'two', who='writer', error='invalid_request')
        self.run_eachboard('send', who='writer', error='invalid_request')

    def test_default_text_and_json_opt_in(self):
        def text(*args, who='reviewer', success=True):
            p = subprocess.run([sys.executable, str(APP / 'eachboard.py'), *args],
                               env=self.identity(who), cwd=APP, capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0 if success else 1)
            self.assertNotIn('Traceback', p.stderr)
            return p.stdout
        self.assertIn('No messages', text('wait', '--timeout', '0'))
        sent = self.send('多行内容\n中文 🤖')
        for name in ('list', 'read', 'peek', 'unread', 'wait'):
            output = text(name, '--timeout', '0') if name == 'wait' else text(name)
            self.assertIn('多行内容\n中文 🤖', output)
            self.assertIn(sent['msg_id'], output)
            self.assertIn('writer:', output)
        self.assertEqual(self.run_eachboard('unread', who='reviewer')['count'], 1)
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

    def test_text_attachments_and_creation(self):
        env = self.identity('writer')
        def text(*args):
            p = subprocess.run([sys.executable, str(APP / 'eachboard.py'), *args], env=env,
                               cwd=APP, capture_output=True, text=True, check=True, timeout=20)
            return p.stdout
        src = self.workspace / 'source.txt'
        src.write_text('test')
        upload = self.run_eachboard('upload', str(src), who='writer')
        self.assertIn('Attachment ID: ', text('upload', str(src)))
        dest = self.workspace / 'downloaded.txt'
        self.assertIn('SHA256: ', text('download', upload['attachment_id'], str(dest)))
        self.assertEqual(dest.read_text(), 'test')
        created = text('open', '--participants', 'one,two')
        self.assertIn('Conversation: ', created)
        self.assertIn('Participant keys', created)
        self.assertIn('one: ', created)
        workspace = self.root / 'new-project'
        workspace.mkdir()
        self.assertIn('Workspace: ', text('init', str(workspace)))

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
        self.assertTrue(any("python3 eachboard.py init" in w for w in result["warnings"]))
        self.run_eachboard("status", env=dict(self.env), error="project_not_found")

    def test_error_messages_include_context(self):
        env = dict(self.identity("writer"), EACHBOARD_CONV="missing")
        p = subprocess.run([sys.executable, str(APP / "eachboard.py"), "status"], env=env,
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
        self.run_eachboard("ack", *sent_ids, who="writer")
        self.run_eachboard("send", "--content", "peer-message", who="writer")
        program = '''import commands
from types import SimpleNamespace
from core import common as c
loaded = []
original = c.load_message
def counted(*args, **kwargs):
    loaded.append(args[1])
    return original(*args, **kwargs)
c.load_message = counted
commands.cmd_read(SimpleNamespace(after_seq=0, limit=100, ack=False))
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
        self.assertTrue(index.read_bytes().startswith(b"EACHIDX1"))

    def test_read_ack_confirms_page_once(self):
        self.send("one")
        self.send("two")
        program = '''import commands
from types import SimpleNamespace
from core import common as c
calls = []
original = c.stamp_many
def counted(cid, mids, who):
    calls.append(list(mids))
    original(cid, mids, who)
c.stamp_many = counted
commands.cmd_read(SimpleNamespace(after_seq=0, limit=100, ack=True))
print("CALLS=" + str(len(calls)) + " IDS=" + str(len(calls[0]) if calls else 0))
'''
        out = subprocess.run([sys.executable, "-c", program], cwd=APP,
                             env=self.identity("reviewer"), capture_output=True, text=True, check=True)
        self.assertIn("CALLS=1 IDS=2", out.stdout)

    def test_send_with_unread_warns_but_succeeds(self):
        sent = self.send("pending")
        result = self.run_eachboard("send", "--content", "still sent", who="reviewer")
        self.assertIn("warnings", result)
        warning = result["warnings"][0]
        self.assertIn("seq 1", warning)
        self.assertIn("writer", warning)
        self.assertIn(sent["msg_id"], warning)
        self.assertNotIn(self.keys["writer"], warning)
        messages = self.run_eachboard("list", "--json", who="reviewer")["messages"]
        self.assertEqual(messages[-1]["content"], "still sent")
        self.assertEqual(self.run_eachboard("unread", who="reviewer")["count"], 1)

    def test_send_without_unread_or_after_ack_has_no_warning(self):
        first = self.run_eachboard("send", "--content", "mine", who="reviewer")
        self.assertNotIn("warnings", first)
        self.run_eachboard("ack", first["msg_id"], who="writer")
        sent = self.send("pending")
        self.run_eachboard("read", who="reviewer")
        result = self.run_eachboard("send", "--content", "warned", who="reviewer")
        self.assertIn("warnings", result)
        self.run_eachboard("ack", sent["msg_id"], who="reviewer")
        result = self.run_eachboard("send", "--content", "clean", who="reviewer")
        self.assertNotIn("warnings", result)

    def test_text_send_renders_warning(self):
        self.send("pending")
        p = subprocess.run([sys.executable, str(APP / "eachboard.py"), "send", "--content", "warn"],
                           env=self.identity("reviewer"), cwd=self.cwd,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("Message sent", p.stdout)
        self.assertIn("Warning: unread messages pending", p.stdout)

    def test_reply_to_unread_warns_and_does_not_ack(self):
        own = self.run_eachboard("send", "--content", "mine", who="reviewer")
        self.run_eachboard("ack", own["msg_id"], who="writer")
        one = self.send("one")
        two = self.send("two")
        reply = self.run_eachboard("send", "--content", "reply", "--reply-to", one["msg_id"], who="reviewer")
        self.assertIn("warnings", reply)
        self.assertEqual(reply["reply_to"], one["msg_id"])
        self.assertEqual(self.run_eachboard("unread", who="reviewer")["count"], 2)

    def test_own_messages_do_not_warn(self):
        self.run_eachboard("send", "--content", "mine", who="reviewer")
        result = self.run_eachboard("send", "--content", "again", who="reviewer")
        self.assertNotIn("warnings", result)

    def test_invisible_directed_message_does_not_block(self):
        self.run_eachboard("send", "--content", "for observer", "--to", "observer", who="writer")
        result = self.run_eachboard("send", "--content", "ok", "--to", "writer", who="reviewer")
        self.assertNotIn("warnings", result)
        self.assertEqual(self.run_eachboard("unread", who="observer")["count"], 1)

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
                msgs = self.run_eachboard("list", "--json", who="reviewer")["messages"]
                self.assertTrue(all(m["content"].endswith("x" * 2048) for m in msgs))
            sent = [f.result() for f in futures]
        got = self.run_eachboard("wait", "--timeout", "0", who="reviewer")["messages"]
        self.assertEqual([m["seq"] for m in got], list(range(1, 25)))
        self.assertEqual({m["msg_id"] for m in got}, {m["msg_id"] for m in sent})

    def test_crash_before_publish_is_invisible(self):
        program = '''import commands
from core import common as c
from types import SimpleNamespace
import os
original = c.write_json
def crash(path, *args, **kwargs):
    if path.parent.parent.name == 'messages': os._exit(9)
    original(path, *args, **kwargs)
c.write_json = crash
commands.cmd_send(SimpleNamespace(stdin=False, content='partial', to=None, reply_to=None, task_id=None, attachment=[]))
'''
        p = subprocess.run([sys.executable, "-c", program], cwd=APP, env=self.identity("writer"))
        self.assertEqual(p.returncode, 9)
        self.assertEqual(self.run_eachboard("list", "--json", who="reviewer")["count"], 0)
        sent = self.send("after crash")
        self.assertEqual(sent["seq"], 2)
        self.assertEqual(self.run_eachboard("read", who="reviewer")["messages"][0]["content"], "after crash")

    def test_output_failure_does_not_ack(self):
        self.send()
        program = '''import commands
from core import common as c
from types import SimpleNamespace
def broken(obj): raise BrokenPipeError()
c.out = broken
try: commands.cmd_wait(SimpleNamespace(timeout=0, limit=100, after_seq=0, ack=True))
except BrokenPipeError: pass
'''
        subprocess.run([sys.executable, "-c", program], cwd=APP, env=self.identity("reviewer"), check=True)
        self.assertEqual(self.run_eachboard("unread", who="reviewer")["count"], 1)

    def test_single_objects_and_idempotent_concurrent_ack_journal(self):
        sent = [self.send(str(i)) for i in range(4)]
        base = self.root / 'conversations' / self.cid
        for message in sent:
            mid = message['msg_id']
            path = base / 'messages' / mid[1:3] / (mid + '.json')
            self.assertTrue(path.is_file())
            self.assertFalse((base / 'messages' / mid).exists())
        self.assertEqual(len(list((base / 'messages').glob('*/*.json'))), 4)
        ids = [m['msg_id'] for m in sent]
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
        self.run_eachboard('ack', one['msg_id'], who='reviewer')
        path = self.root / 'conversations' / self.cid / 'state' / 'reviewer.read'
        with path.open('ab') as stream:
            stream.write(two['msg_id'][:12].encode())
        self.assertEqual(self.run_eachboard('read', who='reviewer')['messages'][0]['msg_id'], two['msg_id'])
        self.run_eachboard('ack', two['msg_id'], who='reviewer')
        self.assertEqual(path.read_text().splitlines(), [one['msg_id'], two['msg_id']])

    def test_crash_between_object_and_index_is_recovered(self):
        program = '''import commands
from core import common as c
from types import SimpleNamespace
import os
def crash(*args): os._exit(9)
c.append_index = crash
commands.cmd_send(SimpleNamespace(stdin=False, content='committed object', to=None, reply_to=None, task_id=None, attachment=[]))
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
        path = self.root / 'conversations' / self.cid / 'messages' / mid[1:3] / (mid + '.json')
        data = json.loads(path.read_text())
        data['content'] = 'modified'
        path.write_text(json.dumps(data))
        self.run_eachboard('show', mid, who='writer', error='checksum_mismatch')
        self.run_eachboard('read', who='reviewer', error='checksum_mismatch')

    def test_stale_sequence_counter_does_not_reuse_ids(self):
        self.send('one')
        self.send('two')
        (self.root / 'conversations' / self.cid / 'sequence').write_text('0')
        self.assertEqual(self.send('three')['seq'], 3)

    def test_index_queries_do_not_scan_the_object_tree(self):
        sent = [self.send(str(i)) for i in range(6)]
        program = '''from core import common as c
import json
cid = c.current_conv()
def forbidden(*args): raise AssertionError('unexpected full object scan')
c._scan_records = forbidden
loaded = []
original = c.load_message
def counted(*args, **kwargs):
    loaded.append(args[1])
    return original(*args, **kwargs)
c.load_message = counted
msgs = c.messages(cid, after_seq=4, limit=1)
assert len(msgs) == 1 and len(loaded) == 1
loaded.clear()
c.get_message(cid, msgs[0]['msg_id'])
assert len(loaded) == 1
print('bounded reads verified')
'''
        subprocess.run([sys.executable, '-c', program], cwd=APP, env=self.identity('reviewer'), check=True, capture_output=True)

    def test_attachment_versions_and_hash(self):
        src = self.workspace / "sample.bin"
        data = os.urandom(65536)
        src.write_bytes(data)
        a = self.run_eachboard("upload", str(src), "--snapshot", who="writer")
        src.write_bytes(b"second version")
        b = self.run_eachboard("upload", str(src), "--snapshot", who="writer")
        self.assertNotEqual(a["attachment_id"], b["attachment_id"])
        self.run_eachboard("download", "sample.bin", str(self.workspace / "ambiguous"), who="reviewer", error="attachment_not_found")
        dest = self.workspace / "download.bin"
        self.run_eachboard("download", a["attachment_id"], str(dest), who="reviewer")
        self.assertEqual(dest.read_bytes(), data)
        self.assertEqual(a["sha256"], hashlib.sha256(data).hexdigest())
        self.run_eachboard("download", b["attachment_id"], str(dest), who="reviewer", error="already_exists")
        self.run_eachboard("download", b["attachment_id"], str(dest), "--force", who="reviewer")
        self.assertEqual(dest.read_bytes(), b"second version")
        msg = self.run_eachboard("send", "--content", "file", "--attachment", a["attachment_id"], who="writer")
        self.assertEqual(msg["attachments"][0]["attachment_id"], a["attachment_id"])

    def test_corrupt_attachment_is_not_delivered(self):
        src = self.workspace / "test.txt"
        src.write_text("good")
        a = self.run_eachboard("upload", str(src), who="writer")
        src.write_text("bad")
        dest = self.workspace / "out.txt"
        self.run_eachboard("download", a["attachment_id"], str(dest), who="reviewer", error="checksum_mismatch")
        self.assertFalse(dest.exists())

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
        self.send()
        self.assertEqual(self.run_eachboard("read", "--ack", who="reviewer")["count"], 1)
        self.assertEqual(self.run_eachboard("read", who="reviewer")["count"], 0)

    def test_bad_inputs_and_identity(self):
        self.send("  ", error="invalid_request")
        self.run_eachboard("send", "--stdin", who="writer", stdin="", error="invalid_request")
        self.run_eachboard("list", "--json", "--limit", "0", who="writer", error="invalid_arguments")
        self.run_eachboard("wait", "--timeout", "-1", who="writer", error="invalid_arguments")
        self.run_eachboard("send", "--content", "hi", "--to", "nobody", who="writer", error="invalid_request")
        self.run_eachboard("open", "--participants", "writer,writer", error="invalid_request")
        self.run_eachboard("open", "--participants", "../writer", error="invalid_request")
        bad = dict(self.identity("writer"), EACHBOARD_WHO="reviewer")
        self.run_eachboard("list", "--json", env=bad, error="unauthorized")
        bad = dict(self.identity("writer"), EACHBOARD_CONV="missing")
        self.run_eachboard("list", "--json", env=bad, error="conversation_not_found")
        self.run_eachboard("download", "../admin.hash", str(self.root / "out"), who="writer", error="invalid_request")

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
        env.pop("EACHBOARD_ADMIN_CODE")
        self.run_eachboard("open", "--participants", "writer", env=env, stdin="", error="admin_code_required")


class ProjectTests(unittest.TestCase):
    run_eachboard = EachboardTests.run_eachboard
    identity = EachboardTests.identity

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='eachboard-project-')
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name) / 'shared'
        self.workspace.mkdir()
        self.cwd = self.workspace
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('EACHBOARD_')}
        self.env.update(EACHBOARD_ADMIN_CODE='test', PYTHONDONTWRITEBYTECODE='1')
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

    def test_python_entry_help(self):
        result = subprocess.run([sys.executable, str(APP / 'eachboard.py'), '--help'],
                                env=self.env, cwd=self.workspace,
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertIn('python3 eachboard.py', result.stdout)
        self.assertFalse((self.workspace / '.board').exists())

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

    def test_reference_does_not_copy_and_detects_changed_file(self):
        src = self.workspace / 'report.txt'
        src.write_text('first version')
        a = self.run_eachboard('upload', str(src), who='writer')
        self.assertEqual(a['kind'], 'reference')
        self.assertEqual(a['path'], 'report.txt')
        self.assertEqual(list((self.root / 'objects').iterdir()), [])
        self.run_eachboard('send', '--content', 'report', '--attachment', a['attachment_id'], who='writer')
        out = self.workspace / 'out.txt'
        self.run_eachboard('download', a['attachment_id'], str(out), who='reviewer')
        self.assertEqual(out.read_text(), 'first version')
        src.write_text('edited')
        target = self.workspace / 'changed.txt'
        self.run_eachboard('download', a['attachment_id'], str(target), who='reviewer', error='checksum_mismatch')
        self.assertFalse(target.exists())

    def test_snapshot_dedup_and_source_deletion(self):
        src = self.workspace / 'result.bin'
        data = os.urandom(65536)
        src.write_bytes(data)
        a = self.run_eachboard('upload', str(src), '--snapshot', who='writer')
        b = self.run_eachboard('upload', str(src), '--snapshot', who='writer')
        self.assertNotEqual(a['attachment_id'], b['attachment_id'])
        self.assertEqual(len(list((self.root / 'objects').iterdir())), 1)
        src.unlink()
        dest = self.workspace / 'restored.bin'
        self.run_eachboard('download', a['attachment_id'], str(dest), who='reviewer')
        self.assertEqual(dest.read_bytes(), data)

    def test_doctor_reports_sources_and_hides_secrets(self):
        env = dict(self.env, EACHBOARD_WHO='writer', EACHBOARD_KEY=self.keys['writer'])
        report = self.run_eachboard('doctor', env=env)
        self.assertEqual(report['root'], str(self.root.resolve()))
        self.assertEqual(report['root_source'], 'project discovery')
        self.assertEqual(report['conversation']['id'], self.cid)
        self.assertEqual(report['conversation']['source'], 'config.json')
        self.assertTrue(report['conversation']['exists'])
        self.assertTrue(report['identity']['authorized'])
        self.assertEqual(report['warnings'], [])
        text = subprocess.run([sys.executable, str(APP / 'eachboard.py'), 'doctor'], env=env,
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

    def test_project_boundary_and_separate_ack_state(self):
        external = Path(self.tmp.name) / 'outside.txt'
        external.write_text('outside')
        link = self.workspace / 'escape.txt'
        link.symlink_to(external)
        for p in (external, link, self.root / 'config.json'):
            self.run_eachboard('upload', str(p), who='writer', error='outside_workspace')
        src = self.workspace / 'good.txt'
        src.write_text('good')
        a = self.run_eachboard('upload', str(src), who='writer')
        self.run_eachboard('download', a['attachment_id'], str(external), who='reviewer', error='outside_workspace')
        sent = self.run_eachboard('send', '--content', 'hello', who='writer')
        self.run_eachboard('ack', sent['msg_id'], who='reviewer')
        self.assertEqual((self.root / 'conversations' / self.cid / 'state' / 'reviewer.read').read_text().splitlines(), [sent['msg_id']])
        self.assertFalse((self.root / 'conversations' / self.cid / 'messages' / sent['msg_id'] / 'read').exists())


if __name__ == "__main__":
    unittest.main()
