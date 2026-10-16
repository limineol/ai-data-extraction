#!/usr/bin/env python3
"""Plan Codex quarantine from verified archives; never modify live stores."""
import argparse
import base64
import gzip
import hashlib
import json
import time
from pathlib import Path

from codex_history import columns, open_history, summarize_history, thread_records
from extract_portable import verify_jsonl, write_json


def backup_proofs(run, home):
    manifest = json.loads((run / 'manifest.json').read_text())
    sources = [s for s in manifest['sources'] if s.get('format') == 'codex-sqlite'
               and Path(s['source_path']).resolve() == (home / 'state_5.sqlite').resolve()]
    if len(sources) != 1 or sources[0]['status'] != 'ok':
        raise ValueError('A complete paginated backup for this Codex home is required')
    source = sources[0]
    if len(source['files']) != 2 or sum(name.endswith('.raw.jsonl.gz') for name in source['files']) != 1:
        raise ValueError('Both raw and normalized archives are required')
    proofs, raw_digests, raw_counts = {}, {}, {}
    for name, expected in source['files'].items():
        if Path(name).name != name or '/' in name or '\\' in name or (run / name).is_symlink():
            raise ValueError('Unsafe archive path')
        if verify_jsonl(run / name) != expected:
            raise ValueError('Archive verification failed')
        with gzip.open(run / name, 'rt', encoding='utf-8') as handle:
            for line in handle:
                conversation = json.loads(line)
                if name.endswith('.raw.jsonl.gz'):
                    if conversation['database'] == 'history':
                        thread_id = conversation['row']['thread_id']
                        record = {'table': conversation['table'], 'row': conversation['row']}
                        raw_digests.setdefault(thread_id, hashlib.sha256()).update(
                            (json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=True) + '\n').encode())
                        counts = raw_counts.setdefault(thread_id, {})
                        counts[record['table']] = counts.get(record['table'], 0) + 1
                    continue
                thread_id = conversation['session_id']
                if thread_id in proofs:
                    raise ValueError('Duplicate backed-up thread')
                proofs[thread_id] = conversation['history_proof']
    for thread_id, proof in proofs.items():
        digest = raw_digests.get(thread_id, hashlib.sha256()).hexdigest()
        if proof['sha256'] != digest or proof['counts'] != raw_counts.get(thread_id, {}):
            raise ValueError('History proof does not match retained raw rows')
    rollouts = {str(Path(s['source_path']).resolve()): s for s in manifest['sources'] if s['harness'] == 'codex' and 'source_sha256' in s}
    for source in rollouts.values():
        if source['status'] not in ['ok', 'no_messages']:
            continue
        for name, expected in source['files'].items():
            if Path(name).name != name or '/' in name or '\\' in name or (run / name).is_symlink():
                raise ValueError('Unsafe rollout archive path')
            if verify_jsonl(run / name) != expected:
                raise ValueError('Rollout archive verification failed')
            if source.get('format') == 'codex-jsonl-raw' and name.endswith('.raw.jsonl.gz'):
                digest = hashlib.sha256()
                with gzip.open(run / name, 'rt', encoding='utf-8') as handle:
                    for line in handle:
                        digest.update(base64.b64decode(json.loads(line)['bytes'], validate=True))
                if digest.hexdigest() != source['source_sha256']:
                    raise ValueError('Rollout proof does not match retained raw bytes')
    return {'threads': proofs, 'rollouts': rollouts}


