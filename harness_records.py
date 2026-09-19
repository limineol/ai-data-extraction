"""Lossless records plus normalized conversation messages for known stores."""
import base64
from collections import defaultdict
import json
import sqlite3
from contextlib import contextmanager


@contextmanager
def read_database(path):
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        yield connection
    finally:
        connection.close()


def binary_json(value):
    if isinstance(value, bytes):
        return {'encoding': 'base64', 'data': base64.b64encode(value).decode('ascii')}
    raise TypeError(type(value).__name__)


def normalize_message(message, event=None):
    result = dict(message)
    role = result.get('role', 'assistant')
    if not isinstance(role, str):
        raise ValueError('Message role must be a string')
    result['role'] = {'model': 'assistant', 'gemini': 'assistant', 'toolResult': 'tool',
                      'tool_result': 'tool'}.get(role, role.lower())
    if 'content' not in result:
        result['content'] = result.get('text', '')
    if event:
        for key in ['timestamp', 'id', 'parentId']:
            if key in event:
                result.setdefault(key, event[key])
    return result


def jsonl_conversation(records, source):
    messages, fallback, metadata = [], [], {}
    for event_index, event in enumerate(records):
        kind = event.get('type')
        if kind in ['session_meta', 'session', 'session_start']:
            metadata.update(event.get('payload', event))
        if source.harness == 'codex':
            payload = event.get('payload', {})
            if kind == 'response_item':
                if payload.get('type') == 'message':
                    messages.append((event_index, normalize_message(payload, event)))
                elif payload.get('type') in ['function_call', 'custom_tool_call', 'function_call_output',
                                              'custom_tool_call_output', 'reasoning', 'web_search_call']:
                    messages.append((event_index, dict(payload, role='tool' if payload['type'].endswith('_output') else 'assistant',
                                         content=payload.get('content', payload.get('output', '')), timestamp=event.get('timestamp'))))
            elif kind == 'event_msg' and payload.get('type') in ['user_message', 'agent_message']:
                fallback.append((event_index, {'role': 'user' if payload['type'] == 'user_message' else 'assistant',
                                 'content': payload.get('message', ''), 'timestamp': event.get('timestamp')}))
        elif isinstance(event.get('message'), dict) and 'role' in event['message']:
            messages.append(normalize_message(event['message'], event))
        elif source.harness == 'grok' and kind in ['system', 'user', 'assistant', 'tool_result', 'reasoning']:
            messages.append(normalize_message(dict(event, role='assistant' if kind == 'reasoning' else kind)))
        elif source.harness == 'copilot' and kind in ['user.message', 'assistant.message', 'tool.execution_complete']:
            payload = event.get('data', {})
            messages.append(normalize_message(dict(payload, role=kind.split('.')[0]), event))
    if source.harness == 'codex':
        # Match only actual duplicate text events. Keep unmatched legacy turns
        # in place when a rollout spans a storage-format transition.
        candidates = defaultdict(list)
        for index, message in fallback:
            candidates[(message['role'], message['content'])].append(index)
        matched = set()
        for index, message in messages:
            if message.get('type') != 'message':
                continue
            content = message.get('content', '')
            text = content if isinstance(content, str) else ''.join(
                part.get('text', '') for part in content if isinstance(part, dict))
            matches = candidates[(message['role'], text)]
            if matches:
                duplicate = min(matches, key=lambda position: abs(position - index))
                matches.remove(duplicate)
                matched.add(duplicate)
        messages.extend((index, message) for index, message in fallback if index not in matched)
        messages = [message for _, message in sorted(messages, key=lambda pair: pair[0])]
    return {'source': source.harness, 'session_id': metadata.get('id', source.path.stem),
            'metadata': metadata, 'messages': messages}


def rows(connection, table, where='', parameters=()):
    return [dict(row) for row in connection.execute('SELECT * FROM "' + table + '" ' + where, parameters)]


