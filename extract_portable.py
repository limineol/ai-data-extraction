#!/usr/bin/env python3
"""Create verified, private conversation archives; never delete source data."""
import argparse
import base64
import gzip
import hashlib
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

from harness_sources import COMMANDS, discover, installed
from harness_records import (binary_json, database_conversations, jsonl_conversation,
                             legacy_json_conversation, read_database)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def verify_jsonl(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    count = 0
    with gzip.open(path, 'rt', encoding='utf-8') as handle:
        for line in handle:
            json.loads(line)
            count += 1
    return {'sha256': digest.hexdigest(), 'records': count, 'bytes': path.stat().st_size}


def extract_source(source, output):
    identity = hashlib.sha256(str(source.path).encode()).hexdigest()[:20]
    raw_path = output / (source.harness + '-' + identity + '.raw.jsonl.gz')
    conversation_path = output / (source.harness + '-' + identity + '.jsonl.gz')
    raw_incomplete = raw_path.with_name(raw_path.name + '.incomplete')
    conversation_incomplete = conversation_path.with_name(conversation_path.name + '.incomplete')
    before = source.path.stat()
    result = {'harness': source.harness, 'source_path': str(source.path),
              'source_bytes': before.st_size, 'source_mtime': before.st_mtime,
              'conversations': 0, 'messages': 0, 'status': 'ok'}
    with gzip.open(raw_incomplete, 'wt', encoding='utf-8', compresslevel=6) as raw, \
            gzip.open(conversation_incomplete, 'wt', encoding='utf-8', compresslevel=6) as normalized:
        def raw_write(record):
            raw.write(json.dumps(record, ensure_ascii=False, default=binary_json) + '\n')

        def emit(conversation):
            conversation['source_path'] = str(source.path)
            normalized.write(json.dumps(conversation, ensure_ascii=False, default=binary_json) + '\n')
            result['conversations'] += 1
            result['messages'] += len(conversation['messages'])
            if conversation.get('archive_only'):
                result['status'] = 'archive_only'
            if conversation.get('incomplete'):
                result['status'] = 'partial'

        if source.format == 'jsonl':
            records = []
            source_hash = hashlib.sha256()
            remaining = before.st_size
            with source.path.open('rb') as handle:
                index = 0
                while remaining:
                    line = handle.readline(remaining)
                    if not line:
                        result['status'] = 'partial'
                        break
                    remaining -= len(line)
                    source_hash.update(line)
                    index += 1
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            raise ValueError('Event is not an object')
                    except (ValueError, UnicodeDecodeError):
                        raw_write({'unparsed_line': index, 'bytes': base64.b64encode(line).decode('ascii')})
                        result['invalid_lines'] = result.get('invalid_lines', 0) + 1
                        result['status'] = 'partial'
                        continue
                    raw_write(event)
                    records.append(event)
            result['source_sha256'] = source_hash.hexdigest()
            emit(jsonl_conversation(records, source))
        elif source.format == 'sqlite':
            with read_database(source.path) as connection:
                for conversation in database_conversations(connection, source, raw_write):
                    emit(conversation)
        elif source.format in ['json', 'opencode-json']:
            emit(legacy_json_conversation(source, raw_write))
        else:
            raw_write({'bytes': base64.b64encode(source.path.read_bytes()).decode('ascii')})
            result['status'] = 'archive_only'
    result['files'] = {final.name: verify_jsonl(temporary) for temporary, final in
                       [(raw_incomplete, raw_path), (conversation_incomplete, conversation_path)]}
    after = source.path.stat()
    result['changed_during_read'] = (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size)
    if source.format == 'jsonl' and result['changed_during_read']:
        # Appending to an active session is normal. Verify the exact prefix read
        # before accepting that prefix as a coherent snapshot.
        prefix_hash = hashlib.sha256()
        remaining = before.st_size
        with source.path.open('rb') as handle:
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                prefix_hash.update(chunk)
                remaining -= len(chunk)
        if remaining or prefix_hash.hexdigest() != result['source_sha256']:
            result['status'] = 'partial'
    elif source.format != 'sqlite' and result['changed_during_read']:
        result['status'] = 'partial'
    if result['messages'] == 0 and result['status'] == 'ok':
        result['status'] = 'no_messages'
    raw_incomplete.replace(raw_path)
    conversation_incomplete.replace(conversation_path)
    return result


def run(output_root, home=None, environ=None, only=None, stale_days=60):
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_root, 0o700)
    previous = {}
    expected_harnesses = set()
    baseline_warning = None
    if (output_root / 'latest.json').exists():
        try:
            previous_run = json.loads((output_root / 'latest.json').read_text())['run']
            previous_manifest = json.loads((Path(previous_run) / 'manifest.json').read_text())
            previous = previous_manifest.get('coverage', {})
            expected_harnesses.update(previous_manifest.get('expected_harnesses', []))
            expected_harnesses.update(name for name, coverage in previous.items() if coverage.get('messages', 0) > 0)
        except (OSError, ValueError, KeyError, TypeError) as error:
            baseline_warning = type(error).__name__
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    output = output_root / stamp
    output.mkdir(mode=0o700)
    sources = [s for s in discover(home, environ) if not only or s.harness in only]
    inventory = installed()
    report = {'version': 1, 'host': socket.gethostname(), 'created_at': stamp,
              'status': 'running', 'baseline_warning': baseline_warning, 'home': str(home or Path.home()), 'installed': inventory, 'sources': [], 'coverage': {}}
    write_json(output / 'manifest.json', report)
    for source in sources:
        try:
            result = extract_source(source, output)
        except Exception as error:
            # Error text may contain a fragment of private source data; keep logs to type/path.
            result = {'harness': source.harness, 'source_path': str(source.path),
                      'status': 'error', 'error_type': type(error).__name__,
                      'incomplete_files': [p.name for p in output.glob(source.harness + '-' +
                          hashlib.sha256(str(source.path).encode()).hexdigest()[:20] + '*.incomplete')]}
        report['sources'].append(result)
        if result['status'] in ['error', 'partial']:
            print(source.harness + ': ' + result['status'] + ' (' + str(source.path) + ')', flush=True)
    for name in COMMANDS:
        if only and name not in only:
            continue
        results = [r for r in report['sources'] if r['harness'] == name]
        statuses = {r['status'] for r in results}
        status = ('error' if 'error' in statuses else 'partial' if 'partial' in statuses else
                  'archive_only' if 'archive_only' in statuses else
                  'no_messages' if results and not any(r.get('messages') for r in results) else 'verified' if results else
                  'no_local_history' if inventory[name] else 'not_detected')
        report['coverage'][name] = {'status': status, 'stores': len(results),
                                    'conversations': sum(r.get('conversations', 0) for r in results),
                                    'messages': sum(r.get('messages', 0) for r in results)}
        print(name + ': ' + json.dumps(report['coverage'][name]), flush=True)
    expected_harnesses.update(name for name, coverage in report['coverage'].items() if coverage['messages'] > 0)
    report['expected_harnesses'] = sorted(expected_harnesses)
    report['missing_history'] = sorted(name for name in expected_harnesses
                                       if name in report['coverage'] and report['coverage'][name]['messages'] == 0)
    report['status'] = ('no_sources' if not sources else 'partial' if report['missing_history'] or baseline_warning or
                        any(r['status'] in ['error', 'partial', 'archive_only'] for r in report['sources']) else
                        'verified_with_empty_stores' if any(r['status'] == 'no_messages' for r in report['sources']) else 'verified')
    write_json(output / 'manifest.json', report)
    from monthly_cleanup import candidate_report
    try:
        cleanup = candidate_report(report, home, stale_days)
    except (OSError, RuntimeError) as error:
        cleanup = {'mode': 'report_only', 'policy_approved': False, 'error_type': type(error).__name__}
    write_json(output / 'cleanup-candidates.json', cleanup)
    report['cleanup_status'] = 'incomplete' if cleanup.get('error_type') or cleanup.get('errors') else 'complete'
    write_json(output / 'manifest.json', report)
    write_json(output_root / 'latest.json', {'run': str(output), 'status': report['status']})
    print('Manifest: ' + str(output / 'manifest.json'), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path.home() / '.local/share/ai-data-extraction/backups')
    parser.add_argument('--inventory', action='store_true')
    parser.add_argument('--harness', action='append', choices=COMMANDS)
    parser.add_argument('--stale-days', type=int, default=60)
    args = parser.parse_args()
    if args.stale_days < 1:
        parser.error('--stale-days must be positive')
    os.umask(0o077)
    if args.inventory:
        print(json.dumps({'installed': installed(), 'sources': [dict(harness=s.harness, path=str(s.path), format=s.format)
                                                               for s in discover()]}, indent=2))
        return 0
    report = run(args.output.expanduser().resolve(), only=args.harness, stale_days=args.stale_days)
    return 0 if report['status'] in ['verified', 'verified_with_empty_stores'] else 2


if __name__ == '__main__':
    sys.exit(main())