def plan(home, proofs, stale_days=60, protected_ids=(), now=None):
    if not isinstance(protected_ids, (list, tuple, set)) or any(not isinstance(value, str) for value in protected_ids):
        raise ValueError('Protected IDs must be a list of strings')
    if stale_days < 60:
        raise ValueError('Quarantine requires at least 60 days of inactivity')
    cutoff = (time.time() if now is None else now) - stale_days * 86400
    with open_history(home) as (connection, tables):
        required = {'archived', 'is_pinned', 'created_at', 'updated_at', 'recency_at', 'history_mode'}
        if not required <= columns(connection, 'main', 'threads'):
            raise ValueError('Missing thread protection metadata')
        if not {'parent_thread_id', 'child_thread_id'} <= columns(connection, 'main', 'thread_spawn_edges'):
            raise ValueError('Missing descendant graph')
        metadata = {row['id']: dict(row) for row in connection.execute('SELECT * FROM threads')}
        children = {thread_id: set() for thread_id in metadata}
        for edge in connection.execute('SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges'):
            children.setdefault(edge[0], set()).add(edge[1])
        protected_ids = set(protected_ids)
        states = {}
        for thread_id, thread in metadata.items():
            reasons = []
            if thread['archived'] not in (0, 1) or thread['is_pinned'] not in (0, 1):
                reasons.append('unknown_flags')
            if thread['is_pinned'] or thread_id in protected_ids:
                reasons.append('pinned_or_runtime_protected')
            activity = [thread.get(key) for key in ('created_at', 'updated_at', 'recency_at')]
            activity.extend(thread[key] / 1000 for key in ('created_at_ms', 'updated_at_ms', 'recency_at_ms') if thread.get(key) is not None)
            if any(not isinstance(value, (int, float)) or value <= 0 for value in activity):
                reasons.append('unknown_activity')
            current = summarize_history(thread_records(connection, tables, thread_id))
            activity.append(current['last_history_activity'])
            if max((v for v in activity if isinstance(v, (int, float))), default=0) >= cutoff:
                reasons.append('recent_activity')
            if thread['history_mode'] != 'paginated':
                reasons.append('unsupported_cleanup_storage')
            reasons.extend(current['issues'])
            rollout = Path(thread['rollout_path']) if thread['rollout_path'] else None
            if rollout and not rollout.is_absolute():
                reasons.append('unknown_rollout_path')
            if rollout and rollout.exists():
                saved = proofs['rollouts'].get(str(rollout.resolve()))
                if not saved or saved.get('format') != 'codex-jsonl-raw' or saved['status'] not in ['ok', 'no_messages']:
                    reasons.append('rollout_not_backed_up')
                else:
                    digest = hashlib.sha256()
                    with rollout.open('rb') as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                            digest.update(chunk)
                    if digest.hexdigest() != saved['source_sha256']:
                        reasons.append('rollout_changed')
            backed_up = proofs['threads'].get(thread_id)
            if not backed_up or backed_up['sha256'] != current['sha256'] or backed_up['counts'] != current['counts']:
                reasons.append('history_not_backed_up_or_changed')
            states[thread_id] = {'archived': thread['archived'], 'reasons': sorted(set(reasons)),
                                 'history_sha256': current['sha256'], 'counts': current['counts']}

    def descendants(root):
        seen, visiting = set(), set()
        stack = [(root, False)]
        while stack:
            thread_id, leaving = stack.pop()
            if leaving:
                visiting.remove(thread_id)
                seen.add(thread_id)
                continue
            if thread_id in visiting:
                raise ValueError('Cycle in descendant graph')
            if thread_id in seen:
                continue
            if thread_id not in states:
                raise ValueError('Missing descendant metadata')
            visiting.add(thread_id)
            stack.append((thread_id, True))
            stack.extend((child, False) for child in children.get(thread_id, ()))
        return seen

    eligible = {}
    for thread_id, state in states.items():
        if state['archived'] == 1 or state['reasons']:
            continue
        try:
            family = descendants(thread_id)
        except ValueError:
            state['reasons'].append('unknown_descendant_graph')
            continue
        affected = {member for member in family if states[member]['archived'] != 1}
        if any(states[member]['reasons'] for member in affected):
            state['reasons'].append('protected_descendant')
        else:
            eligible[thread_id] = affected
    groups, covered = [], set()
    for root, affected in sorted(eligible.items(), key=lambda item: (-len(item[1]), item[0])):
        if root in covered:
            continue
        if affected & covered:
            states[root]['reasons'].append('overlapping_archive_group')
            continue
        groups.append({'root': root, 'affected': sorted(affected)})
        covered.update(affected)
    return {'mode': 'native_archive_plan', 'inactive_days': stale_days, 'quarantine_days': 30,
            'evaluated_at_epoch': time.time() if now is None else now, 'cutoff_epoch': cutoff, 'groups': groups, 'affected_count': len(covered), 'threads': states,
            'requires': ['Confirmed off-device backup', 'Fresh runtime idle/pin check for every affected chat',
                         'Recompute this plan immediately before native archive', 'Verify history and archive state afterward'],
            'permanent_deletion_enabled': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--backup', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--protected-ids', type=Path, help='JSON list of pinned or active thread IDs from the app')
    args = parser.parse_args()
    protected = json.loads(args.protected_ids.read_text()) if args.protected_ids else []
    report = plan(args.home, backup_proofs(args.backup, args.home), protected_ids=protected)
    write_json(args.output, report)
    print(json.dumps({'groups': len(report['groups']), 'affected': report['affected_count']}))


if __name__ == '__main__':
    main()
