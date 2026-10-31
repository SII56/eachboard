"""Concise text views for humans and interactive agents; JSON stays opt-in."""
import time


LABELS = {
    'conv_id': 'Conversation', 'who': 'Identity', 'root': 'Data directory', 'workspace': 'Workspace',
    'participants': 'Participants', 'msg_id': 'Message ID',
    'seq': 'Sequence', 'sender': 'Sender', 'created': 'Time', 'to': 'To',
    'reply_to': 'Reply to', 'task_id': 'Task', 'attachment_id': 'Attachment ID',
    'name': 'Filename', 'path': 'Path', 'kind': 'Type', 'size': 'Bytes',
    'sha256': 'SHA256', 'attachments': 'Attachment',
    'root_source': 'Root source', 'project': 'Project',
    'environment': 'Environment', 'conversation': 'Conversation', 'identity': 'Identity',
    'authorized': 'Authorized', 'key_present': 'Key present', 'exists': 'Exists',
    'source': 'Source', 'config': 'config.json', 'id': 'ID', 'warnings': 'Warnings',
    'unseen': 'Unseen', 'pending': 'Pending',
    'retracts': 'Retracts', 'retracted': 'Retracted',
    'retraction_id': 'Retraction', 'retracted_by': 'Retracted by',
    'retracted_at': 'Retracted at',
}


def timestamp(value):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value))


def message_text(message):
    m = message
    lines = ['{} {}:  [#{} · {}]'.format(timestamp(m['created']), m['sender'], m['seq'], m['msg_id'])]
    context = []
    for key in ('to', 'reply_to', 'task_id'):
        if m.get(key):
            value = ', '.join(m[key]) if isinstance(m[key], list) else m[key]
            context.append(LABELS[key] + ': ' + str(value))
    if m.get('retracts'):
        context.append(LABELS['retracts'] + ': ' + str(m['retracts']))
    if context:
        lines.append(' | '.join(context))
    if m.get('retracted'):
        lines.append('Status: retracted by ' + str(m.get('retracted_by')) + ' (event '
                     + str(m.get('retraction_id')) + ', '
                     + timestamp(m.get('retracted_at')) + ')')
    lines.append(m['content'])
    for attachment in m.get('attachments', []):
        lines.append('Attachment: ' + fields_text(attachment))
    return '\n'.join(lines)


def value_text(key, value):
    if key == 'created':
        return timestamp(value)
    if key == 'kind':
        return {'reference': 'File reference', 'snapshot': 'File snapshot'}.get(value, str(value))
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, dict):
        return fields_text(value)
    if isinstance(value, list):
        return ', '.join(value_text('', item) for item in value)
    return str(value)


def fields_text(data):
    return ' | '.join(LABELS.get(k, k) + ': ' + value_text(k, v)
                      for k, v in data.items() if v is not None and v != [] and v != '')


def render(data):
    if not data.get('ok', True):
        code = '[' + data['code'] + '] ' if data.get('code') else ''
        return 'Error: ' + code + data.get('error', 'Unknown error')
    if 'messages' in data:
        body = '\n\n'.join(message_text(m) for m in data['messages']) or 'No messages.'
        cursor = data.get('next_after_seq', 0)
        return body + ('\n\nCursor: ' + str(cursor) if cursor else '')
    if 'message' in data:
        return message_text(data['message'])
    if 'acked' in data:
        return 'Acknowledged: ' + ', '.join(data['acked'])
    if 'acked_by' in data:
        if data.get('ack_required') is False:
            return 'Message ID: {}\nAcknowledged: {}\nAcknowledgement: not required{}'.format(
                data['msg_id'], ', '.join(data['acked_by']) or 'none',
                ' (message retracted)' if data.get('retracted') else ' (read-only notification)')
        return 'Message ID: {}\nAcknowledged: {}\nPending: {}'.format(
            data['msg_id'], ', '.join(data['acked_by']) or 'none', ', '.join(data['pending']) or 'none')
    if 'conversations' in data:
        return '\n'.join('Conversation: ' + item['conv_id'] for item in data['conversations']) or 'No conversations.'
    lines = []
    if 'seq' in data and 'msg_id' in data:
        lines.append('Message sent.')
    for key, value in data.items():
        if key in ('ok', 'warnings') or value is None or value == [] or value == '':
            continue
        if key == 'keys':
            lines.append('Participant keys (keep private): ')
            lines.extend('  ' + who + ': ' + secret for who, secret in value.items())
        elif key == 'ack_required':
            lines.append('Acknowledgement: explicit ack after processing' if value else 'Acknowledgement: not required')
        elif key == 'attachments':
            lines.extend('Attachment: ' + fields_text(a) for a in value)
        else:
            lines.append(LABELS.get(key, key) + ': ' + value_text(key, value))
    for warning in data.get('warnings') or []:
        lines.append('Warning: ' + warning)
    return '\n'.join(lines) or 'Done.'
