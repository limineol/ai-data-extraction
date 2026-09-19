"""Read-only cleanup candidates. Age alone never authorizes deletion."""
import subprocess
import time
from pathlib import Path


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Git inspection failed')
    return result.stdout


def candidate_report(manifest, home=None, stale_days=60):
    home = Path(home or Path.home())
    cutoff = time.time() - stale_days * 86400
    sessions = []
    for source in manifest['sources']:
        if source.get('source_mtime', time.time()) < cutoff:
            sessions.append({'harness': source['harness'], 'path': source['source_path'],
                             'bytes': source.get('source_bytes', 0), 'backup_status': source['status'],
                             'action': 'review_only',
                             'reason': 'Store mtime is old; per-session activity, pins and active processes still require verification.'})
    worktrees, seen, errors = [], set(), []
    projects = home / 'Projects'
    if projects.is_dir():
        try:
            repositories = sorted(projects.iterdir())
        except OSError as error:
            repositories = []
            errors.append({'path': str(projects), 'error_type': type(error).__name__})
        for repository in repositories:
            if not (repository / '.git').exists():
                continue
            try:
                blocks = git(repository, 'worktree', 'list', '--porcelain').strip().split('\n\n')
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
                errors.append({'repository': str(repository), 'error_type': type(error).__name__})
                continue
            for block in blocks[1:]:
                entry = {'repository': str(repository), 'action': 'review_only'}
                try:
                    details = dict(line.split(' ', 1) if ' ' in line else (line, True) for line in block.splitlines())
                    path = Path(details['worktree'])
                    if str(path) in seen:
                        continue
                    seen.add(str(path))
                    entry['path'] = str(path)
                    if not path.is_dir():
                        entry['reason'] = 'Missing worktree; inspect registration before pruning.'
                    elif 'locked' in details:
                        entry['reason'] = 'Protected: worktree is locked.'
                    else:
                        status = git(path, 'status', '--porcelain', '--untracked-files=all')
                        entry['dirty'] = bool(status.strip())
                        try:
                            upstream = git(path, 'rev-parse', '--abbrev-ref', '@{upstream}').strip()
                            ahead = int(git(path, 'rev-list', '--count', '@{upstream}..HEAD').strip())
                        except RuntimeError:
                            upstream, ahead = None, None
                        entry.update(upstream=upstream, unpushed_commits=ahead)
                        entry['reason'] = ('Protected: uncommitted or untracked files.' if entry['dirty'] else
                                           'Protected: upstream missing or commits unpushed.' if upstream is None or ahead else
                                           'Clean relative to locally cached upstream; remote freshness and activity need verification.')
                        entry['last_commit_epoch'] = int(git(path, 'log', '-1', '--format=%ct').strip())
                        entry['old_commit'] = entry['last_commit_epoch'] < cutoff
                except (RuntimeError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
                    entry['error_type'] = type(error).__name__
                    errors.append(entry)
                worktrees.append(entry)
    return {'mode': 'report_only', 'proposed_inactive_days': stale_days, 'proposed_quarantine_days': 30,
            'policy_approved': False, 'sessions': sessions, 'worktrees': worktrees, 'errors': errors,
            'protections': ['Verified complete backup required', 'Never remove pinned, active or unknown-state sessions',
                            'Never remove dirty, untracked, locked or unpushed worktrees',
                            'Confirm current remote refs and all preserved branches before worktree removal',
                            'Use harness-native deletion; unknown formats remain report-only',
                            'No deletion until the user approves retention and protection rules']}
