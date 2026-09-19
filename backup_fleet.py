#!/usr/bin/env python3
"""Run host-local extraction over SSH and collect verified snapshots with SCP."""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from extract_portable import write_json
from harness_sources import COMMANDS

SSH_OPTIONS = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
               '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3']


def latest_run(root):
    latest = json.loads((root / 'latest.json').read_text())
    run = Path(latest['run']).resolve()
    if run.parent != root.resolve():
        raise ValueError('Latest run is outside the configured backup root')
    manifest = json.loads((run / 'manifest.json').read_text())
    if manifest['status'] == 'running' or set(manifest['coverage']) != set(COMMANDS):
        raise ValueError('Latest run is unfinished or contains only selected harnesses')
    return run


def worker(collect_latest, allow_archive_only):
    root = Path.home() / '.local/share/ai-data-extraction/backups'
    if not collect_latest:
        before = (root / 'latest.json').read_bytes() if (root / 'latest.json').exists() else None
        command = [sys.executable, str(Path(__file__).with_name('monthly_backup.py'))]
        if allow_archive_only:
            command.append('--allow-archive-only')
        completed = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr, timeout=5400)
        after = (root / 'latest.json').read_bytes() if (root / 'latest.json').exists() else None
        if completed.returncode not in (0, 2) or not after or after == before:
            raise RuntimeError('Extraction did not produce a new completed manifest')
    return {'run': str(latest_run(root)), 'reused_existing_snapshot': collect_latest}


def verify_snapshot(directory, allow_archive_only=False):
    manifest = json.loads((directory / 'manifest.json').read_text())
    if not manifest.get('sources'):
        raise ValueError('Snapshot contains no source stores')
    files = 0
    for source in manifest['sources']:
        for name, expected in source.get('files', {}).items():
            if Path(name).name != name or '/' in name or '\\' in name:
                raise ValueError('Unsafe archive filename in manifest')
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('Missing or symlinked archive')
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected['sha256'] or path.stat().st_size != expected['bytes']:
                raise ValueError('Transferred archive does not match its manifest')
            files += 1
    if not files:
        raise ValueError('No verified archives in snapshot')
    acceptable = {'ok', 'no_messages'}
    if allow_archive_only:
        acceptable.add('archive_only')
    failures = [s['harness'] for s in manifest['sources']
                if s['status'] not in acceptable or not s.get('files')]
    complete = not failures and not manifest.get('missing_history') and not manifest.get('baseline_warning')
    return {'backup_complete': complete, 'verified_files': files,
            'source_created_at': manifest['created_at'], 'coverage': manifest['coverage'],
            'failed_harnesses': sorted(set(failures)),
            'cleanup_status': manifest.get('cleanup_status', 'unknown'),
            'missing_history': manifest.get('missing_history', []),
            'baseline_warning': manifest.get('baseline_warning')}


def link_or_copy(source, destination):
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def collect_host(host, destination, collect_latest, allow_archive_only, log):
    flags = ['--worker']
    if collect_latest:
        flags.append('--collect-latest')
    if allow_archive_only:
        flags.append('--allow-archive-only')
    if host == 'nexus':
        command = [sys.executable, str(Path(__file__).resolve()), *flags]
    else:
        remote = 'python3 "$HOME/Projects/ai-data-extraction/backup_fleet.py" ' + shlex.join(flags)
        command = ['ssh', *SSH_OPTIONS, '--', host, remote]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=log, text=True, timeout=6000, check=True)
    result = json.loads(completed.stdout)
    run = result['run']
    if not isinstance(run, str) or not run.startswith('/') or any(c in run for c in '\n\r\x00'):
        raise ValueError('Invalid worker output path')
    if host == 'nexus':
        shutil.copytree(run, destination, copy_function=link_or_copy)
    else:
        # Current OpenSSH uses SFTP for scp, so the path is one literal argument.
        # Refuse shell metacharacters for compatibility with older remote scp modes.
        if not re.fullmatch(r'/[A-Za-z0-9_./-]+', run):
            raise ValueError('Remote backup path contains unsupported characters')
        subprocess.run(['scp', '-r', '-B', *SSH_OPTIONS, '--', host + ':' + run, str(destination)],
                       stdout=log, stderr=log, timeout=6000, check=True)
    return dict(verify_snapshot(destination, allow_archive_only),
                reused_existing_snapshot=result['reused_existing_snapshot'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', action='append', default=[], help='SSH alias; repeat for each remote')
    parser.add_argument('--output', type=Path, default=Path.home() / '.local/share/ai-data-extraction/monthly')
    parser.add_argument('--allow-archive-only', action='store_true')
    parser.add_argument('--collect-latest', action='store_true', help='Collect existing completed snapshots without extracting again')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    os.umask(0o077)
    if args.worker:
        print(json.dumps(worker(args.collect_latest, args.allow_archive_only)))
        return 0
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', host) or host == 'nexus' for host in args.host):
        parser.error('Use simple SSH aliases distinct from the reserved local label nexus')
    if len(set(args.host)) != len(args.host):
        parser.error('Remote aliases must be unique')
    root = args.output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    with (root / '.fleet.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another fleet backup is running.')
            return 1
        batch = root / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        batch.mkdir(mode=0o700)
        report = {'status': 'running', 'hosts': {}, 'cleanup': 'report_only',
                  'reused_existing_snapshots': args.collect_latest}
        write_json(batch / 'backup-set.json', report)
        for host in ['nexus', *args.host]:
            try:
                with (batch / (host + '.log')).open('w') as log:
                    report['hosts'][host] = collect_host(host, batch / host, args.collect_latest, args.allow_archive_only, log)
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                report['hosts'][host] = {'backup_complete': False, 'error_type': type(error).__name__}
            write_json(batch / 'backup-set.json', report)
            print(host + ': ' + ('verified' if report['hosts'][host]['backup_complete'] else 'needs attention'), flush=True)
        complete = all(host['backup_complete'] for host in report['hosts'].values())
        warnings = any(host.get('cleanup_status') != 'complete' for host in report['hosts'].values())
        report['status'] = 'needs_attention' if not complete else 'completed_with_cleanup_warnings' if warnings else 'completed'
        write_json(batch / 'backup-set.json', report)
        write_json(root / 'latest.json', {'run': str(batch), 'status': report['status']})
        print('Backup set: ' + str(batch), flush=True)
        return 0 if complete else 2


if __name__ == '__main__':
    sys.exit(main())
