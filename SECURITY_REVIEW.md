# Pre-publication security review — 0.4.0

## Scoped refresh adapted from wacli

Reviewed 6 October 2026. The existing helper adds `sync --refresh` through the
same exact Whatsmeow revision and Go 1.27.1; Python/native/module lock versions
remain unchanged. The source fingerprint now also covers `refresh.go`.

wacli 0.20.0's presence, offline replay, history notification and primary-phone
recovery behavior was inspected at a4f23eef7395473931e3a44c93eacd6ebebdc313.
This independent implementation imports/invokes no wacli code or executable.
It uses an existing single account identity and nonblocking account lock.
After the helper returns, the parent reacquires that lock, rechecks identity,
validates the complete row batch, and writes only the selected chat's two indexes.
Rolling-cache schemas with views, triggers or generated columns fail closed.
Revocation tombstones prevent restoration by a subsequent replay.

Available/online presence is a deliberate transient account-visible side effect,
matching wacli normal sync. A serialized unavailable/offline cleanup runs before
the connection context is cancelled, including errors. Presence operations are
bounded to three seconds. Raw presence errors are never output. Failure or forced
process termination can prevent final presence; no privacy settings are changed.

Offline replay completion describes the device backlog offered by WhatsApp, not
the full chat timeline. Result booleans keep recent coverage and complete history
unverified. Counters use fixed names, bounded integers and booleans, not chat names,
message secrets, account-wide counts or raw errors. Dry runs load/read nothing.

Manual history processing bounds each compressed/decompressed blob to 16 MiB,
four downloads per invocation, newest selected headers to 200, and returned text
to 512 KiB beneath the 4 MiB IPC cap. Conversation ID and message key must both
match the verified phone/LID aliases. Plain text ignores protocol metadata values;
media, expiring, edit and view-once bodies remain excluded. No history media keys
or blobs are returned or written to the application archive.

Eligible selected-chat decryption failures use real received IDs and sender/chat
metadata to request the primary phone's copy after five seconds. At most 20 IDs
are requested once, each with a three-second deadline; recovery is cancelled when
a readable event arrives or the receive window ends. Cancellation survives bounded
row eviction. Hidden/view-once failures and duplicate unavailable-envelope requests
are excluded. Automatic delayed account-wide phone rerequests/reconnect remain
disabled. Upstream Whatsmeow still processes account-wide acknowledgments, sender
retry receipts and its own immediate unavailable-envelope phone requests; selected
storage is not an account-wide network isolation boundary.

Evidence: **109 offline Python tests** pass on macOS Python 3.11 and 3.14 (the
uninstalled Segno check skips on the latter), **14 Go tests** with race detection,
Go vet, native protobuf smoke, Ruff F/E9, and nine synthetic booking-adapter tests
pass. Synthetic PTY tests require approved execution outside the filesystem sandbox;
they use no real QR or account. Bandit reports only four reviewed LOW subprocess
findings in the existing launcher/explicit builder, no medium/high findings.
Secret scanning and manual review found only fictional fixtures and public source,
native/module integrity hashes. Account data, private outputs and compiled helpers
are excluded from publication. Govulncheck 1.8.0 reports zero reachable and zero
imported-package vulnerabilities; GO-2026-5932 affects the unimported OpenPGP
package in a required module. This is a maintainer review, not an independent audit.

There is no general verified request for all messages newer than a date. Messages
already acknowledged or unavailable on the primary phone may remain absent. No
human sends, new devices, credential extraction or security-setting changes are
required to implement or test this behavior; booking dispatch/ledger logic is
unchanged.

## Historical 0.3.2 review

## Live-event wire-format correction

Neonize 0.5.2's pinned upstream EncodeMessageInfo serializes live timestamps with
UnixMilli, while the WebMessageInfo history timestamp is seconds. Earlier synthetic
live fixtures incorrectly used seconds, masking rejection of real live timestamps
by the seconds-based cache/archive validation. Version 0.3.2 converts only the live
event timestamp at its ABI boundary. History and send/cache timestamps are unchanged.

