#!/usr/bin/env python3
"""Unix scheduler entry point: serialize backups and record an explicit result."""
import argparse
import fcntl
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from extract_portable import run, write_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path.home() / '.local/share/ai-data-extraction/backups')
    parser.add_argument('--allow-archive-only', action='store_true',
                        help='Allow verified binary Antigravity archives; never enables cleanup')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.output.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / '.backup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('A backup is already running.', flush=True)
            return 1
        started = datetime.now(timezone.utc).isoformat()
        write_json(root / 'job-status.json', {'status': 'running', 'started_at': started})
        try:
            report = run(root)
            acceptable = {'ok', 'no_messages'}
            if args.allow_archive_only:
                acceptable.add('archive_only')
            failed = [r for r in report['sources'] if r['status'] not in acceptable]
            status = 'needs_attention' if failed else 'completed_with_binary_archive' if report['status'] == 'partial' else 'completed'
            write_json(root / 'job-status.json', {'status': status, 'started_at': started,
                       'finished_at': datetime.now(timezone.utc).isoformat(), 'failed_stores': len(failed),
                       'cleanup': 'report_only'})
            return 2 if failed else 0
        except Exception as error:
            write_json(root / 'job-status.json', {'status': 'failed', 'started_at': started,
                                                 'error_type': type(error).__name__})
            raise


if __name__ == '__main__':
    sys.exit(main())
