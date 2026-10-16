import gzip
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codex_cleanup import backup_proofs, plan
from codex_history import export_history
from extract_portable import extract_source
from harness_sources import Source, discover


class CodexHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / '.codex'
        self.home.mkdir()
        self.output = self.root / 'backup'
        self.output.mkdir()
        self.state = sqlite3.connect(self.home / 'state_5.sqlite')
        self.history = sqlite3.connect(self.home / 'thread_history_1.sqlite')
        self.addCleanup(self.state.close)
        self.addCleanup(self.history.close)
        self.state.executescript('''
            CREATE TABLE threads(id TEXT PRIMARY KEY, history_mode TEXT, rollout_path TEXT,
                archived INTEGER DEFAULT 0, is_pinned INTEGER DEFAULT 0,
                created_at INTEGER DEFAULT 1, updated_at INTEGER DEFAULT 1, recency_at INTEGER DEFAULT 1);
            CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT);
        ''')
        self.history.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE thread_turns(thread_id TEXT, turn_id TEXT, rollout_ordinal INTEGER,
                status TEXT, started_at INTEGER, completed_at INTEGER);
            CREATE TABLE thread_items(thread_id TEXT, turn_id TEXT, item_id TEXT,
                rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT);
            CREATE TABLE thread_history_projection_state(thread_id TEXT, next_rollout_byte_offset INTEGER,
                next_rollout_ordinal INTEGER);
            CREATE TABLE thread_realtime_items(thread_id TEXT, item_id TEXT, rollout_ordinal INTEGER,
                created_at_ms INTEGER, item_json TEXT);
        ''')
        self.add_thread('parent')

    def add_thread(self, name, status='completed'):
        self.state.execute('INSERT INTO threads(id,history_mode,rollout_path) VALUES(?,?,?)',
                           (name, 'paginated', str(self.home / (name + '.jsonl'))))
        self.state.commit()
        self.history.execute('INSERT INTO thread_turns VALUES(?,?,0,?,1,2)', (name, 'turn', status))
        self.history.execute('INSERT INTO thread_items VALUES(?,?,?,1,2000,?)',
                             (name, 'turn', 'user', json.dumps({'id': 'user', 'type': 'userMessage',
                                                              'content': [{'text': 'hello'}]})))
        self.history.commit()

    def backup(self):
        result = extract_source(Source('codex', self.home / 'state_5.sqlite', 'codex-sqlite'), self.output)
        (self.output / 'manifest.json').write_text(json.dumps({'sources': [result]}))
        return backup_proofs(self.output, self.home)

    def get_plan(self, proofs=None, **kwargs):
        return plan(self.home, proofs if proofs is not None else self.backup(), now=100 * 86400, **kwargs)

    def test_wal_and_nonmessage_payloads_preserved(self):
        payload = {'id': 'cmd', 'type': 'commandExecution', 'status': 'completed',
                   'aggregatedOutput': 'output', 'extraFutureField': {'value': 1}}
        self.history.execute('INSERT INTO thread_items VALUES(?,?,?,2,3000,?)',
                             ('parent', 'turn', 'cmd', json.dumps(payload)))
        self.history.commit()
        raw = []
        conversations = list(export_history(self.home, raw.append))
        self.assertEqual(conversations[0]['messages'][1]['extraFutureField'], {'value': 1})
        self.assertEqual(conversations[0]['messages'][1]['aggregatedOutput'], 'output')
        self.assertEqual(len([r for r in raw if r['table'] == 'thread_items']), 2)
        self.assertEqual(self.get_plan()['affected_count'], 1)

    def test_changed_history_is_protected_but_unrelated_append_is_not(self):
        proofs = self.backup()
        self.add_thread('other')
        self.assertEqual(self.get_plan(proofs)['groups'], [{'root': 'parent', 'affected': ['parent']}])
        self.history.execute("UPDATE thread_items SET item_json=? WHERE thread_id='parent'",
                             (json.dumps({'id': 'user', 'type': 'userMessage', 'content': ['changed']}),))
        self.history.commit()
        self.assertEqual(self.get_plan(proofs)['affected_count'], 0)

    def test_archive_flag_does_not_change_history_fingerprint(self):
        proofs = self.backup()
        self.state.execute('UPDATE threads SET archived=1')
        self.state.commit()
        result = self.get_plan(proofs)
        self.assertEqual(result['affected_count'], 0)
        self.assertEqual(result['threads']['parent']['reasons'], [])

    def test_parent_protected_by_pinned_recent_or_active_child(self):
        self.add_thread('child')
        self.state.execute("INSERT INTO thread_spawn_edges VALUES('parent','child')")
        self.state.commit()
        self.assertEqual(self.get_plan()['groups'], [{'root': 'parent', 'affected': ['child', 'parent']}])
        for column, value in [('is_pinned', 1), ('recency_at', 99 * 86400), ('archived', None)]:
            with self.subTest(column=column):
                self.state.execute(f'UPDATE threads SET {column}=? WHERE id=?', (value, 'child'))
                self.state.commit()
                self.assertEqual(self.get_plan()['affected_count'], 0)
                self.state.execute(f'UPDATE threads SET {column}=? WHERE id=?', (1 if column == 'recency_at' else 0, 'child'))
                self.state.commit()
        self.history.execute("UPDATE thread_turns SET status='inProgress' WHERE thread_id='child'")
        self.history.commit()
        self.assertEqual(self.get_plan()['affected_count'], 0)

    def test_unknown_descendants_and_cycles_protected(self):
        self.state.execute("INSERT INTO thread_spawn_edges VALUES('parent','missing')")
        self.state.commit()
        self.assertEqual(self.get_plan()['affected_count'], 0)
        self.state.execute("UPDATE thread_spawn_edges SET child_thread_id='parent'")
        self.state.commit()
        self.assertEqual(self.get_plan()['affected_count'], 0)

    def test_runtime_protection_and_shorter_policy_rejected(self):
        self.assertEqual(self.get_plan(protected_ids=['parent'])['affected_count'], 0)
        with self.assertRaises(ValueError):
            self.get_plan(stale_days=59)

    def test_orphan_and_open_realtime_are_retained_and_protected(self):
        self.history.execute("UPDATE thread_items SET turn_id='missing'")
        self.history.execute('INSERT INTO thread_realtime_items VALUES(?,?,3,3000,?)',
                             ('parent', 'rt', json.dumps({'id': 'rt', 'type': 'realtime_session_started',
                                                         'realtime_session_id': 'session'})))
        self.history.commit()
        result = self.get_plan()
        self.assertEqual(result['affected_count'], 0)
        self.assertIn('items_without_turn_metadata', result['threads']['parent']['reasons'])
        self.assertIn('open_realtime_session', result['threads']['parent']['reasons'])

    def test_tampered_archive_rejected(self):
        self.backup()
        normalized = next(p for p in self.output.glob('*.gz') if not p.name.endswith('.raw.jsonl.gz'))
        with gzip.open(normalized, 'wt') as handle:
            handle.write('{}\n')
        with self.assertRaises(ValueError):
            backup_proofs(self.output, self.home)

    def test_discovery_avoids_duplicate_paginated_rollout_and_keeps_legacy(self):
        sessions = self.home / 'sessions'
        sessions.mkdir()
        for name in ['parent', 'legacy']:
            (sessions / (name + '.jsonl')).write_text('{}\n')
        self.state.execute('UPDATE threads SET rollout_path=?', (str(sessions / 'parent.jsonl'),))
        self.state.commit()
        sources = discover(self.root, {})
        self.assertEqual({s.path.name for s in sources}, {'state_5.sqlite', 'legacy.jsonl', 'parent.jsonl'})
        self.assertEqual(next(s.format for s in sources if s.path.name == 'parent.jsonl'), 'codex-jsonl-raw')

    def test_missing_and_unknown_history_fail_visibly(self):
        self.history.close()
        (self.home / 'thread_history_1.sqlite').unlink()
        self.assertEqual(len(discover(self.root, {})), 1)
        with self.assertRaises(ValueError):
            self.backup()
        (self.home / 'thread_history_2.sqlite').touch()
        with self.assertRaises(ValueError):
            self.backup()

    def test_missing_metadata_history_is_still_backed_up(self):
        self.state.execute('DELETE FROM threads')
        self.state.commit()
        proofs = self.backup()
        self.assertIn('missing_thread_metadata', proofs['threads']['parent']['issues'])

    def test_protected_ids_reject_wrong_shape(self):
        for value in ['parent', {'ids': ['parent']}, [42]]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.get_plan(protected_ids=value)

    def test_raw_archive_must_actually_contain_history(self):
        self.backup()
        manifest_path = self.output / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        raw = next(self.output.glob('*.raw.jsonl.gz'))
        with gzip.open(raw, 'wt') as handle:
            handle.write('')
        from extract_portable import verify_jsonl
        manifest['sources'][0]['files'][raw.name] = verify_jsonl(raw)
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            backup_proofs(self.output, self.home)

    def test_surviving_rollout_requires_raw_backup_and_unchanged_bytes(self):
        path = self.home / 'parent.jsonl'
        path.write_text(' {"type":"tail","data":"unprojected"}\n')
        self.assertEqual(self.get_plan()['affected_count'], 0)
        result = extract_source(Source('codex', path, 'codex-jsonl-raw'), self.output)
        self.assertEqual(result['conversations'], 0)
        import base64
        raw = next(self.output.glob('codex-' + hashlib.sha256(str(path).encode()).hexdigest()[:20] + '.raw.jsonl.gz'))
        with gzip.open(raw, 'rt') as handle:
            self.assertEqual(base64.b64decode(json.loads(handle.readline())['bytes']), path.read_bytes())
        manifest_path = self.output / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['sources'].append(result)
        manifest_path.write_text(json.dumps(manifest))
        proofs = backup_proofs(self.output, self.home)
        self.assertEqual(self.get_plan(proofs)['affected_count'], 1)
        path.write_text('{"type":"tail","data":"changed"}\n')
        self.assertEqual(self.get_plan(proofs)['affected_count'], 0)

    def test_leftover_older_state_version_does_not_hide_current_history(self):
        (self.home / 'state_4.sqlite').touch()
        self.assertEqual(len(discover(self.root, {})), 1)
        self.assertEqual(self.get_plan()['affected_count'], 1)

    def test_unrecognized_item_status_is_protected(self):
        self.history.execute('UPDATE thread_items SET item_json=?',
                             (json.dumps({'id': 'user', 'type': 'imageGeneration', 'status': 'generating'}),))
        self.history.commit()
        self.assertEqual(self.get_plan()['affected_count'], 0)


if __name__ == '__main__':
    unittest.main()
