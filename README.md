# whatsapp-cli-python

A small Python CLI for **bounded local WhatsApp history reads** and **guarded
text sends**, with a familiar interface inspired by
[messages-cli-python](https://github.com/maralski/messages-cli-python).
Python 3.9+ and the standard library only; macOS and Linux.
Copy the single `whatsapp_cli.py` file to use it elsewhere.

History reads an explicitly selected [wacli](https://github.com/openclaw/wacli)
message index. Sending requires a separately installed, trusted **wacli 0.20.0**
executable and an already linked account. This wrapper does not install wacli,
pair devices, launch WhatsApp Desktop, run a server, or change OS permissions.

**This uses an unofficial WhatsApp Web client, not Meta's supported Business
API.** Protocol changes, account restrictions, and session expiry are possible.
Review [WhatsApp's terms](https://www.whatsapp.com/legal/terms-of-service) and
[wacli's documentation](https://github.com/openclaw/wacli/tree/v0.20.0/docs).
Use only accounts, conversations, and recipients you are authorized to access.

## Run

```sh
python3 whatsapp_cli.py --version
python3 whatsapp_cli.py --help
python3 -m unittest -v
```

Both commands default to **dry-run**. Add `--execute` only for an intentional
read or send. A dry-run validates arguments and, for sends, reads bounded stdin;
it does not inspect any account files or binaries, open a database, or launch
a subprocess. Its JSON output omits recipients, paths, and text.

Tests use synthetic SQLite fixtures, a fake backend, and local Python subprocesses.
They do not open real WhatsApp data, pair accounts, or send messages.

## Read one chat

Supply the absolute path to your already synced `wacli.db` and a known exact
international phone number or phone JID. This CLI does not enumerate chats or
resolve names. The example number below is fictional.

```sh
# Preview only. Replace the example path and scope when you intend to read.
python3 whatsapp_cli.py history \
  --db /absolute/path/to/private-store/wacli.db \
  --chat '+12025550123' \
  --since 2026-01-01T00:00:00Z --until 2026-01-02T00:00:00Z --limit 20
```

Add `--execute` to read. Dates require an explicit timezone and must be from
1970 onward. Start is inclusive; end is exclusive. Windows are limited to
**31 days**, results to **200 rows**, and each text to **10,000 characters**.
Output is JSON Lines, newest first, with message ID, UTC timestamp, direction,
text, and availability/truncation flags. Unicode and terminal controls are
escaped in JSON; normal JSON parsers restore them.

The query uses SQLite `mode=ro`, `query_only`, bound parameters, a restricted
authorizer, and a progress budget/deadline. Only the `messages` table's selected
fields are read; views, virtual tables, and generated required columns are rejected.
Revoked, locally deleted, and purged rows are excluded according to the index's
current flags. Null text remains null. Media, rich messages, and captions stored
outside the `text` column are not decoded. No contacts, media keys, filenames,
or session credentials are returned.

This is **cached history**, potentially incomplete or stale. Reading does not
connect to WhatsApp, sync, fetch older messages, or mark anything read. Modern
wacli 0.20.0 schema is required; incompatible data fails with a redacted error.
Queries have a three-second progress deadline, an instruction budget, and a
one-second lock wait. These cannot bound a stalled filesystem or OS. On Python
3.11+, SQLite's per-value length limit is also reduced to 1 MiB; oversized
records may fail instead of truncating. Keep your OS/Python SQLite up to date.

The index contents are never written. SQLite may use or create WAL coordination
sidecars (`-shm`) when reading a live WAL database; this is not a guarantee of
zero filesystem writes. Existing sidecars are checked for unsafe paths first.

## Send one text

The body comes from UTF-8 stdin: **1–10,000 characters**, at most **40,000
bytes**, nonblank and without NUL. It is passed unchanged as one backend argument,
never through a shell or as executable code. Newlines and Unicode are preserved.

```sh
# Preview only. The number and paths are examples.
printf '%s' 'Example message' | python3 whatsapp_cli.py send \
  --to '+12025550123' --store /absolute/path/to/private-store
```

For execution, additionally supply:

- `--wacli /absolute/direct/path/to/wacli`
- `--wacli-sha256 TRUSTED_EXECUTABLE_SHA256`
- `--execute`

Obtain wacli from its [official release](https://github.com/openclaw/wacli/releases/tag/v0.20.0),
verify the archive against the trusted published checksum and applicable signing
information, then pin the extracted executable's SHA-256. The archive checksum
and executable checksum are different. Hashing an untrusted binary does not
establish trust; no checksum is fetched or automatically accepted by this CLI.
After a deliberate trusted upgrade, review compatibility and update the pin yourself.

An integration can call the script without a shell:

```python
import subprocess

result = subprocess.run(
    ["python3", "whatsapp_cli.py", "send",
     "--to", "+12025550123",
     "--store", "/absolute/path/to/private-store",
     "--wacli", "/absolute/direct/path/to/wacli",
     "--wacli-sha256", trusted_executable_sha256,
     "--execute"],
    input=notice_text.encode("utf-8"), capture_output=True, timeout=65,
)
```

Replace all examples before any intentional execution. Avoid putting real
message text in interactive shell history; use a trusted input tool or caller.

The store must already exist, belong to the current user, and have no group/other
permissions (normally `0700`). Its existing `session.db`, `wacli.db`, sidecars,
and known backend markers must also be private (normally `0600`). The binary
and all paths must be direct, without symlinks, parent traversal, unsafe owners,
or writable ancestors. Regular files with multiple hard links and special files
are rejected. On macOS `/tmp` and `/var`, and many Homebrew executable paths,
are symlinks: select the direct path yourself after checking its target.
Preflight failures do not repair permissions, create an account, or initiate pairing.
If `.send.sock` exists, the wrapper refuses execution; stop its owning sync process
through normal controls. It does not remove locks or sockets.

Only exact phone numbers/JIDs are accepted. Groups, contact names, hidden-user
LIDs, broadcasts, attachments, mentions, replies, and batches are unsupported.
Self-sends remain blocked by default. An intentional test to your own linked
account can use `--allow-self` together with `--execute`; the backend warns that
self delivery is not guaranteed. Booking integrations should never add this flag.
Every send includes **`--no-preview`**, preventing automatic URL-preview fetches
by the backend's reviewed text-send path.

Success is JSON with `status: "accepted"`, `message_id`, and
`delivery_confirmed: false`. It means protocol acceptance, **not delivery or
read confirmation**. `local_store_warning: true` means the backend reported an
index write problem after acceptance; its private warning text is suppressed.

The wrapper invokes the backend once, with a 40-second backend timeout, a
50-second child deadline, and 64 KiB output caps per stream. It never automatically
retries. The backend itself can reconnect or perform protocol retries.
After launch, any backend error, timeout, interruption, malformed output, or
unexpected recipient is `send_unknown`: inspect the actual chat before deliberately
repeating. A recipient mismatch is detected **after** possible dispatch; it cannot
undo a send. `send_not_started` means the subprocess could not be launched.
Argument/preflight errors mean this wrapper did not launch the backend.
Exit code is 0 for a valid dry-run, history output, or acceptance; 1 for a redacted error.

There is no cross-invocation ledger or deduplication. For booking integrations,
keep durable caller-side state keyed by the authoritative booking ID, record an
attempt before dispatch, and reconcile uncertain outcomes instead of retrying.

## Privacy and security

**wacli 0.20.0 accepts text only through `--message`.** Therefore the body and
recipient can be visible in the child process arguments to local monitoring
software or another process running as the same user. stdin at the Python entry
point does not remove that backend limitation. This version is unsuitable if
your requirements prohibit that exposure.

The wrapper does not persist bodies, sessions, telemetry, logs, or media and has
no network client. Executed sends let wacli access the linked account, connect to
WhatsApp, update its credential/index files, and retain sent text in its index.
Those stores are sensitive: protect your user account, disk, backups, and linked
devices. File modes do not encrypt databases. History stdout intentionally
contains private message text; terminals and callers control its retention.
Never commit captured output or account files.

The child has a minimal environment: no inherited proxy settings, WACLI account
overrides, or dynamic-loader variables. New child-created files use a private
umask. Raw backend stdout/stderr are never echoed; accepted IDs and a boolean
store warning are the only selected success fields.

Checks protect against accidental scope expansion and unsafe pre-existing paths.
They do not isolate a compromised same-user process or remove all path races
between validation and execution. A digest pins bytes, not the entire OS,
linked account, dynamic libraries, or upstream dependency chain.

See [SECURITY_REVIEW.md](SECURITY_REVIEW.md) for the pre-publication review,
test evidence, and residual risks, and [SECURITY.md](SECURITY.md) for reporting.

MIT license. Interface and redacted-validation patterns are adapted from
messages-cli-python; wacli is a separate MIT-licensed dependency, not bundled here.
