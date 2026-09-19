import gzip
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(os.name == 'posix', 'SSH fleet runner uses Unix locking')
class FleetTests(unittest.TestCase):
    def setUp(self):
        import backup_fleet
        self.fleet = backup_fleet
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        content = gzip.compress(b'{"role":"user","content":"hello"}\n')
        (self.root / 'records.jsonl.gz').write_bytes(content)
        self.manifest = {'created_at': '20260919T000000.000000Z', 'cleanup_status': 'complete',
                         'sources': [{'harness': 'pi', 'status': 'ok', 'files': {
                             'records.jsonl.gz': {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)}}}],
                         'coverage': {'pi': {'messages': 1}}}
        self.write_manifest()

    def write_manifest(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_transfer_hash_matches(self):
        result = self.fleet.verify_snapshot(self.root)
        self.assertTrue(result['backup_complete'])
        self.assertEqual(result['verified_files'], 1)

    def test_transfer_corruption_fails(self):
        (self.root / 'records.jsonl.gz').write_bytes(b'corrupted')
        with self.assertRaises(ValueError):
            self.fleet.verify_snapshot(self.root)

    def test_manifest_cannot_reference_parent_files(self):
        files = self.manifest['sources'][0]['files']
        files['../records.jsonl.gz'] = files.pop('records.jsonl.gz')
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.fleet.verify_snapshot(self.root)

    def test_empty_or_partial_source_cannot_claim_complete(self):
        self.manifest['sources'][0]['status'] = 'partial'
        self.write_manifest()
        self.assertFalse(self.fleet.verify_snapshot(self.root)['backup_complete'])
        self.manifest['sources'] = []
        self.write_manifest()
        with self.assertRaises(ValueError):
            self.fleet.verify_snapshot(self.root)

    def test_binary_approval_does_not_excuse_other_failures(self):
        self.manifest['sources'][0]['status'] = 'archive_only'
        self.write_manifest()
        self.assertFalse(self.fleet.verify_snapshot(self.root)['backup_complete'])
        self.assertTrue(self.fleet.verify_snapshot(self.root, True)['backup_complete'])
        self.manifest['missing_history'] = ['claude']
        self.write_manifest()
        self.assertFalse(self.fleet.verify_snapshot(self.root, True)['backup_complete'])

    def test_cleanup_warning_does_not_invalidate_archives(self):
        self.manifest['cleanup_status'] = 'incomplete'
        self.write_manifest()
        result = self.fleet.verify_snapshot(self.root)
        self.assertTrue(result['backup_complete'])
        self.assertEqual(result['cleanup_status'], 'incomplete')

    def test_failed_host_does_not_prevent_collecting_next_host(self):
        calls = []

        def collect(host, *args):
            calls.append(host)
            if host == 'vector':
                raise OSError('offline')
            return {'backup_complete': True, 'cleanup_status': 'complete'}

        output = self.root / 'sets'
        with patch('sys.argv', ['backup_fleet.py', '--host', 'vector', '--host', 'forge', '--output', str(output)]), \
                patch('backup_fleet.collect_host', side_effect=collect), patch('builtins.print'):
            self.assertEqual(self.fleet.main(), 2)
        self.assertEqual(calls, ['nexus', 'vector', 'forge'])
        latest = json.loads((output / 'latest.json').read_text())
        report = json.loads((Path(latest['run']) / 'backup-set.json').read_text())
        self.assertEqual(report['status'], 'needs_attention')
        self.assertTrue(report['hosts']['forge']['backup_complete'])


if __name__ == '__main__':
    unittest.main()
