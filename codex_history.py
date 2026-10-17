"""Read Codex's persisted history without resuming or modifying a thread."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


HISTORY_TABLES = {
    'thread_turns': ('turn_id', 'rollout_ordinal', 'status'),
    'thread_items': ('turn_id', 'item_id', 'rollout_ordinal', 'created_at_ms', 'item_json'),
    'thread_history_projection_state': ('next_rollout_byte_offset', 'next_rollout_ordinal'),
    'thread_realtime_items': ('item_id', 'rollout_ordinal', 'created_at_ms', 'item_json'),
}
TERMINAL_TURNS = {'completed', 'failed', 'interrupted'}


def columns(connection, database, table):
    return {row['name'] for row in connection.execute(f'PRAGMA {database}.table_info("{table}")')}


@contextmanager
def open_history(home):
    home = Path(home).resolve()
    if any(int(p.stem.split('_')[-1]) > 5 for p in home.glob('state_[0-9]*.sqlite')):
        raise ValueError('Unsupported or ambiguous Codex state database version')
    if any(p.name != 'thread_history_1.sqlite' for p in home.glob('thread_history_[0-9]*.sqlite')):
        raise ValueError('Unsupported or ambiguous Codex history database version')
    connection = sqlite3.connect((home / 'state_5.sqlite').as_uri() + '?mode=ro', uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        if not {'id', 'rollout_path'} <= columns(connection, 'main', 'threads'):
            raise ValueError('Unrecognized Codex thread metadata schema')
        history = home / 'thread_history_1.sqlite'
        if history.exists():
            connection.execute('ATTACH DATABASE ? AS history', (history.as_uri() + '?mode=ro',))
            tables = {row['name'] for row in connection.execute("SELECT name FROM history.sqlite_master WHERE type='table'")}
            if not {'thread_turns', 'thread_items'} <= tables:
                raise ValueError('Unrecognized Codex paginated history schema')
            unexpected = tables - set(HISTORY_TABLES) - {'_sqlx_migrations', 'sqlite_sequence', 'sqlite_stat1', 'sqlite_stat4'}
            if unexpected:
                raise ValueError('Unrecognized Codex history tables')
            for table, required in HISTORY_TABLES.items():
                if table in tables and not {'thread_id', *required} <= columns(connection, 'history', table):
                    raise ValueError('Unrecognized Codex history columns: ' + table)
            # Establish both read snapshots before reading any conversation. They
            # are separate WAL databases; cleanup always rechecks each thread later.
            connection.execute('SELECT (SELECT COUNT(*) FROM main.threads), (SELECT COUNT(*) FROM history.thread_items)').fetchone()
        else:
            tables = set()
            if 'history_mode' in columns(connection, 'main', 'threads') and connection.execute("SELECT 1 FROM threads WHERE history_mode='paginated' LIMIT 1").fetchone():
                raise ValueError('Paginated threads exist but their history database is missing')
        yield connection, tables
    finally:
        connection.close()


def thread_records(connection, tables, thread_id):
    for table in HISTORY_TABLES:
        if table not in tables:
            continue
        order = 'rollout_ordinal' if table != 'thread_history_projection_state' else 'thread_id'
        for row in connection.execute(f'SELECT * FROM history."{table}" WHERE thread_id=? ORDER BY {order}', (thread_id,)):
            yield {'table': table, 'row': dict(row)}


def summarize_history(records):
    digest = hashlib.sha256()
    counts = {}
    turns, item_turns, realtime_sessions = set(), set(), set()
    timestamps = []
    issues = set()
    for record in records:
        digest.update((json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode())
        table, row = record['table'], record['row']
        counts[table] = counts.get(table, 0) + 1
        if table == 'thread_turns':
            turns.add(row['turn_id'])
            if row['status'] not in TERMINAL_TURNS:
                issues.add('unfinished_or_unknown_turn')
            timestamps.extend(row.get(key) for key in ['started_at', 'completed_at'])
        elif table in ['thread_items', 'thread_realtime_items']:
            payload = json.loads(row['item_json'])
            if not isinstance(payload, dict) or not isinstance(payload.get('type'), str):
                raise ValueError('Invalid Codex item payload')
            if payload.get('id') != row['item_id']:
                raise ValueError('Codex item identity mismatch')
            if row.get('item_type') and row['item_type'] != payload['type']:
                raise ValueError('Codex item type mismatch')
            timestamps.extend(row.get(key) / 1000 for key in ['created_at_ms', 'started_at_ms', 'completed_at_ms'] if row.get(key) is not None)
            if table == 'thread_items':
                item_turns.add(row['turn_id'])
                if 'status' in payload and payload['status'] not in ['completed', 'failed', 'interrupted', 'declined']:
                    issues.add('unfinished_item')
            else:
                session = payload.get('realtime_session_id')
                if payload['type'] == 'realtime_session_started':
                    realtime_sessions.add(session)
                elif payload['type'] == 'realtime_session_closed':
                    realtime_sessions.discard(session)
    if item_turns - turns:
        issues.add('items_without_turn_metadata')
    if realtime_sessions:
        issues.add('open_realtime_session')
    if not counts.get('thread_items') and not counts.get('thread_realtime_items'):
        issues.add('no_history_items')
    values = [value for value in timestamps if isinstance(value, (int, float)) and value > 0]
    return {'sha256': digest.hexdigest(), 'counts': counts, 'last_history_activity': max(values, default=0),
            'issues': sorted(issues)}


def normalized_item(record):
    row = record['row']
    payload = json.loads(row['item_json'])
    kind = payload['type']
    role = {'userMessage': 'user', 'agentMessage': 'assistant', 'functionCallOutput': 'tool'}.get(kind, 'assistant')
    if record['table'] == 'thread_realtime_items':
        role = payload.get('role', 'event') if kind == 'transcript_segment' else 'event'
    return dict(payload, role=role, content=payload.get('content', payload.get('text', payload.get('output', ''))),
                turn_id=row.get('turn_id'), rollout_ordinal=row['rollout_ordinal'],
                timestamp_ms=row['created_at_ms'], history_table=record['table'])


def export_history(home, raw_write):
    with open_history(home) as (connection, tables):
        metadata = {row['id']: dict(row) for row in connection.execute('SELECT * FROM threads ORDER BY id')}
        for row in metadata.values():
            raw_write({'database': 'state', 'table': 'threads', 'row': row})
        state_tables = {row['name'] for row in connection.execute("SELECT name FROM main.sqlite_master WHERE type='table'")}
        for table in ['thread_spawn_edges', 'thread_dynamic_tools']:
            if table in state_tables:
                for row in connection.execute(f'SELECT * FROM "{table}"'):
                    raw_write({'database': 'state', 'table': table, 'row': dict(row)})
        ids = set(metadata)
        for table in tables & set(HISTORY_TABLES):
            ids.update(row[0] for row in connection.execute(f'SELECT DISTINCT thread_id FROM history."{table}"'))
        for thread_id in sorted(ids):
            records = list(thread_records(connection, tables, thread_id))
            for record in records:
                raw_write(dict(record, database='history'))
            thread = metadata.get(thread_id)
            if thread and thread.get('history_mode', 'legacy') == 'legacy':
                if thread.get('has_user_event') and not Path(thread['rollout_path']).is_file():
                    raise ValueError('Legacy Codex transcript is missing')
                continue
            if thread and thread['history_mode'] != 'paginated':
                raise ValueError('Unsupported Codex history mode')
            proof = summarize_history(records)
            if thread is None:
                proof['issues'].append('missing_thread_metadata')
            messages = [normalized_item(record) for record in records if record['table'] in ['thread_items', 'thread_realtime_items']]
            messages.sort(key=lambda m: (m['rollout_ordinal'], m['history_table']))
            yield {'source': 'codex', 'storage': 'paginated', 'session_id': thread_id, 'metadata': thread,
                   'messages': messages, 'history_proof': proof}


def paginated_rollout_paths(home):
    with open_history(home) as (connection, _):
        if 'history_mode' not in columns(connection, 'main', 'threads'):
            return set()
        return {str(Path(row[0]).resolve()) for row in connection.execute("SELECT rollout_path FROM threads WHERE history_mode='paginated' AND rollout_path IS NOT NULL")}
