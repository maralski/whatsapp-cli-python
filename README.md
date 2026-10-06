# whatsapp-cli-python

A small Python CLI for private account linking, bounded local history, and guarded
WhatsApp text sends. Inspired by
[messages-cli-python](https://github.com/maralski/messages-cli-python).

**Version 0.3.0 has no wacli dependency.** It connects directly through
[Neonize 0.5.2](https://github.com/krypton-byte/neonize/releases/tag/0.5.2), a Python
binding to the native Whatsmeow protocol library. It works in the background;
WhatsApp Desktop and browser automation are unnecessary. Python **3.11+**, macOS
or Linux, arm64 or x86_64 are supported. This is Python code with a native Go
library dependency, rather than a pure Python implementation of the protocol.

**This is an unofficial personal-account client, not Meta's supported Business
API.** Review [WhatsApp's terms](https://www.whatsapp.com/legal/terms-of-service).
Protocol changes, restrictions, and session expiry remain possible. Use only
accounts and conversations you are authorized to access.

## Install

Keep `whatsapp_cli.py`, `whatsapp_backend.py`, and `whatsapp_history.py` together.
Install reviewed dependencies explicitly in a dedicated environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.lock
.venv/bin/python whatsapp_cli.py --version
.venv/bin/python -W error::ResourceWarning -m unittest -v
.venv/bin/python -I -B verify_protocol.py
```

The lock pins all Python dependencies and permitted distribution hashes. The
worker verifies every installed version and the platform-specific native library
against official 0.5.2 release digests before loading it. Missing or mismatched
libraries fail closed. Neonize's runtime downloader is replaced **before** its
eager import; this CLI never downloads or repairs dependencies at runtime.
`libmagic` and FFmpeg are unnecessary for this text-only interface: media access
is deliberately disabled. The offline smoke check loads the library and parses
synthetic events; it does not instantiate or connect an account client.

## Older messages and paginated history

`fetch` requests earlier message batches from your primary phone through the
existing linked device. It never sends a text to the selected person, marks their
chat read, pairs another device, or invokes wacli. The optional helper requires
Go 1.27.1 and a C compiler to build; ordinary pairing/sync/send retain the pinned
Neonize backend. Build explicitly, placing `history-runtime` **beside** the store:

```sh
# /private/whatsapp/account is your EXISTING store, not a new account.
.venv/bin/python build_history_backend.py --output /private/whatsapp/history-runtime

.venv/bin/python whatsapp_cli.py fetch --store /private/whatsapp/account \
  --chat +12025550123 --since 1970-01-01T00:00:00Z \
  --until 2026-10-06T23:59:59Z --count 50 --pages 5 --seconds 120 --execute

.venv/bin/python whatsapp_cli.py history-page --store /private/whatsapp/account \
  --chat +12025550123 --since 1970-01-01T00:00:00Z \
  --until 2026-10-06T23:59:59Z --limit 50 --execute

.venv/bin/python whatsapp_cli.py history-status --store /private/whatsapp/account \
  --chat +12025550123 --execute
```

Pass a returned `next_cursor` to the same `history-page` chat/date range to get
the next page. These new commands accept dates from 1970 onward without a 31-day
window. Each network invocation is bounded to 1–50 messages per batch, 1–5
batches, and a 1–120-second response budget, plus bounded connection time.
Deliberately repeat `fetch` to continue older batches. No automatic retry or
all-chat backfill exists. A batch may cross the `--since` stop boundary; that
selected chat's complete bounded batch is archived, while local pages apply
exact date filtering. `--until` filters displayed pages and explicit cache import;
phone fetching always proceeds backward from the oldest available local anchor.

`history.sqlite3` is a separate private archive. Explicit selected-chat sync now
retains message headers, including media anchors without media bodies, and
available plain text there. The original rolling `messages.sqlite3` cache and
`history --db` behavior remain available. Archive rows are deduplicated; received
revocations purge text and prevent replay restoration. This archive grows only
through explicit scoped sync/fetch/import operations and does not automatically
prune older fetched messages.

The phone protocol requires a **real known message ID and timestamp**. An empty
chat returns `no_local_anchor` without connecting. Run scoped `sync` while a new
message arrives, or explicitly add `--anchor-db /absolute/local/messages.sqlite3`
to import only this chat/date range from a compatible existing plaintext message
index. Such an index must have the documented `messages` columns, including
revoked/deleted/purged flags; no session tables, credentials or media keys are
imported. This can read a retained compatible cache without installing or running
the program that created it. Never invent an anchor or relink to work around this.

Responses expose `stop_reason` and `phone_reported_end`; `complete_history` always
remains false. Phone-offline timeouts, no progress, budget limits, unavailable
older history, and an explicit phone end marker are distinct. An end marker is
not proof of all-time access to deleted, expiring or unavailable messages. Late
protocol responses cannot always be correlated to an exact request; only the
selected verified phone/LID chat and messages no newer than its anchor are kept.
Runtime source and helper digests are verified; runtime builds/downloads are
disabled. See [third-party notices](THIRD_PARTY_NOTICES.md).

## Account lifecycle commands

Commands default to **dry-run**, including init, status, and pairing. Execution
requires `--execute`. Dry-run validates input without inspecting account files,
opening databases, importing Neonize, or starting a child process. Output omits
paths, phone numbers, QR data, and bodies.

Choose a direct absolute path under a trusted existing parent directory:

```sh
# Create a NEW private account store; existing paths are never overwritten.
.venv/bin/python whatsapp_cli.py init --store /absolute/path/to/cli-store --execute

# Run this YOURSELF in an interactive terminal, then scan the displayed QR
# from WhatsApp on your phone: Settings > Linked devices > Link a device.
.venv/bin/python whatsapp_cli.py pair --store /absolute/path/to/cli-store --execute

# Local session status only; this does not connect or prove online health.
.venv/bin/python whatsapp_cli.py status --store /absolute/path/to/cli-store --execute
```

Pairing is explicit and requires a controlling terminal. Pairing failures now
distinguish `connect_failed`, `qr_render_failed`, `pair_timeout`,
`connection_ended`, and `pair_rejected`. Connection categories expose only fixed
labels such as DNS, TLS, websocket, timeout, or database failure; raw errors and
addresses stay private. Check local status before a deliberate repeat. QR credentials are
written only to a verified handle to the concrete controlling terminal, selected
by the parent and explicitly inherited by the isolated worker; never captured
stdout/stderr/result logs. Version 0.2.3 fixes macOS writes through the session-relative
`/dev/tty` alias after worker isolation. Ordinary commands
require one existing session, select that exact device, and refuse unexpected QR
or account changes. Pairing an additional account into an already linked store
is forbidden. Revoke an unwanted device through WhatsApp's Linked devices UI.

**Upgrade from 0.1.x:** existing account stores are not imported or altered.
Create and explicitly link a separate store, validate it, then switch your caller.
A new linked device is persistent account access and needs an intentional user
scan. Avoid replacing a working booking sender until that step is complete.
There is no automatic fallback to the earlier transport.

Stores are created as `0700`; account marker, session database, index, lock, and
sidecars must be owned by the current user and private (`0600`). Unsafe symlinks,
hard links, special files, writable ancestors, and URI query/fragment characters
in store paths are rejected. Permissions are never silently repaired. On macOS,
`/tmp` and `/var` are symlinks: choose their direct paths when appropriate.

## Send text

The body comes from UTF-8 stdin: nonblank, without NUL, at most **10,000
characters / 40,000 bytes**. The example number is fictional.

```sh
# Preview only; add --execute for an intentional send.
printf '%s' 'Example message' | .venv/bin/python whatsapp_cli.py send \
  --store /absolute/path/to/cli-store --to '+12025550123'
```

Both the parent and worker validate exact international phone numbers or phone
JIDs. Names, groups, LIDs, broadcasts, attachments, replies, and batches are not
supported. Self-sends require explicit `--allow-self`; booking callers should
never add it. Text is sent as a literal conversation protobuf: no automatic
mentions or URL previews. The message and recipient travel to the worker over
stdin, **never through process arguments**, shell code, or an intermediate file.
Avoid putting real bodies in interactive shell history; use a trusted caller.

```python
import subprocess

result = subprocess.run(
    ["/absolute/path/to/.venv/bin/python", "/absolute/path/to/whatsapp_cli.py",
     "send", "--store", "/absolute/path/to/cli-store",
     "--to", "+12025550123", "--execute"],
    input=notice_text.encode("utf-8"), capture_output=True, timeout=65,
)
```

Success contains `status: "accepted"`, `message_id`, and
`delivery_confirmed: false`. This is protocol acceptance, not a delivery/read
receipt. `local_store_warning: true` means acceptance succeeded but indexing
failed; do not resend to fix the cache. There is one send invocation and no CLI
retry. The protocol library may reconnect or retry internally. A timeout,
interruption, native exception, or invalid result after possible dispatch leaves
the outcome unknown; inspect the actual chat before any manual repeat.

The account store has an exclusive nonblocking lock. Pair, sync, and send cannot
run concurrently against it. Sends have a 50-second worker deadline, connection
waits 20 seconds, pairing at most 150 seconds including cleanup. The worker has
bounded input/result pipes and a minimal environment. Native and Python raw logs
are suppressed at the file descriptor level. Process-group cleanup bounds native
blocking calls. This does not impose a native-memory or filesystem-I/O quota.

For booking notices, retain a durable caller ledger: record the authoritative
booking/event key **before dispatch**, block repeats of pending/uncertain
attempts, and mark acceptance afterward. This CLI does not replace that ledger
or promise exactly-once delivery.

## Sync and read one chat

```sh
# Receive available selected-chat text events for a bounded interval.
.venv/bin/python whatsapp_cli.py sync --store /absolute/path/to/cli-store \
  --chat '+12025550123' --seconds 30 --limit 200 --execute

# Read the local index; no connection or sync occurs during history reads.
.venv/bin/python whatsapp_cli.py history \
  --db /absolute/path/to/cli-store/messages.sqlite3 --chat '+12025550123' \
  --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z --limit 20 --execute
```

Sync accepts 1–120 seconds and 1–200 newly cached rows. It handles selected-chat
live and available history-sync events, without requesting an exhaustive history
export. Exact phone matches and uniquely mapped privacy IDs (LIDs) are indexed
under the selected phone. LID mappings come only from the owned account's local
`whatsmeow_lid_map`, with a unique forward and reverse match; names are never
used to infer identity. Missing, conflicting, generated, or view-based mappings
do not expand the chat scope. Sync does not enumerate contacts or chats,
send read receipts deliberately, or expose session keys. The protocol client can
still receive account-wide events, metadata, and protocol history automatically;
selection limits the CLI's text index, not all data received by the native client.
An empty cache does not mean the person sent no messages. This pinned binding
does not expose on-demand phone history retrieval; earlier messages may remain
unavailable even after a successful sync. No automatic monitoring is started.

Sandboxed runners may need approved execution access to update this private
store, even when receiving messages. `store_access_denied` means the account
lock was denied before dispatch. Use the runner's normal approval mechanism;
the CLI does not loosen filesystem permissions or grant permanent OS access.

The cache stores plain text from the selected chat, skipping media, edits,
view-once, and disappearing payloads. Available revocation events purge indexed
text and retain a tombstone. Replay cannot restore those rows. Cache pruning at
writes retains a 31-day window and at most 10,000 rows; pruning is not a background
erasure service. Revocations/deletions missed while offline and backup copies can
remain. Cache status is never presented as complete server history.

History uses read-only/query-only SQLite, bound parameters, restricted access to
an ordinary messages table, and query deadlines. Dates require explicit timezones;
start is inclusive, end exclusive. The window is at most **31 days**, results
**200 rows**, text **10,000 characters**. JSON Lines are newest first and escape
terminal controls. Revoked, deleted, and purged rows are excluded. Reading live
WAL databases may create/use coordination sidecars. No zero-write guarantee is
made for SQLite coordination or the OS.

## Privacy and security

The native backend has access to the **whole linked account** and persists
cryptographic session material. The session database and text cache are not
encrypted by this CLI; protect disk, backups, device access, and local user
account. History stdout intentionally contains requested text. No account keys,
QR data, raw logs, account databases, or private message output belong in GitHub.

Checks reduce accidental scope expansion and unsafe paths; they do not isolate
root or a compromised same-user process, prevent all path races, or establish
that upstream native code is vulnerability-free. Release hashes establish byte
integrity relative to published artifacts, not an independent security audit.

See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) and [SECURITY.md](SECURITY.md).
The CLI is MIT licensed, with retained messages-cli-python attribution. Neonize
is a separately installed Apache-2.0 dependency; its native code/dependency
licenses remain upstream. No native binaries or account data are bundled here.