Normal messageContextInfo metadata may accompany plain text. Both Python and the
optional history helper ignore that field's values while permitting otherwise
literal conversation/extended text. Metadata is not extracted, stored or returned;
wrappers, media, mixed content, edits and expiring content retain their exclusions.
Seven new offline Python regressions and one Go regression cover wire units,
metadata non-access, privacy boundaries, exact chat scope and bounded diagnostics.
Real protobuf smoke fixtures now use milliseconds for live events. Sync adds only
bounded selected-chat event/header counters, not unrelated chat data or raw errors.

This fix does not certify that older missed/acknowledged messages will replay.
Fresh sync remains mandatory before latest-message requests, with explicitly
unverified coverage if the known newer message is not retrieved. There is no
wacli runtime use, relinking, automatic retry, security-setting change or send.
Dependency locks and the official Neonize native digests remain unchanged;
the optional helper is rebuilt from its reviewed source with pinned Go 1.27.1.

## Historical 0.3.1 review

## Subprocess cleanup correction

The macOS/Python 3.14 CI run for documentation commit 0a925a2 exposed an existing
cleanup bug: after the synthetic output-cap test raised its intended safe error,
a denied process-group SIGKILL raised PermissionError from the finally block.
That error replaced the redacted result and bypassed pipe closure. The log proves
the cleanup denial, not the precise kernel timing or a WhatsApp account failure.

Version 0.3.1 handles OS errors from group cleanup, falls back only to the owned
Popen child, waits at most two seconds for cleanup, and closes all pipes even if
waiting fails. An incomplete cleanup always returns send_unknown; it never
silently claims success or retries. Group termination remains the first attempt,
preserving cleanup when descendants retain pipes after the leader exits. If the
OS denies group termination, descendant termination cannot be guaranteed.

Four new synthetic regressions cover output overflow, live-child timeout, success
with denied group cleanup, and a wait failure. The full **94-test** suite passes
on macOS with Python 3.11 and 3.14; native protobuf smoke and Ruff F/E9 pass.
Bandit still reports only the two reviewed low-severity subprocess findings in
the CLI. History code, fresh-sync-first policy, dependency locks, native/helper
digests, linked account and booking semantics are unchanged. No live WhatsApp
request or message is needed to test this fix.

## Historical 0.3.0 review

Reviewed 6 October 2026. This extends the maintainer review below; it is not an
independent audit or a claim of unrestricted account-history availability.

The new runtime is `whatsapp_history.py` plus a small optional Go helper. The
helper uses the same exact Whatsmeow revision as Neonize 0.5.2, SQLite, protobuf,
an existing single device and the same nonblocking account lock. It never calls
pairing, NewDevice, mark-read, contact enumeration, human text send, or wacli APIs.
The only message dispatch is Whatsmeow's `SendPeerMessage` carrying a history
protocol request to the linked account's own primary phone. Automatic reconnect,
message rerequest and retry are disabled in this helper.

Evidence: **90 offline Python tests**, **5 Go tests**, Go race detection and vet,
native/protobuf smoke checks, and **9 synthetic booking-adapter tests** pass.
Ruff F/E9 passes. Bandit 1.9.4 reports four low severity subprocess findings
(two in the existing bounded launcher and two in the explicit build script),
with no medium/high results. Argument lists use no shell; the builder's Go path
is an explicit local developer choice. Secret scanning finds only fictional
fixtures, public dependency/native/source digests, and no credentials.

The initial Go 1.26.0 build was rejected during review: govulncheck found sixteen
reachable standard-library advisories. The reviewed helper is built with pinned
**Go 1.27.1**, verified through Go's toolchain checksum mechanism. Govulncheck
1.8.0 then reports **zero reachable and zero imported-package vulnerabilities**.
It reports GO-2026-5932 in the required x/crypto module's OpenPGP package, which
this helper does not import or call. This result covers the new helper, not an
independent audit of the unchanged opaque Neonize release library. The Python
dependency lock and official native digests remain unchanged.

