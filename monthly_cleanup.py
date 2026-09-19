"""Read-only cleanup candidates. Age alone never authorizes deletion."""
import subprocess
import time
from pathlib import Path


def git(path, *args):
    result = subprocess.run(['git', '-C', str(path), *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Git inspection failed')
    return result.stdout


def candidate_report(manifest, home=None, stale_days=90):
    home = Path(home or Path.home())
    cutoff = time.time() - stale_days * 86400
    sessions = []
    for source in manifest['sources']:
        if source.get('source_mtime', time.time()) < cutoff:
            sessions.append({'harness': source['harness'], 'path': source['source_path'],
                             'bytes': source.get('source_bytes', 0), 'backup_status': source['status'],
                             'action': 'review_only',
                             'reason': 'Store mtime is old; per-session activity, pins and active processes still require verification.'})
    worktrees, seen = [], set()
    projects = home / 'Projects'
    # Discover registered worktrees through their main repositories, not by guessing
    # that an old directory can safely be removed.
    if projects.is_dir():
        for repository in sorted(projects.iterdir()):
            if not (repository / '.git').exists():
                continue
            try:
                blocks = git(repository, 'worktree', 'list', '--porcelain').strip().split('\n\n')
                for block in blocks[1:]:
                    details = dict(line.split(' ', 1) if ' ' in line else (line, True) for line in block.splitlines())
                    path = Path(details['worktree'])
                    if str(path) in seen:
                        continue
                    seen.add(str(path))
                    entry = {'path': str(path), 'repository': str(repository), 'action': 'review_only'}
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
                    worktrees.append(entry)
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
                worktrees.append({'repository': str(repository), 'action': 'review_only', 'error_type': type(error).__name__})
    return {'mode': 'report_only', 'proposed_inactive_days': stale_days, 'proposed_quarantine_days': 30,
            'policy_approved': False, 'sessions': sessions, 'worktrees': worktrees,
            'protections': ['Verified complete backup required', 'Never remove pinned, active or unknown-state sessions',
                            'Never remove dirty, untracked, locked or unpushed worktrees',
                            'Confirm current remote refs and all preserved branches before worktree removal',
                            'Use harness-native deletion; unknown formats remain report-only',
                            'No deletion until the user approves retention and protection rules']}
