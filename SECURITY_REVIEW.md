# Pre-publication security review — 0.2.1

Reviewed 4 October 2026. Maintainer review plus automated checks; not an
independent audit or certification.

## Scope and evidence

All runtime code (`whatsapp_cli.py`, `whatsapp_backend.py`), the dependency lock,
synthetic tests, native smoke check, documentation, licensing, ignore rules and
CI workflow were reviewed before publication. No native binaries, account stores,
QR artifacts, real recipient data, or private outputs are published.

**60 offline tests pass** on this Mac with Python 3.11.6. An additional **9
synthetic booking-adapter tests** verify preservation of the existing durable
ledger, accepted/pending entries, concurrent dispatch, recipient guards, and
notice confirmation. The adapter is staged separately and is not activated by
this release. The native smoke check passes on macOS arm64: real protobuf live
and history events, exact-chat indexing, revocation, native import and disabled
runtime download. It never connects or instantiates an account client. No live
pairing or send was performed for this release. CI exercises Linux/macOS and
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
| QR credentials | Parent opens `/dev/tty` write-only, validates a character TTY, and forwards only that descriptor through pass_fds to the isolated worker. Worker revalidates it and writes QR output there, never logs or JSON pipes. No phone-code login or captured QR files. Revocation remains an intentional WhatsApp UI action. |
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
| Text caching | Selected exact phone chat only. Available live/history plain text; media/edits/view-once/disappearing content skipped. Available revocations purge text, tombstones prevent replay restoration while retained. 31-day/10,000-row pruning at writes. Native account-wide metadata/events still occur and cannot be represented as only selected-chat access. |
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
