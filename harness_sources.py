"""Discover conversation stores without scanning credentials or project files."""
import os
import fnmatch
import shutil
from dataclasses import dataclass
from pathlib import Path


COMMANDS = {
    'codex': ('codex',), 'claude': ('claude',), 'grok': ('grok',),
    'droid': ('droid',), 'pi': ('pi',), 'omp': ('omp',),
    'cursor-cli': ('cursor-agent', 'agent'), 'opencode': ('opencode', 'opencode2'),
    'gemini': ('gemini',), 'devin': ('devin',), 'forgecode': ('forge',),
    'hermes': ('hermes',), 'slate': ('slate',), 'antigravity': ('agy',),
    'copilot': ('copilot',), 'openclaw': ('openclaw',),
}


@dataclass(frozen=True)
class Source:
    harness: str
    path: Path
    format: str


def discover(home=None, environ=None):
    home = Path(home or Path.home())
    env = os.environ if environ is None else environ
    data = Path(env.get('XDG_DATA_HOME') or home / '.local/share')
    found = {}

    def add(name, root, pattern, fmt):
        root = Path(root)
        if root.is_dir():
            paths = []
            if pattern.startswith('**/'):
                visited = set()
                for directory, dirs, files in os.walk(root, followlinks=True):
                    resolved = Path(directory).resolve()
                    if resolved in visited:
                        dirs[:] = []
                        continue
                    visited.add(resolved)
                    paths.extend(Path(directory) / name for name in fnmatch.filter(files, pattern[3:]))
            else:
                paths = root.glob(pattern)
            for path in sorted(paths):
                if path.is_file():
                    key = str(path.resolve())
                    found.setdefault(key, Source(name, path.resolve(), fmt))

    for name, prefix, variable, subdirs in [
        ('codex', '.codex', 'CODEX_HOME', ('sessions', 'archived_sessions', 'projects')),
        ('claude', '.claude', 'CLAUDE_CONFIG_DIR', ('projects',)),
        ('grok', '.grok', 'GROK_HOME', ('sessions',)),
    ]:
        roots = [home / prefix, *sorted(home.glob(prefix + '-*'))]
        if env.get(variable):
            roots.append(Path(env[variable]))
        if name == 'codex':
            roots.extend(home.glob('.openclaw/agents/*/agent/codex-home'))
        for root in roots:
            for subdir in subdirs:
                add(name, root / subdir, '**/chat_history.jsonl' if name == 'grok' else '**/*.jsonl', 'jsonl')
    for name, root, pattern in [
        ('droid', home / '.factory/sessions', '**/*.jsonl'),
        ('pi', Path(env.get('PI_CODING_AGENT_DIR') or home / '.pi/agent') / 'sessions', '**/*.jsonl'),
        ('omp', Path(env.get('OMP_CODING_AGENT_DIR') or home / '.omp/agent') / 'sessions', '**/*.jsonl'),
        ('openclaw', Path(env.get('OPENCLAW_STATE_DIR') or home / '.openclaw') / 'agents', '*/sessions/*.jsonl'),
        ('openclaw', Path(env.get('OPENCLAW_STATE_DIR') or home / '.openclaw') / 'agents', '*/sessions/*.jsonl.deleted.*'),
        ('openclaw', Path(env.get('OPENCLAW_STATE_DIR') or home / '.openclaw') / 'agents', '*/sessions/*.jsonl.reset.*'),
        ('copilot', home / '.copilot/session-state', '**/events.jsonl'),
    ]:
        add(name, root, pattern, 'jsonl')
    add('cursor-cli', home / '.cursor/chats', '*/*/store.db', 'sqlite')
    add('gemini', home / '.gemini/tmp', '*/chats/session-*.json', 'json')
    for root in [data / 'opencode', home / 'Library/Application Support/opencode',
                 Path(env.get('APPDATA') or home / 'AppData/Roaming') / 'opencode']:
        add('opencode', root, 'opencode.db', 'sqlite')
        # Legacy JSON storage can coexist with newer sessions in SQLite.
        add('opencode', root / 'storage/session', '**/*.json', 'opencode-json')
    for dirname in ['cli', 'cli-next']:
        add('devin', data / 'devin' / dirname, 'sessions.db', 'sqlite')
    add('forgecode', home / '.forge', '.forge.db', 'sqlite')
    add('hermes', Path(env.get('HERMES_HOME') or home / '.hermes'), 'state.db', 'sqlite')
    add('slate', data / 'slate/storage/session', '**/*.json', 'opencode-json')
    add('antigravity', home / '.gemini/antigravity/conversations', '*.db', 'sqlite')
    add('antigravity', home / '.gemini/antigravity/conversations', '*.pb', 'opaque')
    return sorted(found.values(), key=lambda s: (s.harness, str(s.path)))


def installed():
    home = Path.home()
    bins = [home / '.local/bin', home / '.bun/bin', home / '.opencode/bin',
            home / '.local/share/mise/shims', Path('/opt/homebrew/bin'), Path('/usr/local/bin')]
    bins.extend(sorted((home / '.local/share/mise/installs/node').glob('*/bin')))
    result = {}
    for name, commands in COMMANDS.items():
        paths = set()
        for command in commands:
            executable = shutil.which(command)
            if executable:
                paths.add(executable)
            for directory in bins:
                candidate = directory / command
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    paths.add(str(candidate))
        result[name] = sorted(paths)
    return result
