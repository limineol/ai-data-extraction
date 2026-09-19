# Portable monthly backups

`extract_portable.py` is the recommended unattended runner in this fork. It uses
Python 3.9+ and the standard library. The original individual extractors remain
available for older GUI tools and training-data workflows.

```sh
python3 extract_portable.py --inventory
python3 extract_portable.py
python3 extract_portable.py --harness opencode --output /private/backups
```

Each run creates a separate UTC timestamp directory under
`~/.local/share/ai-data-extraction/backups/`, with owner-only permissions on Unix.
For every store it writes compressed raw records and normalized conversations,
then rereads each archive and records its SHA-256, record count and size in
`manifest.json`. SQLite reads use a read-only transaction, including committed
WAL data. SQLite still needs access to its shared-memory sidecar (or permission
to create it); a missing inaccessible sidecar fails visibly rather than ignoring
WAL transactions. Only named conversation tables are exported, never credential tables.
Conversation content itself can contain secrets; these are private backups, not
sanitized training datasets. Nothing is uploaded or deleted.

## Coverage

| Harness | Local storage | Support |
| --- | --- | --- |
| Codex | `$CODEX_HOME`, `~/.codex*`; active and archived rollouts | Response items, tool calls/results, legacy event messages; raw events retained |
| Claude Code | `$CLAUDE_CONFIG_DIR`, `~/.claude*` projects | Nested subagents and full message content |
| Grok Build | `~/.grok*/sessions/**/chat_history.jsonl` | Messages, reasoning, tools |
| Factory Droid | `~/.factory/sessions/**/*.jsonl` | Message envelopes and raw session events |
| Pi / Oh My Pi | `~/.pi/agent/sessions`, `~/.omp/agent/sessions` | Branch parents, subagents, tools, compaction records |
| Cursor CLI | `~/.cursor/chats/*/*/store.db` | Content-addressed transcript and raw blobs |
| OpenCode / OpenCode2 | XDG `opencode/opencode.db` | Both `session/message/part` and `session_v2/session_message` schemas |
| OpenCode / Slate legacy | XDG `storage/session`, `message`, `part` | JSON sessions with message parts |
| Gemini CLI | `~/.gemini/tmp/*/chats/session-*.json` | Messages with original thoughts, tool and token metadata |
| Devin | XDG `devin/cli{,-next}/sessions.db` | Session forest, branch parents and tool-call state |
| ForgeCode | `~/.forge/.forge.db` | Context messages and tools |
| Hermes | `$HERMES_HOME/state.db` | Sessions and messages, including inactive/compacted records |
| OpenClaw | `$OPENCLAW_STATE_DIR/agents/*/sessions/*.jsonl*` | Session events, including renamed deleted/reset transcripts; nested Codex homes |
| GitHub Copilot CLI | `~/.copilot/session-state/**/events.jsonl` | User/assistant/tool-completion events; raw events retained |
| Antigravity | `~/.gemini/antigravity/conversations` | **Archive only:** exact protobuf bytes; no claim of decoded messages |

Installed commands and stored history are discovered separately. `no_local_history`
means there is nothing available to verify on this host, not that an empty
extraction is proof of compatibility. Unknown database schemas fail. Failed output files retain an `.incomplete`
suffix and are listed in the error record; only verified archives receive their
final names. Empty discovery and previously present harness history disappearing
produce an attention status. Metadata-only stores are labeled `no_messages`, not
verified transcript extraction. Malformed
JSONL lines are retained as base64 and mark the store partial. JSONL reads are bounded to the initial file size; if the source changes, its
original prefix is hashed again. Appends are accepted only when that prefix is
unchanged. Rewrites, truncation and changes to other non-SQLite inputs are partial. Modern Codex's duplicated event messages are
not repeated in the normalized model transcript. Raw records retain all events.
Branching harnesses preserve all messages and parent IDs rather than pretending
that every branch is one linear conversation.

`--inventory` includes local paths; do not publish its output. Additional harness
home overrides supported by discovery: `GROK_HOME`, `PI_CODING_AGENT_DIR`,
`OMP_CODING_AGENT_DIR`, `XDG_DATA_HOME`, and `APPDATA`.

## Monthly scheduling (macOS / Linux)

Run and inspect an initial backup before installing a schedule. Use the actual
absolute Python and checkout paths; cron does not load your interactive shell.
For example, add this to `crontab -e`, preserving all existing jobs:

```cron
15 9 1 * * /absolute/python3 /absolute/ai-data-extraction/monthly_backup.py >> /absolute/private/backup.log 2>&1
```

This runs at 09:15 on the first day of each month in the host's local timezone.
Cron skips runs while the machine is off/asleep. For Linux without cron, use a
user systemd service with the same `ExecStart` and a timer:

```ini
[Timer]
OnCalendar=*-*-01 09:15:00
Persistent=true
[Install]
WantedBy=timers.target
```

A user timer needs a running user manager; enable user lingering if it should
run without an interactive login. `Persistent=true` catches up after downtime.
The Unix scheduler entry point uses an OS file lock to prevent overlapping scheduled
runs and writes `job-status.json`. Nonzero exit means attention is required. `completed_with_empty_stores` explicitly
reports metadata-only stores; this does not verify their transcript format.
Use `--allow-archive-only` only after explicitly accepting Antigravity's binary
archive limitation. It does not excuse corrupt data or parsing errors.

Backups are full snapshots. Old backups and failed runs remain available until
a separate retention policy is approved. Keep backups on a durable private
volume; a same-disk archive does not protect against disk loss. No auto-update
or remote code execution occurs in the monthly job.

## Cleanup proposal — approval required

Every run writes `cleanup-candidates.json`. The starting proposal is 90 days of
inactivity followed by 30 days of quarantine. This is a proposal, not an enabled
deletion policy. The report lists old session-store files and registered
worktrees found through direct repositories in `~/Projects`.

File mtime and last commit time are hints, not reliable activity evidence. Shared
SQLite stores need per-session checks. Worktree reports protect dirty, untracked,
locked and unpushed work, but cached upstream refs do not establish remote safety.
Active/pinned sessions, unknown state and undecoded formats must remain protected.
Harness-native deletion, fresh remote refs and a verified complete archive are
prerequisites for a future cleanup implementation. This version has no deletion
or quarantine code.

## Regular ChatGPT and Claude chats

These cloud histories are separate from Codex and Claude Code. Personal accounts
provide user-requested exports, not a documented cron export API. Request the
export in account settings and keep the downloaded archive in private storage.
ChatGPT exports can take up to seven days and their links expire after 24 hours.
No cloud chat extraction or deletion is performed by this runner.

- [ChatGPT export instructions](https://help.openai.com/en/articles/7260999)
- [Claude export instructions](https://support.claude.com/en/articles/9450526-export-your-claude-data)

## Validation

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic stores, including a WAL-mode OpenCode2 database. Real-host
verification must be reported separately from fixture coverage. Never commit
actual conversations, machine inventories or private cleanup reports.
