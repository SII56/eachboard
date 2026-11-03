"""Concise text views for humans and interactive agents; JSON stays opt-in."""
import time


LABELS = {
    'admin_key': 'Admin key (save now)', 'note': 'Note',
    'conv_id': 'Conversation', 'who': 'Identity', 'root': 'Data directory', 'workspace': 'Workspace',
    'participants': 'Participants', 'msg_id': 'Message ID',
    'seq': 'Sequence', 'sender': 'Sender', 'created': 'Time', 'to': 'To',
    'reply_to': 'Reply to', 'task_id': 'Task',
    'root_source': 'Root source', 'project': 'Project',
    'environment': 'Environment', 'conversation': 'Conversation', 'identity': 'Identity',
    'authorized': 'Authorized', 'key_present': 'Key present', 'exists': 'Exists',
    'source': 'Source', 'head': 'HEAD', 'conversation_source': 'Conversation source', 'id': 'ID', 'warnings': 'Warnings',
    'unseen': 'Unseen', 'pending': 'Pending',
    'retracts': 'Retracts', 'retracted': 'Retracted',
    'retraction_id': 'Retraction', 'retracted_by': 'Retracted by',
    'retracted_at': 'Retracted at',
}


STYLES = {
    'label': '1',
    'heading': '1;36',
    'identity': '36',
    'hash': '33',
    'task': '35',
    'muted': '2',
    'notice': '1;33',
    'pending': '36',
    'success': '32',
    'error': '1;31',
}


def use_color(stream, environ):
    """Only decorate interactive terminals; explicit opt-outs always win."""
    return ('NO_COLOR' not in environ and environ.get('TERM') != 'dumb'
            and getattr(stream, 'isatty', lambda: False)())


def highlight(text, style, color):
    return '\x1b[' + STYLES[style] + 'm' + text + '\x1b[0m' if color else text


def timestamp(value):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(value))


def message_text(message, color=False):
    m = message
    lines = ['{} {}:  [#{} · {}]'.format(
        timestamp(m['created']),
        highlight(m['sender'], 'identity', color),
        str(m['seq']),
        highlight(m['msg_id'], 'hash', color))]
    context = []
    for key in ('to', 'reply_to', 'task_id'):
        if m.get(key):
            context.append(LABELS[key] + ': ' + value_text(key, m[key], color))
    if m.get('retracts'):
        context.append(LABELS['retracts'] + ': ' + value_text('retracts', m['retracts'], color))
    if context:
        lines.append(' | '.join(context))
    if m.get('retracted'):
        lines.append(highlight('Status: retracted', 'notice', color) + ' by '
                     + value_text('sender', m.get('retracted_by'), color) + ' (event '
                     + value_text('retraction_id', m.get('retraction_id'), color) + ', '
                     + timestamp(m.get('retracted_at')) + ')')
    # User text is never decorated or rewritten, including forced originals.
    lines.append(m['content'])
    return '\n'.join(lines)


def value_text(key, value, color=False):
    if key == 'created':
        return highlight(timestamp(value), 'muted', color)
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, dict):
        return fields_text(value, color)
    if isinstance(value, list):
        return ', '.join(value_text(key, item, color) for item in value)
    if key in ('unseen', 'pending') and isinstance(value, int):
        style = ('notice' if key == 'unseen' else 'pending') if value else 'muted'
        return highlight(str(value), style, color)
    if key in ('who', 'sender', 'to', 'participants'):
        return highlight(str(value), 'identity', color)
    if key in ('msg_id', 'reply_to', 'retracts', 'retraction_id'):
        return highlight(str(value), 'hash', color)
    if key == 'task_id':
        return highlight(str(value), 'task', color)
    if key == 'conv_id':
        return highlight(str(value), 'heading', color)
    return str(value)


def field_text(key, value, color=False):
    return highlight(LABELS.get(key, key), 'label', color) + ': ' + value_text(key, value, color)


def fields_text(data, color=False):
    return ' | '.join(field_text(k, v, color)
                      for k, v in data.items() if v is not None and v != [] and v != '')


def render(data, color=False):
    if not data.get('ok', True):
        code = '[' + data['code'] + '] ' if data.get('code') else ''
        return highlight('Error: ' + code + data.get('error', 'Unknown error'), 'error', color)
    if 'messages' in data:
        body = '\n\n'.join(message_text(m, color) for m in data['messages']) or highlight('No messages.', 'muted', color)
        cursor = data.get('next_after_seq', 0)
        return body + ('\n\n' + highlight('Cursor: ' + str(cursor), 'muted', color) if cursor else '')
    if 'message' in data:
        return message_text(data['message'], color)
    if 'acked' in data:
        return highlight('Acknowledged:', 'success', color) + ' ' + ', '.join(data['acked'])
    if 'acked_by' in data:
        def group(label, names):
            return (highlight(label, 'label', color) + ': '
                    + (', '.join(highlight(n, 'identity', color) for n in names) or 'none'))
        notification = bool(data.get('retracts'))
        lines = [field_text('msg_id', data['msg_id'], color),
                 group('Not displayed', data.get('not_displayed', [])),
                 # Display is not processing; only acknowledgement means processed.
                 group('Displayed' if notification else 'Displayed, not acknowledged',
                       data.get('displayed', []))]
        if not notification:
            # Retracted originals keep their acknowledgement history.
            lines.append(group('Acknowledged', data['acked_by']))
        if data.get('ack_required') is False:
            lines.append(highlight('Acknowledgement: not required' + (
                ' (message retracted)' if data.get('retracted') else ' (read-only notification)'), 'muted', color))
        return '\n'.join(lines)
    if 'conversations' in data:
        return '\n'.join(field_text('conv_id', item['conv_id'], color) for item in data['conversations']) or highlight('No conversations.', 'muted', color)
    lines = []
    if 'seq' in data and 'msg_id' in data:
        lines.append(highlight('Message sent.', 'success', color))
    for key, value in data.items():
        if key in ('ok', 'warnings') or value is None or value == [] or value == '':
            continue
        if key == 'keys':
            lines.append(highlight('Participant keys (keep private): ', 'notice', color))
            lines.extend('  ' + who + ': ' + secret for who, secret in value.items())
        elif key == 'ack_required':
            lines.append(highlight('Acknowledgement: explicit ack after processing' if value else 'Acknowledgement: not required', 'muted', color))
        else:
            lines.append(field_text(key, value, color))
    for warning in data.get('warnings') or []:
        lines.append(highlight('Warning: ' + warning, 'notice', color))
    return '\n'.join(lines) or 'Done.'