def database_conversations(connection, source, raw_write):
    tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    name = source.harness
    if name == 'cursor-cli':
        from extract_cursor_cli import load_meta, resolve_messages
        for table in ['meta', 'blobs']:
            for row in connection.execute('SELECT * FROM "' + table + '"'):
                raw_write({'table': table, 'row': dict(row)})
        meta = load_meta(connection)
        if not meta.get('latestRootBlobId'):
            raise ValueError('Cursor store has no transcript root')
        yield {'source': name, 'session_id': source.path.parent.name, 'metadata': meta,
               'messages': resolve_messages(connection, meta['latestRootBlobId'])}
    elif name == 'opencode':
        supported = False
        for session_table, message_table in [('session', 'message'), ('session_v2', 'session_message')]:
            if session_table not in tables or message_table not in tables:
                continue
            supported = True
            for session in rows(connection, session_table):
                raw_write({'table': session_table, 'row': session})
                messages = []
                order = 'seq' if message_table == 'session_message' else 'time_created, id'
                for row in rows(connection, message_table, 'WHERE session_id=? ORDER BY ' + order, (session['id'],)):
                    raw_write({'table': message_table, 'row': row})
                    data = json.loads(row['data'])
                    if message_table == 'message':
                        parts = rows(connection, 'part', 'WHERE message_id=? ORDER BY time_created, id', (row['id'],))
                        for part in parts:
                            raw_write({'table': 'part', 'row': part})
                        data['content'] = [json.loads(part['data']) for part in parts]
                    else:
                        data['role'] = row['type']
                    messages.append(normalize_message(dict(data, id=row['id'])))
                yield {'source': name, 'schema': session_table, 'session_id': session['id'],
                       'metadata': session, 'messages': messages}
        if not supported:
            raise ValueError('Unrecognized OpenCode schema')
    elif name in ['hermes', 'devin']:
        message_table = 'messages' if name == 'hermes' else 'message_nodes'
        for session in rows(connection, 'sessions'):
            raw_write({'table': 'sessions', 'row': session})
            if name == 'hermes' and 'system_prompts' in tables and session.get('system_prompt_hash'):
                for prompt in rows(connection, 'system_prompts', 'WHERE hash=?', (session['system_prompt_hash'],)):
                    raw_write({'table': 'system_prompts', 'row': prompt})
            messages = []
            order = 'timestamp, id' if name == 'hermes' else 'node_id'
            for row in rows(connection, message_table, 'WHERE session_id=? ORDER BY ' + order, (session['id'],)):
                raw_write({'table': message_table, 'row': row})
                message = row if name == 'hermes' else dict(json.loads(row['chat_message']),
                                                          node_id=row['node_id'], parent_node_id=row['parent_node_id'])
                messages.append(normalize_message(message))
            if name == 'devin' and 'tool_call_state' in tables:
                for row in rows(connection, 'tool_call_state', 'WHERE session_id=?', (session['id'],)):
                    raw_write({'table': 'tool_call_state', 'row': row})
            yield {'source': name, 'session_id': session['id'], 'metadata': session, 'messages': messages}
    elif name == 'forgecode':
        for row in rows(connection, 'conversations'):
            raw_write({'table': 'conversations', 'row': row})
            context = json.loads(row['context'] or '{}')
            messages = []
            for entry in context.get('messages', []):
                message = entry['message']
                if 'text' in message:
                    messages.append(normalize_message(message['text']))
                elif 'tool' in message:
                    messages.append(dict(message['tool'], role='tool', content=message['tool'].get('output')))
                else:
                    raise ValueError('Unrecognized ForgeCode message')
            yield {'source': name, 'session_id': row['conversation_id'],
                   'metadata': {k: v for k, v in row.items() if k != 'context'}, 'messages': messages}
    elif name == 'antigravity':
        # Payloads are versioned protobufs, not text. Preserve them byte-for-byte
        # without claiming that an untyped string scrape is a decoded transcript.
        allowed = ['trajectory_meta', 'steps', 'gen_metadata', 'executor_metadata',
                   'parent_references', 'trajectory_metadata_blob', 'battle_mode_infos']
        if not {'trajectory_meta', 'steps'} <= tables:
            raise ValueError('Unrecognized Antigravity schema')
        for table in allowed:
            if table in tables:
                for row in connection.execute('SELECT * FROM "' + table + '"'):
                    raw_write({'table': table, 'row': dict(row)})
        yield {'source': name, 'session_id': source.path.stem, 'messages': [], 'archive_only': True}
    else:
        raise ValueError('Unsupported database harness: ' + name)


def legacy_json_conversation(source, raw_write):
    path = source.path
    session = json.loads(path.read_text(encoding='utf-8'))
    raw_write({'path': str(path), 'record': session})
    if source.format == 'json':
        return {'source': source.harness, 'session_id': session.get('sessionId', path.stem),
                'metadata': {k: v for k, v in session.items() if k != 'messages'},
                'messages': [normalize_message(dict(m, role=m.get('type', m.get('role', 'assistant'))))
                             for m in session.get('messages', [])]}
    storage = next(p for p in path.parents if p.name == 'storage')
    session_id = session['id']
    messages = []
    for message_path in sorted((storage / 'message' / session_id).glob('*.json')):
        message = json.loads(message_path.read_text(encoding='utf-8'))
        raw_write({'path': str(message_path), 'record': message})
        parts = []
        for part_path in sorted((storage / 'part' / message['id']).glob('*.json')):
            part = json.loads(part_path.read_text(encoding='utf-8'))
            parts.append(part)
            raw_write({'path': str(part_path), 'record': part})
        messages.append(normalize_message(dict(message, content=parts)))
    messages.sort(key=lambda m: m.get('time', {}).get('created', 0))
    return {'source': source.harness, 'session_id': session_id, 'metadata': session, 'messages': messages}
