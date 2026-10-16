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
| Codex | `$CODEX_HOME`, `~/.codex*`; active and archived rollouts, `state_5.sqlite` + `thread_history_1.sqlite` | Paginated turns/items/realtime history plus response items, tool calls/results, legacy event messages; raw events retained |
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

## Centralized monthly scheduling from Nexus

Nexus is the local coordinator. Vector and Forge need SSH access, Python 3.9+,
and this checkout at `~/Projects/ai-data-extraction`. OpenSSH scp transfers the
completed snapshots; it never copies live SQLite databases. No remote schedules
are needed, and no files are uploaded to cloud storage.

```sh
python3 backup_fleet.py --host vector --host forge --allow-archive-only
```

Each host runs its local extractor, then Nexus collects the results under
`~/.local/share/ai-data-extraction/monthly/<UTC timestamp>/{nexus,vector,forge}/`.
Nexus hard-links its own immutable archives where possible to avoid duplicate
storage, and copies them if hard links are unavailable. Remote archives are
copied with SCP. Every collected archive is checked against its manifest's
SHA-256 and byte count. The aggregate `backup-set.json` records per-host coverage
and verification. A failed host does not prevent collecting the other hosts.
A set with failed extraction or transfer is never marked complete.

The `nexus` label denotes the local machine. Remote names must be simple SSH
aliases (letters, numbers, dots, underscores or hyphens), and remote backup paths
must contain only letters, numbers, underscores, dots, slashes or hyphens.
Interactive authentication is disabled; existing SSH configuration and known
host verification apply. No host-key bypass or credentials are stored.

To collect already completed initial snapshots without extracting again:

```sh
python3 backup_fleet.py --host vector --host forge --allow-archive-only --collect-latest
```

Existing snapshots must be no more than 24 hours old; a clock more than 30
minutes ahead is rejected. The report explicitly records that snapshots were
reused and their original timestamps. Do not put `--collect-latest` in the monthly schedule.

Install **one** cron entry on Nexus, with absolute paths to Python and the checkout:

```cron
15 9 1 * * /absolute/python3 /absolute/ai-data-extraction/backup_fleet.py --host vector --host forge --allow-archive-only >> /absolute/private/backup.log 2>&1
```

This runs at 09:15 on the first of each month in Nexus's local timezone. Nexus
must be awake at that time; cron does not catch up after downtime. Vector and
Forge must be reachable over SSH. OS file locks prevent overlapping fleet jobs
and overlapping per-host extraction. The collector requires manifest version 2, rejecting older extractors that cannot
see paginated Codex history. No auto-update or remote code deployment
occurs in the scheduled job. Deploy code updates deliberately to all hosts.

`--allow-archive-only` accepts the explicit Antigravity protobuf limitation;
it does not excuse malformed records or failed transfers. Empty history is not
live compatibility verification. Metadata-only stores are labeled `no_messages`.
The set can be `completed_with_cleanup_warnings` when archives verify but Git
metadata inspection needs attention. These warnings never enable deletion.

Each host retains its extraction snapshot. The central set is a private folder
ready for a later Proton Drive upload step. Upload is not implemented in this
version. Future upload must check `backup-set.json` and refuse incomplete sets.
Full snapshots and intermediate failed runs have no automatic retention policy.
Same-disk local archives do not protect against disk loss.

## Cleanup proposal — approval required

Every run writes `cleanup-candidates.json`. The default report threshold is 60 days of
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

## Codex paginated history and quarantine planning

The extractor reads committed WAL data from both Codex databases in read-only
transactions. Raw archives preserve thread metadata, spawn edges, dynamic tools,
turns, items, projection state, and realtime events. Normalized conversations
retain complete item payloads, ordering, turn IDs, and a per-thread history digest.
Surviving migrated JSONL files are still archived as raw records, without
duplicating their normalized conversations. Missing or
unknown database versions fail visibly. Orphan history rows are preserved and
protected from cleanup. Use a current SQLite runtime if a host cannot open a WAL
store read-only; the fleet runner prefers Homebrew Python on Macs when available.

To create a **read-only** quarantine plan from an explicit backup:

```sh
python3 codex_cleanup.py --backup /private/backups/RUN --output /private/plan.json
```

The planner verifies both archive files and compares current per-thread history
against the backed-up digests. It requires at least 60 days of inactivity, known
pin/archive flags, completed turns, and backed-up history for every affected
descendant, including archived descendants that native archive may stop. Unknown state, missing graph members, cycles, and protected
children prevent a parent from being selected. `--protected-ids` accepts a JSON
list of additional pinned or active IDs from the app.

A plan is not an archive command or an authorization token. Before acting, confirm
the off-device copy, check every `runtime_check` chat's current runtime and pin state (including archived
descendants), and recompute the plan. Check each root's live runtime subtree and
abort if it contains IDs outside `runtime_check`. Resolve `unmatched_protected_ids`
before acting; do not assume an unknown protected ID belongs to another host. Codex's native archive operation also archives descendants.
Afterward, verify history digests and archive flags and record a recovery ledger
with a minimum 30-day quarantine. Separate metadata and history WAL transactions
are not an atomic cross-database snapshot; fresh checks are required. This runner
never archives, edits live databases, or permanently deletes history. Legacy
JSONL cleanup and other harnesses remain report-only.

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
