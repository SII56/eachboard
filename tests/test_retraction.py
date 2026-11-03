import concurrent.futures
import subprocess
import sys

from support import APP, BoardTestCase


class RetractionTests(BoardTestCase):
    def retract(self, mid, who="writer", error=None):
        return self.run_eachboard("retract", mid, who=who, error=error)

    def test_retract_hides_body_in_every_output(self):
        sent = self.send("secret body")
        event = self.retract(sent["msg_id"])
        self.assertEqual(event["retracts"], sent["msg_id"])
        self.assertEqual(event["target_seq"], 1)
        # read first: the notification is new work, the retracted original is not.
        self.assertEqual([m["content"] for m in self.run_eachboard("read", who="reviewer")["messages"]],
                         ["writer retracted message #1"])
        listed = self.run_eachboard("list", who="reviewer")["messages"]
        self.assertEqual([m["content"] for m in listed],
                         ["[retracted]", "writer retracted message #1"])
        self.assertTrue(listed[0]["retracted"])
        self.assertEqual(listed[0]["retraction_id"], event["msg_id"])
        self.assertEqual(listed[0]["retracted_by"], "writer")
        self.assertIsNotNone(listed[0]["retracted_at"])
        hidden = self.run_eachboard("show", sent["msg_id"], who="reviewer")["message"]
        self.assertEqual(hidden["content"], "[retracted]")
        forced = self.run_eachboard("show", sent["msg_id"], "--force", who="reviewer")["message"]
        self.assertEqual(forced["content"], "secret body")
        self.assertTrue(forced["retracted"])

    def test_retract_requires_sender_and_rejects_notifications(self):
        sent = self.send("mine")
        self.run_eachboard("retract", sent["msg_id"], who="reviewer", error="not_sender")
        event = self.retract(sent["msg_id"])
        self.run_eachboard("retract", event["msg_id"], who="writer",
                           error="retraction_not_retractable")

    def test_retract_is_idempotent(self):
        sent = self.send("one")
        first = self.retract(sent["msg_id"])
        second = self.retract(sent["msg_id"])
        self.assertEqual(first["msg_id"], second["msg_id"])
        self.assertTrue(second["already"])
        self.assertEqual(self.run_eachboard("list", who="reviewer")["count"], 2)

    def test_retract_unseen_message_clears_new_and_pending(self):
        sent = self.send("pending")
        self.assertEqual(self.run_eachboard("status", who="reviewer")["unseen"], 1)
        self.retract(sent["msg_id"])
        status = self.run_eachboard("status", who="reviewer")
        # Only the notification is new; the retracted original is neither.
        self.assertEqual((status["unseen"], status["pending"]), (1, 0))
        self.run_eachboard("send", "--content", "blocked", who="reviewer", error="unread_messages")
        self.run_eachboard("read", who="reviewer")
        self.run_eachboard("send", "--content", "allowed", who="reviewer")

    def test_retract_seen_unacked_message(self):
        sent = self.send("pending")
        self.run_eachboard("read", who="reviewer")
        self.assertEqual(self.run_eachboard("status", who="reviewer")["pending"], 1)
        self.retract(sent["msg_id"])
        status = self.run_eachboard("status", who="reviewer")
        self.assertEqual((status["unseen"], status["pending"]), (1, 0))

    def test_retract_after_ack_still_notifies(self):
        sent = self.send("done")
        self.run_eachboard("read", who="reviewer")
        self.run_eachboard("ack", sent["msg_id"], who="reviewer")
        self.assertEqual(self.run_eachboard("status", who="reviewer")["pending"], 0)
        self.retract(sent["msg_id"])
        self.assertEqual(self.run_eachboard("status", who="reviewer")["unseen"], 1)

    def test_notification_ack_is_rejected_atomically(self):
        first = self.send("normal")
        event = self.retract(first["msg_id"])
        self.run_eachboard("read", who="reviewer")
        self.run_eachboard("ack", event["msg_id"], who="reviewer", error="ack_not_required")
        second = self.send("another")
        self.run_eachboard("read", who="reviewer")
        self.run_eachboard("ack", second["msg_id"], event["msg_id"], who="reviewer",
                           error="ack_not_required")
        self.assertEqual(self.run_eachboard("status", who="reviewer")["pending"], 1)

    def test_read_ack_acknowledges_only_ordinary_messages(self):
        first = self.send("normal")
        self.retract(first["msg_id"])
        self.send("another")
        result = self.run_eachboard("read", "--ack", who="reviewer")
        self.assertEqual(result["count"], 2)
        status = self.run_eachboard("status", who="reviewer")
        self.assertEqual((status["unseen"], status["pending"]), (0, 0))

    def test_placeholder_display_does_not_mark_the_original_seen(self):
        sent = self.send("one")
        self.retract(sent["msg_id"])
        self.run_eachboard("list", who="reviewer")
        seen_file = self._conversation_path("state") / "reviewer.seen"
        seen = seen_file.read_text().splitlines() if seen_file.exists() else []
        self.assertNotIn(sent["msg_id"], seen)
        self.run_eachboard("show", sent["msg_id"], who="reviewer")
        seen = seen_file.read_text().splitlines() if seen_file.exists() else []
        self.assertNotIn(sent["msg_id"], seen)
        self.run_eachboard("show", sent["msg_id"], "--force", who="reviewer")
        self.assertIn(sent["msg_id"], seen_file.read_text().splitlines())

    def test_retraction_notification_follows_the_original_recipients(self):
        sent = self.run_eachboard("send", "--content", "for reviewer", "--to", "reviewer",
                                  who="writer")
        self.retract(sent["msg_id"])
        self.assertEqual(self.run_eachboard("status", who="observer")["unseen"], 0)
        self.assertEqual(self.run_eachboard("status", who="reviewer")["unseen"], 1)
        self.run_eachboard("retract", sent["msg_id"], who="observer", error="message_not_found")

    def test_retract_keeps_the_source_object_and_survives_index_rebuild(self):
        sent = self.send("one")
        path = self._conversation_path("messages") / sent["msg_id"][1:3] / sent["msg_id"]
        before = path.read_bytes()
        self.retract(sent["msg_id"])
        self.assertEqual(path.read_bytes(), before)
        self._conversation_path("index").unlink()
        listed = self.run_eachboard("list", who="reviewer")["messages"]
        self.assertEqual(listed[0]["content"], "[retracted]")

    def test_concurrent_retract_is_idempotent(self):
        sent = self.send("one")
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = [future.result() for future in
                       [pool.submit(self.retract, sent["msg_id"]) for _ in range(4)]]
        self.assertEqual(len({r["msg_id"] for r in results}), 1)
        self.assertEqual(self.run_eachboard("list", who="reviewer")["count"], 2)

    def test_reply_reference_survives_retraction(self):
        first = self.run_eachboard("send", "--content", "question", "--to", "reviewer",
                                   who="writer")
        self.run_eachboard("read", who="reviewer")
        self.retract(first["msg_id"])
        self.run_eachboard("read", who="reviewer")
        reply = self.run_eachboard("send", "--content", "answer", "--reply-to", first["msg_id"],
                                   who="reviewer")
        self.assertEqual(reply["reply_to"], first["msg_id"])
        self.assertEqual(self.run_eachboard("show", first["msg_id"], who="reviewer")["message"]["content"],
                         "[retracted]")

    def test_retracting_a_message_with_a_relative_path_leaves_the_shared_file_alone(self):
        report = self.workspace / 'report.txt'
        report.write_text('shared file content')
        sent = self.send('Please review ./report.txt')
        event = self.retract(sent['msg_id'])
        hidden = self.run_eachboard('show', sent['msg_id'], who='reviewer')['message']
        self.assertEqual(hidden['content'], '[retracted]')
        forced = self.run_eachboard('show', sent['msg_id'], '--force', who='reviewer')['message']
        self.assertEqual(forced['content'], 'Please review ./report.txt')
        self.assertNotIn('attachments', forced)
        notice = self.run_eachboard('show', event['msg_id'], who='reviewer')['message']
        self.assertNotIn('attachments', notice)
        self.assertEqual(report.read_text(), 'shared file content')


    def test_receipts_do_not_request_ack_for_retracted_messages_or_notifications(self):
        for state in ('unseen', 'seen', 'acked'):
            with self.subTest(state=state):
                sent = self.send('request-' + state)
                if state != 'unseen':
                    self.run_eachboard('read', who='reviewer')
                if state == 'acked':
                    self.run_eachboard('ack', sent['msg_id'], who='reviewer')
                event = self.retract(sent['msg_id'])
                original = self.run_eachboard('receipts', sent['msg_id'], who='writer')
                self.assertFalse(original['ack_required'])
                self.assertTrue(original['retracted'])
                self.assertEqual(original['pending'], [])
                self.assertEqual(original['acked_by'], ['reviewer'] if state == 'acked' else [])
                notice = self.run_eachboard('receipts', event['msg_id'], who='writer')
                self.assertFalse(notice['ack_required'])
                self.assertEqual((notice['pending'], notice['acked_by']), ([], []))
                for mid in (sent['msg_id'], event['msg_id']):
                    text = subprocess.run([sys.executable, '-B', '-m', 'eachboard',
                                           'receipts', mid], cwd=self.cwd,
                                          env=self.identity('writer'), capture_output=True,
                                          text=True, check=True).stdout
                    self.assertIn('Acknowledgement: not required', text)
                    self.assertNotIn('Pending:', text)

    def test_ack_of_retracted_original_is_unnecessary_and_batch_is_atomic(self):
        original = self.send('old')
        self.run_eachboard('read', who='reviewer')
        self.retract(original['msg_id'])
        live = self.send('live')
        self.run_eachboard('read', who='reviewer')
        self.run_eachboard('ack', live['msg_id'], original['msg_id'],
                           who='reviewer', error='ack_not_required')
        self.assertFalse(self._conversation_path('state/reviewer.read').exists())
        self.assertEqual(self.run_eachboard('status', who='reviewer')['pending'], 1)

    def test_index_rebuild_preserves_text_messages_and_retraction(self):
        sent = [self.send('message-' + str(i)) for i in range(2)]
        self.retract(sent[0]['msg_id'])
        index = self._conversation_path('index')
        index.write_bytes(b'INVALID!' + bytes(8))
        listed = self.run_eachboard('list', who='reviewer')['messages']
        self.assertTrue(index.read_bytes().startswith(b'EACHIDX3'))
        self.assertEqual([m['content'] for m in listed],
                         ['[retracted]', 'message-1', 'writer retracted message #1'])

    def test_retraction_object_survives_crash_before_index_append(self):
        sent = self.send('obsolete')
        program = '''import os
from types import SimpleNamespace
from eachboard import commands
from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
def crash(*args): os._exit(9)
c.store.append_index = crash
commands.cmd_retract(c, SimpleNamespace(msg_id=MID))
'''.replace('MID', repr(sent['msg_id']))
        crashed = subprocess.run([sys.executable, '-B', '-c', program], cwd=APP,
                                 env=self.identity('writer'), capture_output=True)
        self.assertEqual(crashed.returncode, 9)
        self.assertTrue(self._conversation_path('.index-dirty').exists())
        messages = self.run_eachboard('read', who='reviewer')['messages']
        self.assertEqual([m['content'] for m in messages], ['writer retracted message #1'])
        self.assertFalse(self._conversation_path('.index-dirty').exists())
        retried = self.retract(sent['msg_id'])
        self.assertTrue(retried['already'])
        self.assertEqual(retried['msg_id'], messages[0]['msg_id'])
        self.assertEqual(self.send('next')['seq'], 3)

    def test_force_never_bypasses_directed_visibility(self):
        sent = self.run_eachboard('send', 'private original', '--to', 'reviewer', who='writer')
        event = self.retract(sent['msg_id'])
        for mid in (sent['msg_id'], event['msg_id']):
            self.run_eachboard('show', mid, '--force', who='observer', error='message_not_found')
            output = subprocess.run([sys.executable, '-B', '-m', 'eachboard',
                                     'show', mid, '--force'], cwd=self.cwd,
                                    env=self.identity('observer'), capture_output=True, text=True)
            self.assertEqual(output.returncode, 1)
            self.assertNotIn('private original', output.stdout)
        self.assertFalse(self._conversation_path('state/observer.seen').exists())

    def test_wait_ack_skips_notifications_and_retracted_originals(self):
        original = self.send('obsolete')
        notice = self.retract(original['msg_id'])
        live = self.send('active')
        result = self.run_eachboard('wait', '--timeout', '0', '--ack', who='reviewer')
        self.assertEqual([m['msg_id'] for m in result['messages']], [notice['msg_id'], live['msg_id']])
        self.assertEqual(self._conversation_path('state/reviewer.read').read_text().splitlines(),
                         [live['msg_id']])
        self.assertEqual(set(self._conversation_path('state/reviewer.seen').read_text().splitlines()),
                         {notice['msg_id'], live['msg_id']})
        status = self.run_eachboard('status', who='reviewer')
        self.assertEqual((status['unseen'], status['pending']), (0, 0))

    def test_retraction_output_failure_does_not_advance_seen_or_ack(self):
        original = self.send('obsolete')
        self.retract(original['msg_id'])
        calls = [
            'commands.cmd_read(c, SimpleNamespace(after_seq=0, limit=100, pending=False, ack=True))',
            'commands.cmd_list(c, SimpleNamespace(after_seq=None, limit=100))',
            'commands.cmd_wait(c, SimpleNamespace(after_seq=0, limit=100, timeout=0, ack=True))',
            'commands.cmd_show(c, SimpleNamespace(msg_id=' + repr(original['msg_id']) + ', force=True))',
        ]
        for call in calls:
            with self.subTest(command=call):
                program = '''from eachboard import commands
from eachboard.context import BoardContext
from eachboard.errors import EachboardError
c = BoardContext()
from types import SimpleNamespace
def broken(obj): raise BrokenPipeError()
c.out = broken
try: CALL
except BrokenPipeError: pass
else: raise AssertionError('expected failed output')
'''.replace('CALL', call)
                subprocess.run([sys.executable, '-B', '-c', program], cwd=APP,
                               env=self.identity('reviewer'), check=True)
                self.assertFalse(self._conversation_path('state/reviewer.seen').exists())
                self.assertFalse(self._conversation_path('state/reviewer.read').exists())

    def test_broadcast_notification_is_seen_independently_by_each_recipient(self):
        original = self.send('broadcast')
        self.run_eachboard('read', who='reviewer')
        self.retract(original['msg_id'])
        self.run_eachboard('read', who='reviewer')
        self.assertEqual(self.run_eachboard('status', who='reviewer')['unseen'], 0)
        self.assertEqual(self.run_eachboard('status', who='observer')['unseen'], 1)
        self.run_eachboard('send', 'blocked', who='observer', error='unread_messages')
        self.run_eachboard('read', who='observer')
        self.assertEqual(self.run_eachboard('status', who='observer')['unseen'], 0)

    def test_retract_tenth_message_after_fourteenth_appends_fifteenth_event(self):
        sent = [self.send('message-' + str(i)) for i in range(1, 15)]
        event = self.retract(sent[9]['msg_id'])
        listed = self.run_eachboard('list', who='reviewer')['messages']
        self.assertEqual([m['seq'] for m in listed], list(range(1, 16)))
        self.assertEqual(listed[9]['content'], '[retracted]')
        self.assertEqual(listed[14]['msg_id'], event['msg_id'])
        self.assertEqual(listed[14]['content'], 'writer retracted message #10')

    def test_receipts_keep_display_history_after_retraction(self):
        sent = self.send('to be retracted')
        self.run_eachboard('read', who='reviewer')
        self.run_eachboard('ack', sent['msg_id'], who='reviewer')
        self.run_eachboard('show', sent['msg_id'], who='observer')
        event = self.retract(sent['msg_id'])
        original = self.run_eachboard('receipts', sent['msg_id'], who='writer')
        self.assertEqual((original['not_displayed'], original['displayed'], original['acked_by']),
                         ([], ['observer'], ['reviewer']))
        self.run_eachboard('read', who='reviewer')
        notice = self.run_eachboard('receipts', event['msg_id'], who='writer')
        self.assertEqual((notice['not_displayed'], notice['displayed'], notice['acked_by']),
                         (['observer'], ['reviewer'], []))
        text = subprocess.run([sys.executable, '-B', '-m', 'eachboard', 'receipts', event['msg_id']],
                              cwd=self.cwd, env=self.identity('writer'), capture_output=True,
                              text=True, check=True).stdout
        self.assertIn('Displayed: reviewer', text)
        self.assertNotIn('Acknowledged:', text)
        self.assertIn('Acknowledgement: not required (read-only notification)', text)

    def test_task_filter_includes_notifications_for_task_messages(self):
        tagged = self.run_eachboard('send', 'task work', '--task-id', 'task-a', who='writer')
        other = self.run_eachboard('send', 'other work', '--task-id', 'task-b', who='writer')
        self.run_eachboard('read', who='reviewer')
        event = self.retract(tagged['msg_id'])
        self.retract(other['msg_id'])
        page = self.run_eachboard('read', '--task-id', 'task-a', who='reviewer')
        self.assertEqual([m['msg_id'] for m in page['messages']], [event['msg_id']])
        self.assertEqual(self.run_eachboard('status', who='reviewer')['unseen'], 1)
        waited = self.run_eachboard('wait', '--timeout', '0', '--task-id', 'task-b', who='reviewer')
        self.assertEqual(waited['messages'][0]['retracts'], other['msg_id'])
        history = self.run_eachboard('list', '--task-id', 'task-a', who='observer')['messages']
        self.assertEqual([m['msg_id'] for m in history], [tagged['msg_id'], event['msg_id']])
        self.assertEqual(history[0]['content'], '[retracted]')
