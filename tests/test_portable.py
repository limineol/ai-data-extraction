import gzip
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from extract_portable import extract_source, run, verify_jsonl
from harness_sources import Source, discover
from monthly_cleanup import candidate_report


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.output = self.home / 'output'
        self.output.mkdir()

    def jsonl(self, relative, records):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(record) + '\n' for record in records))
        return path

    def extract(self, name, path, fmt='jsonl'):
        result = extract_source(Source(name, path, fmt), self.output)
        output = next(p for p in self.output.glob(name + '-*.jsonl.gz') if '.raw.' not in p.name)
        with gzip.open(output, 'rt') as handle:
            records = [json.loads(line) for line in handle]
        return result, records

    def database(self, relative, schema):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.executescript(schema)
        return path, connection

    def test_discovery_profiles_archived_subagents_and_environment(self):
        expected = [self.jsonl(path, []) for path in [
            '.codex/archived_sessions/rollout-old.jsonl',
            '.claude-k3/projects/demo/session/subagents/agent-one.jsonl',
            'custom-codex/sessions/rollout-custom.jsonl',
            '.openclaw/agents/main/agent/codex-home/sessions/rollout-nested.jsonl',
            '.omp/agent/sessions/demo/child/__advisor.jsonl']]
        self.jsonl('.codex/auth.json', [])
        sources = discover(self.home, {'CODEX_HOME': str(self.home / 'custom-codex')})
        self.assertEqual({s.path for s in sources}, {p.resolve() for p in expected})

    def test_codex_preserves_tools_without_double_counting_messages(self):
        path = self.jsonl('rollout.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'one'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Hello'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'Hello'}]}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'a', 'arguments': '{}', 'name': 'read'}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'a', 'output': 'contents'}}])
        result, records = self.extract('codex', path)
        self.assertEqual(result['messages'], 3)
        self.assertEqual(records[0]['messages'][2]['output'], 'contents')
        self.assertEqual(result['files'][next(n for n in result['files'] if '.raw.' in n)]['records'], 5)

    def test_message_envelopes_preserve_branch_parent_and_tools(self):
        for name in ['claude', 'droid', 'pi', 'omp', 'openclaw']:
            with self.subTest(name=name):
                path = self.jsonl(name + '.jsonl', [{'type': 'message', 'id': 'child', 'parentId': 'parent',
                    'message': {'role': 'assistant', 'content': [{'type': 'toolCall', 'id': 'tool', 'arguments': {'x': 1}}]}}])
                result, records = self.extract(name, path)
                self.assertEqual(records[0]['messages'][0]['parentId'], 'parent')
                self.assertEqual(records[0]['messages'][0]['content'][0]['arguments'], {'x': 1})
                self.assertEqual(result['status'], 'ok')

    def test_malformed_line_is_preserved_and_not_successful(self):
        path = self.jsonl('bad.jsonl', [{'type': 'user', 'content': 'hello'}])
        with path.open('ab') as handle:
            handle.write(b'{broken\xff')
        result, _ = self.extract('grok', path)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['invalid_lines'], 1)
        raw = next(self.output.glob('*.raw.jsonl.gz'))
        with gzip.open(raw, 'rt') as handle:
            records = [json.loads(line) for line in handle]
        self.assertEqual(records[1]['unparsed_line'], 2)

    def test_opencode2_wal_and_sensitive_tables_excluded(self):
        path, connection = self.database('opencode.db', '''
            PRAGMA journal_mode=WAL;
            CREATE TABLE session_v2(id TEXT PRIMARY KEY, title TEXT);
            CREATE TABLE session_message(id TEXT, session_id TEXT, type TEXT, seq INT, data TEXT);
            CREATE TABLE credential(value TEXT);
            INSERT INTO session_v2 VALUES ('s', 'Session');
            INSERT INTO credential VALUES ('DO-NOT-EXPORT');
        ''')
        connection.execute('INSERT INTO session_message VALUES (?,?,?,?,?)', ('m', 's', 'user', 1, json.dumps({'text': 'hello'})))
        connection.commit()
        try:
            result, records = self.extract('opencode', path, 'sqlite')
            self.assertEqual(result['messages'], 1)
            self.assertEqual(records[0]['messages'][0]['content'], 'hello')
            with gzip.open(next(self.output.glob('*.raw.jsonl.gz')), 'rt') as handle:
                self.assertNotIn('DO-NOT-EXPORT', handle.read())
        finally:
            connection.close()

    def test_devin_keeps_branch_nodes(self):
        path, connection = self.database('sessions.db', '''
            CREATE TABLE sessions(id TEXT);
            CREATE TABLE message_nodes(session_id TEXT, node_id INT, parent_node_id INT, chat_message TEXT);
            INSERT INTO sessions VALUES ('s');
        ''')
        for node, parent in [(1, None), (2, 1), (3, 1)]:
            connection.execute('INSERT INTO message_nodes VALUES (?,?,?,?)', ('s', node, parent, json.dumps({'role': 'user', 'content': str(node)})))
        connection.commit()
        connection.close()
        _, records = self.extract('devin', path, 'sqlite')
        self.assertEqual([m['parent_node_id'] for m in records[0]['messages']], [None, 1, 1])

    def test_antigravity_is_archive_only_with_exact_binary(self):
        path, connection = self.database('trajectory.db', '''
            CREATE TABLE trajectory_meta(trajectory_id TEXT);
            CREATE TABLE steps(idx INT, step_payload BLOB);
        ''')
        connection.execute('INSERT INTO steps VALUES (?,?)', (1, b'\x00\xff\x10'))
        connection.commit()
        connection.close()
        result, records = self.extract('antigravity', path, 'sqlite')
        self.assertEqual(result['status'], 'archive_only')
        self.assertTrue(records[0]['archive_only'])
        with gzip.open(next(self.output.glob('*.raw.jsonl.gz')), 'rt') as handle:
            row = json.loads(handle.readline())
        self.assertEqual(row['row']['step_payload'], {'encoding': 'base64', 'data': 'AP8Q'})

    def test_no_history_does_not_claim_live_verification(self):
        with patch('extract_portable.installed', return_value={'pi': ['/bin/pi']}):
            result = run(self.output, self.home, {}, only=['pi'])
        self.assertEqual(result['coverage']['pi']['status'], 'no_local_history')

    def test_corrupt_gzip_fails_verification(self):
        path = self.output / 'bad.gz'
        path.write_bytes(b'not gzip')
        with self.assertRaises(OSError):
            verify_jsonl(path)

    def test_cleanup_never_authorizes_deletion(self):
        manifest = {'sources': [{'harness': 'pi', 'source_path': '/old/session', 'source_mtime': 0,
                                 'source_bytes': 1, 'status': 'ok'}]}
        report = candidate_report(manifest, self.home)
        self.assertFalse(report['policy_approved'])
        self.assertEqual(report['sessions'][0]['action'], 'review_only')

    def test_grok_roles_and_tool_calls(self):
        path = self.jsonl('grok.jsonl', [
            {'type': 'user', 'content': 'Hello'},
            {'type': 'assistant', 'content': '', 'tool_calls': [{'id': 't', 'name': 'read'}]},
            {'type': 'tool_result', 'tool_call_id': 't', 'content': 'result'}])
        _, records = self.extract('grok', path)
        self.assertEqual([m['role'] for m in records[0]['messages']], ['user', 'assistant', 'tool'])
        self.assertEqual(records[0]['messages'][1]['tool_calls'][0]['id'], 't')

    def test_gemini_preserves_thoughts_and_token_counts(self):
        path = self.home / 'gemini.json'
        path.write_text(json.dumps({'sessionId': 'g', 'messages': [
            {'type': 'user', 'content': 'hello'},
            {'type': 'gemini', 'content': 'world', 'thoughts': [{'text': 'reason'}], 'tokens': {'total': 12}}]}))
        _, records = self.extract('gemini', path, 'json')
        self.assertEqual(records[0]['messages'][1]['role'], 'assistant')
        self.assertEqual(records[0]['messages'][1]['tokens']['total'], 12)

    def test_forgecode_decodes_text_and_tools(self):
        path, connection = self.database('forge.db', 'CREATE TABLE conversations(conversation_id TEXT, context TEXT);')
        context = {'messages': [{'message': {'text': {'role': 'User', 'content': 'hello'}}},
                                {'message': {'tool': {'call_id': 't', 'output': {'ok': True}}}}]}
        connection.execute('INSERT INTO conversations VALUES (?,?)', ('f', json.dumps(context)))
        connection.commit()
        connection.close()
        _, records = self.extract('forgecode', path, 'sqlite')
        self.assertEqual(records[0]['messages'][0]['role'], 'user')
        self.assertEqual(records[0]['messages'][1]['content'], {'ok': True})

    def test_hermes_includes_compacted_messages(self):
        path, connection = self.database('hermes.db', '''
            CREATE TABLE sessions(id TEXT, pinned INT);
            CREATE TABLE messages(id INT, session_id TEXT, timestamp REAL, role TEXT, content TEXT, compacted INT);
            INSERT INTO sessions VALUES ('h', 1);
            INSERT INTO messages VALUES (1, 'h', 1, 'user', 'old', 1);
            INSERT INTO messages VALUES (2, 'h', 2, 'assistant', 'new', 0);
        ''')
        connection.close()
        _, records = self.extract('hermes', path, 'sqlite')
        self.assertEqual(len(records[0]['messages']), 2)
        self.assertEqual(records[0]['metadata']['pinned'], 1)

    def test_cursor_missing_reference_fails_instead_of_losing_messages(self):
        import hashlib
        path, connection = self.database('store.db', 'CREATE TABLE blobs(id TEXT, data BLOB); CREATE TABLE meta(value TEXT);')
        root, missing = hashlib.sha256(b'root').hexdigest(), hashlib.sha256(b'missing').hexdigest()
        connection.execute('INSERT INTO blobs VALUES (?,?)', (root, b'\x0a\x20' + bytes.fromhex(missing)))
        connection.execute('INSERT INTO meta VALUES (?)', (json.dumps({'latestRootBlobId': root}).encode().hex(),))
        connection.commit()
        connection.close()
        with self.assertRaises(ValueError):
            self.extract('cursor-cli', path, 'sqlite')

    def test_copilot_message_and_tool_completion(self):
        path = self.jsonl('copilot.jsonl', [
            {'type': 'user.message', 'data': {'content': 'hello'}},
            {'type': 'assistant.message', 'data': {'content': 'answer'}},
            {'type': 'tool.execution_complete', 'data': {'toolCallId': 't', 'result': {'content': 'result'}}}])
        _, records = self.extract('copilot', path)
        self.assertEqual([m['role'] for m in records[0]['messages']], ['user', 'assistant', 'tool'])
        self.assertEqual(records[0]['messages'][2]['result']['content'], 'result')

    def test_slate_legacy_message_parts(self):
        session = self.home / 'storage/session/project/s.json'
        session.parent.mkdir(parents=True)
        session.write_text(json.dumps({'id': 's'}))
        message = self.home / 'storage/message/s/m.json'
        message.parent.mkdir(parents=True)
        message.write_text(json.dumps({'id': 'm', 'role': 'assistant', 'time': {'created': 1}}))
        part = self.home / 'storage/part/m/p.json'
        part.parent.mkdir(parents=True)
        part.write_text(json.dumps({'type': 'text', 'text': 'hello'}))
        _, records = self.extract('slate', session, 'opencode-json')
        self.assertEqual(records[0]['messages'][0]['content'][0]['text'], 'hello')

    def test_openclaw_does_not_parse_json_sidecars_as_transcripts(self):
        self.jsonl('.openclaw/agents/main/sessions/s.jsonl', [])
        self.jsonl('.openclaw/agents/main/sessions/s.jsonl.codex-app-server.json', [])
        self.jsonl('.openclaw/agents/main/sessions/s.jsonl.deleted.2026-01-01', [])
        self.assertEqual(len(discover(self.home, {})), 2)

    def test_cursor_source_code_is_not_a_reference_tree(self):
        from extract_cursor_cli import parse_ref_list
        self.assertEqual(parse_ref_list(b'import something\n ' + b'a' * 32 + b';\n'), [])
        self.assertEqual(parse_ref_list(b'\x0a\x20' + b'a' * 32), [(b'a' * 32).hex()])

    def test_append_during_extraction_keeps_verified_initial_prefix(self):
        from harness_records import jsonl_conversation
        path = self.jsonl('active.jsonl', [{'type': 'user', 'content': 'first'}])

        def append(records, source):
            with path.open('a') as handle:
                handle.write(json.dumps({'type': 'assistant', 'content': 'later'}) + '\n')
            return jsonl_conversation(records, source)

        with patch('extract_portable.jsonl_conversation', side_effect=append):
            result, records = self.extract('grok', path)
        self.assertEqual(result['status'], 'ok')
        self.assertTrue(result['changed_during_read'])
        self.assertEqual(len(records[0]['messages']), 1)

    def test_rewrite_during_extraction_is_partial(self):
        from harness_records import jsonl_conversation
        path = self.jsonl('active.jsonl', [{'type': 'user', 'content': 'first'}])

        def rewrite(records, source):
            path.write_text(json.dumps({'type': 'user', 'content': 'rewritten'}))
            return jsonl_conversation(records, source)

        with patch('extract_portable.jsonl_conversation', side_effect=rewrite):
            result, _ = self.extract('grok', path)
        self.assertEqual(result['status'], 'partial')

    def test_mixed_codex_events_keep_order_and_reasoning(self):
        path = self.jsonl('mixed.jsonl', [
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'legacy'}},
            {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'read'}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': 'old answer'}},
            {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'modern'}},
            {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'text': 'modern'}]}},
            {'type': 'response_item', 'payload': {'type': 'reasoning', 'content': [{'text': 'reason'}]}}])
        _, records = self.extract('codex', path)
        messages = records[0]['messages']
        self.assertEqual(len(messages), 5)
        self.assertEqual(messages[0]['content'], 'legacy')
        self.assertEqual(messages[1]['name'], 'read')
        self.assertEqual(messages[2]['content'], 'old answer')
        self.assertEqual(messages[-1]['content'], [{'text': 'reason'}])

    def test_failed_database_archives_are_marked_incomplete(self):
        path, connection = self.database('bad.db', 'CREATE TABLE unknown(x TEXT);')
        connection.close()
        with self.assertRaises(ValueError):
            self.extract('opencode', path, 'sqlite')
        self.assertEqual(len(list(self.output.glob('*.incomplete'))), 2)
        self.assertEqual(list(self.output.glob('*.jsonl.gz')), [])

    def test_repeated_cursor_message_reference_preserves_occurrences(self):
        from extract_cursor_cli import resolve_messages
        path, connection = self.database('repeat.db', 'CREATE TABLE blobs(id TEXT, data BLOB);')
        leaf = 'aa' * 32
        connection.execute('INSERT INTO blobs VALUES (?,?)', (leaf, json.dumps({'role': 'user', 'content': 'repeat'}).encode()))
        connection.execute('INSERT INTO blobs VALUES (?,?)', ('root', (b'\x0a\x20' + bytes.fromhex(leaf)) * 2))
        self.assertEqual(len(resolve_messages(connection, 'root')), 2)
        connection.close()

    @unittest.skipUnless(os.name == 'posix', 'Unix scheduler')
    def test_monthly_status_flags_empty_discovery_and_binary_archive(self):
        import monthly_backup
        for source_status, allow, expected_exit, expected_status in [
            (None, False, 2, 'needs_attention'),
            ('archive_only', False, 2, 'needs_attention'),
            ('archive_only', True, 0, 'completed_with_binary_archive'),
            ('no_messages', False, 0, 'completed_with_empty_stores')]:
            with self.subTest(status=source_status, allow=allow):
                args = ['monthly_backup.py', '--output', str(self.output)]
                if allow:
                    args.append('--allow-archive-only')
                report = {'sources': [] if source_status is None else [{'status': source_status}],
                          'status': 'partial' if allow else 'verified', 'cleanup_status': 'complete'}
                with patch('sys.argv', args), patch('monthly_backup.run', return_value=report):
                    self.assertEqual(monthly_backup.main(), expected_exit)
                self.assertEqual(json.loads((self.output / 'job-status.json').read_text())['status'], expected_status)

    @unittest.skipUnless(os.name == 'posix', 'Unix locking')
    def test_monthly_lock_prevents_overlapping_run(self):
        import fcntl
        import monthly_backup
        with (self.output / '.backup.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch('sys.argv', ['monthly_backup.py', '--output', str(self.output)]), patch('monthly_backup.run') as run_backup:
                self.assertEqual(monthly_backup.main(), 1)
                run_backup.assert_not_called()

    def test_worktree_failure_does_not_hide_later_worktrees(self):
        repo = self.home / 'Projects/repo'
        (repo / '.git').mkdir(parents=True)
        broken, dirty = self.home / 'broken', self.home / 'dirty'
        broken.mkdir()
        dirty.mkdir()

        def inspect(path, *args):
            if args[:2] == ('worktree', 'list'):
                return f'worktree {repo}\n\nworktree {broken}\n\nworktree {dirty}\n'
            if path == broken:
                raise RuntimeError('unreadable')
            if args[0] == 'status':
                return '?? untracked.txt'
            if args[0] == 'rev-parse':
                raise RuntimeError('no upstream')
            if args[0] == 'log':
                return '1'
            raise AssertionError(args)

        with patch('monthly_cleanup.git', side_effect=inspect):
            report = candidate_report({'sources': []}, self.home)
        self.assertEqual(len(report['worktrees']), 2)
        self.assertEqual(len(report['errors']), 1)
        self.assertTrue(report['worktrees'][1]['dirty'])

    @unittest.skipUnless(os.name == 'posix', 'Symlink permission differs on Windows')
    def test_symlinked_session_directory_is_discovered_without_looping(self):
        target = self.home / 'session-target'
        target.mkdir()
        (target / 'one.jsonl').write_text('')
        (target / 'loop').symlink_to(target, target_is_directory=True)
        sessions = self.home / '.pi/agent/sessions'
        sessions.mkdir(parents=True)
        (sessions / 'project').symlink_to(target, target_is_directory=True)
        self.assertEqual(len(discover(self.home, {})), 1)


if __name__ == '__main__':
    unittest.main()