The installed macOS arm64 Neonize artifact's Go build metadata identifies
Go 1.26.8 and the expected upstream revision; it is not the vulnerable initial
1.26.0 helper build. This metadata check is not a full native-library audit.

History boundaries: explicit one-chat operations only, uniquely verified PN/LID
aliases, both conversation ID and message key filtering, and a real known anchor.
Request count is 1–50, pages 1–5, response time 1–120 seconds with bounded
connection allowance, output at most 4 MiB per pipe. Compressed/decompressed
history blobs are capped at 16 MiB. Automatic account-wide history downloads are
disabled. Only on-demand notifications are downloaded; unrelated conversations,
keys, media bodies and contact metadata are not copied to the archive. Expiring,
view-once, edited and media wrappers are never unpacked for text.

The private archive validates ordinary non-generated columns, rejects triggers,
views and unsafe files, uses parameterized queries, query-only local readers,
trusted_schema OFF, SQLite length/time budgets, and stable rowid-bound cursors.
Revocation tombstones purge text and prevent replay resurrection. Phone end,
inaccessible history, empty anchors, timeout and no progress are distinct;
complete_history is always false. A late response cannot be conclusively tied to
one request, so chat/anchor filtering and explicit incompleteness remain necessary.
An explicit compatible cache import accesses only this chat/date range's message
IDs, timestamps, direction, text and deletion flags, not session/key/media columns.

Source-module digest and private manifest/binary digest/platform/compiler checks
precede optional helper execution. Runtime build/download paths do not exist.
These checks do not defend against compromise of the owning user. Upstream native
client memory/metadata behavior and WhatsApp account restrictions remain risks.
The fixed-recipient booking adapter, interpreter pin and durable pending/sent
ledger are preserved during installation; no sends are needed for validation.

## Historical 0.2.4 review

Reviewed 6 October 2026. Maintainer review plus automated checks; not an
independent audit or certification.

## Scope and evidence

All runtime code (`whatsapp_cli.py`, `whatsapp_backend.py`), the dependency lock,
synthetic tests, native smoke check, documentation, licensing, ignore rules and
CI workflow were reviewed before publication. No native binaries, account stores,
QR artifacts, real recipient data, or private outputs are published.

**77 offline tests pass** on this Mac with Python 3.11.6. An additional **9
synthetic booking-adapter tests** verify preservation of the existing durable
ledger, accepted/pending entries, concurrent dispatch, recipient guards, and
notice confirmation. The adapter is staged separately and is not activated by
this release. The native smoke check passes on macOS arm64: real protobuf live
and history events, exact-chat indexing, revocation, native import and disabled
runtime download. It never connects or instantiates an account client. No live
pairing or send was performed for this release. Manual unauthenticated connection
probes reached the QR stage in fresh temporary stores and the explicitly selected,
still-unlinked store. QR data was discarded or rendered to /dev/null; no QR was
displayed, device linked, session keys queried, or message sent. CI exercises Linux/macOS and
Python 3.11/3.12/3.14; local native validation covers macOS arm64 only.

Native CI exposed an upstream Python 3.10 import of typing.Self despite its
metadata claiming 3.10 support. This release therefore requires Python 3.11+;
the minimum-version guard fails early. CI installs into a dedicated virtual
environment because shared runner toolchain ancestors fail the same strict
path checks retained in production.

The direct binding was reviewed at
[Neonize 0.5.2 revision 840dd69](https://github.com/krypton-byte/neonize/tree/840dd69fe22fe7d5e2156eeeb02574a861db22c2),
including `_binder.py`, `download.py`, `client.py`, `events.py`, platform/media
helpers, protobuf schemas, and `goneonize/main.go`. Its Whatsmeow revision is
`35ae40906e74` (21 September 2026). This review checks the called text/lifecycle
paths and integration boundaries; it is not an exhaustive audit of every Python
or Go dependency or the full native binary.

The official macOS arm64 wheel SHA-256 is
`670376f5479c56da35fce80ba3c0f98540a46d77364b04f4a6405f6dda783617`.
Its bundled native library was verified against the matching official
[0.5.2 release asset](https://github.com/krypton-byte/neonize/releases/tag/0.5.2)
digest. Four supported native digests are pinned in `NATIVE_HASHES`. Python
package versions and permitted wheel hashes are locked in `requirements.lock`.
The original interface attribution remains in LICENSE, from
[messages-cli-python revision d3185fe](https://github.com/maralski/messages-cli-python/tree/d3185fe9be9af043f089b89ea3f74a5d98a3ef42).

## Threat model and review findings

Protect against accidental dispatch, unintended pairing, ambiguous accounts or
recipients, shell/argument injection, unsafe paths, noisy private logs, dependency
drift/runtime downloads, concurrent store clients, unbounded pipes and misleading
acceptance/retries. Arguments, message contents, database rows and worker output
are treated as untrusted. Root, malicious same-user processes, compromised OS or
Python/native dependencies are outside the CLI's isolation guarantees.

| Area | Control and reviewed behavior |
| --- | --- |
| Transport | No dependency on, invocation of, or fallback to wacli. Direct pinned Neonize/Whatsmeow binding; no Desktop or browser control. |
| Execution intent | All commands dry-run by default. Tests forbid filesystem and worker activity in previews. `--execute` is required even for local initialization/status. |
| Pairing/account scope | Separate TTY-only pair operation, new owned-format store, no automatic migration or credential copy. One stored device selected explicitly. Multiple identities, unexpected QR, changed account, and extra pairing refused. Status returns booleans, not addresses. |
| QR credentials | Parent validates the controlling TTY and resolves its concrete device from attached terminal streams with the same foreground process group. It duplicates a writable handle, or opens the concrete device without following symlinks, validates type/device/foreground group, and forwards only that descriptor through pass_fds. Worker revalidates it and writes QR output there, never logs or JSON pipes. No terminal flags or signal handlers/masks changed. No phone-code login or captured QR files. Revocation remains an intentional WhatsApp UI action. |
| Dependency integrity | All 20 Python packages pinned and hash-locked for installation; versions checked at runtime. Native library ownership/type/links/size/digest/metadata checked before C loading. Unsupported platforms fail closed. |
| Automatic downloader | **Fixed during review:** upstream can fetch a native binary on absence/version mismatch. A disabled `neonize.download` shim is inserted BEFORE eager package import; missing/corrupt native code fails before import. A platform shim also avoids the upstream Linux `uname` shell command. Tests/smoke verify disabled downloads. |
| Media surface | Text-only API; libmagic shim raises on media operations. No FFmpeg installation, media download/upload, preview generation or URL fetching in the called text path. Broad unused upstream dependencies remain pinned because the client imports them. |
| Literal text | Fixed conversation protobuf bypasses upstream string mention parsing and rich link handling; link_preview=False. Text/recipient sent on bounded stdin JSON, never argv, shell, temporary body file, eval or dynamic generated code. |
| Paths/state | Private user-owned stores, credentials, index, marker, lock and existing sidecars; no symlinks/hard links/special files/writable ancestors. New store only; umask 0077. Native raw SQLite URI injection via ?/# paths rejected. No automatic permission repair. |
| Concurrency | Exclusive nonblocking owned-store lock for pair/sync/send. The old sender and new native store are intentionally not activated concurrently. No background daemon or webhook. |
| Native-call bounds | Isolated Python worker, 20-second connection wait, bounded operation deadline, 64 KiB per output stream, bounded input, minimal environment and process-group cleanup. Hard process deadline applies even to Go context.Background sends. Native memory/filesystem behavior is not fully bounded. |
| Log privacy | Python and native stdout/stderr redirected at fd level before import; a private result pipe exposes only reviewed fields. No raw exceptions, credentials or bodies in errors. |
| Local session status | Read-only/query-only ordinary-table validation; SELECT jid only, LIMIT 2, generated/view identities rejected. No key columns queried. Public device identity is used internally and not displayed. |
| History | Read-only/query-only SQLite, bound parameters, restricted authorizer, validated ordinary table, limited chat/date/rows/text, progress budget, revoked/deleted/purged exclusion. Views and generated required fields rejected. |
| Text caching | Selected exact phone chat plus uniquely verified local LID alias only. Both forward and reverse mappings must be unique in an ordinary nongenerated metadata table, queried read-only with bound parameters and a progress budget. Names never authorize aliases. All rows remain keyed to the selected phone. Available live/history plain text; media/edits/view-once/disappearing content skipped. Available alias revocations purge text, tombstones prevent replay restoration while retained. 31-day/10,000-row pruning at writes. Native account-wide metadata/events still occur and cannot be represented as only selected-chat access. |
| Acceptance and retry | Protocol acceptance is not delivery. One send invocation, no CLI retry or fallback. Self-send opt-in checked both parent and worker. Unknown post-dispatch results stay unknown; cache failure after acceptance is a boolean warning. Upstream reconnect/retry behavior remains. |
| Booking continuity | Staged replacement uses same ledger schema/path and pending-before-send transaction. Existing accepted and uncertain rows remain protected. Working installed transport is preserved until intentional native pairing and activation. |
| Publication/CI | Explicit source allowlist; no databases/native artifacts/private output. Immutable official actions, read-only contents, no persisted checkout credentials or account secrets, synthetic tests and offline native smoke. Dependency installation is locked and wheels-only. |

## Pairing terminal fix in 0.2.1

A user-reported terminal_required error exposed a missed boundary in 0.2.0:
opening a terminal in Python text mode r+ requires a seekable stream and fails on
a real TTY. The new-session worker also cannot reopen /dev/tty. The parent now
opens a write-only terminal descriptor, checks it, forwards only that descriptor,
and closes it after success or failure. The worker validates the descriptor
before loading the protocol, then duplicates it only for QR rendering. Send/sync
inherit no such descriptor; isolation and process-group cleanup remain intact.

Four regression tests cover explicit forwarding, cleanup on launch failure,
rejection of stdio/non-TTY/wrong-command descriptors, and synthetic QR rendering
through a real pseudo-terminal from a detached subprocess. The real PTY test
proves the QR marker reaches only the terminal while captured result output is
separate; it loads no WhatsApp account/client and performs no network operation.

## Pairing diagnostics in 0.2.2

The generic not_connected result previously combined native connection errors,
QR renderer errors, ended sessions and pairing deadlines. The worker now reports
those pairing phases separately. Native exception strings are matched only in
memory and mapped to fixed allowlisted categories. Neither raw errors, endpoint
addresses nor QR/account data are emitted. The parent accepts only fixed error
codes/categories. Five regression tests cover safe classification, malicious
backend reason redaction, native connection failure, QR-render failure and the
pairing deadline. Send behavior and its uncertain-outcome/no-retry policy remain
unchanged. Pairing is never automatically repeated after a failure.

## Concrete terminal fix in 0.2.3

The user's qr_render_failed report exposed another macOS boundary: an inherited
descriptor opened through the session-relative /dev/tty alias remains isatty(),
but writes fail with EIO after start_new_session. The parent now forwards a handle
to the actual terminal device after checking the controlling terminal's foreground
group and the selected character device. The detached worker and bounded cleanup
are preserved; no terminal settings or signal handlers/masks are changed.

Two regressions create a real controlling pseudo-terminal, enable TOSTOP only in
that private test terminal, reproduce the alias failure on macOS, and verify the
concrete handle works in the isolated worker. They exercise both a synthetic
writer and the installed Segno QR renderer with synthetic pairing data. Terminal
flags, signal state and result-pipe separation are checked; QR bytes are discarded.
These checks load no account client and do not connect. CI repeats the real
renderer check after installing the locked dependencies on all four runners.

## Verified privacy-ID reads and permission diagnosis in 0.2.4

The previous phone-only cache silently skipped LID-addressed messages, even
when the native account contained an exact identity mapping. The sync worker now
accepts one unique forward/reverse mapping for the selected phone. Contradictory,
missing, malformed, generated, or view-based metadata never broadens scope.
History conversation and message keys must both match an accepted address.
Live/history events and revocations use the same canonical phone cache key.
Send arguments, fixed recipients, link-preview controls and caller ledgers are
unchanged. Ten additional tests cover these boundaries and safe permission errors;
the installed-wheel smoke exercises real LID live/history/revocation protobufs.

Account-lock PermissionError now produces a fixed store_access_denied code
before loading or calling the transport. There is no automatic retry or permission
repair. A sandbox approval enabled a bounded requested sync against the existing
linked account; it returned zero selected-chat rows. No human messages were sent,
new devices linked, credentials extracted, read receipts deliberately sent, or OS
security settings changed. Successful approval does not imply permanent sandbox
or OS permission. The CLI still lacks phone history backfill through this pinned
binding and cannot promise complete reads or recover uncached earlier messages.

## Automated security checks

- **Bandit 1.9.4:** zero medium/high findings. Two LOW findings, B404 subprocess
  import and B603 fixed Popen call, manually reviewed. No shell; only the selected
  Python interpreter and owned worker run; body stays on stdin; minimal env and
  output/deadline cleanup are enforced. No suppressions.
- **Ruff 0.16.10, F/E9:** passed for runtime, tests and native smoke source.
- **pip-audit 2.10.1:** all 20 locked Python packages checked; **no known
  vulnerabilities found**. This is a database snapshot, not proof of safety; the
  bundled Go/native dependency chain is not covered by pip-audit.
- **detect-secrets 1.5.0:** publication file scan and manual content review. The
  intentionally fake `synthetic-private-value` redaction fixture is not an account
  credential. The scanner also flagged four native release SHA-256 constants;
  these are public integrity metadata, not secrets.

Reproduce with:

```sh
python -W error::ResourceWarning -m unittest -v
python -I -B verify_protocol.py
bandit -f json whatsapp_cli.py whatsapp_backend.py
ruff check --select F,E9 whatsapp_cli.py whatsapp_backend.py test_*.py verify_protocol.py
pip-audit --disable-pip --no-deps -r requirements.lock
```

## Residual risks and activation limits

This is an unofficial client; account restrictions, protocol changes and session
expiry remain possible. A release digest establishes artifact integrity relative
to published bytes, not independent authorship/security assurance. The pinned
native library and Python imports are trusted code with full user/account access.
Path checks leave races against same-user attackers; private modes do not encrypt
sessions or messages. The native client may automatically process account-wide
history/events and protocol metadata. Cached text can be incomplete, miss LID
routing, revocations/deletions while offline, and survive in backups. SQLite WAL
coordination may write sidecars even during read-only reads.

There is no exactly-once or delivery guarantee. Native protocol retries exist;
interruption can follow acceptance. The caller must retain its durable attempt
ledger and manually reconcile uncertain sends. The new backend has not been
paired or live-send-tested in this release. An intentional user QR scan is needed
before activating it for booking notices; the working installed sender remains
unchanged until then. No account keys were extracted or migrated during review.

No unresolved medium/high finding was identified in the reviewed CLI code. That
finding is limited to this scope and does not certify the upstream native client.
