# Pre-publication security review — 0.1.0

Reviewed on 4 October 2026, before initial publication. This is a maintainer
review with automated checks, not an independent audit or certification.

## Scope and evidence

- All runtime code in `whatsapp_cli.py`, all synthetic tests, documentation,
  licensing, ignore rules, and the CI workflow were reviewed.
- Runtime source SHA-256:
  `ea9b989718eb477ae130e8ea363bef2cbb4d00cfde6232a272ad7b4ef937a4d1`.
- Test source SHA-256:
  `4ffdeabbbeeba7cb40e93125f6f535b73a1f9c5610aaccc0e13e833533ab4d2a`.
- **41 tests passed** on macOS with Python **3.9.6, 3.11.6, and 3.14**.
  Tests used only synthetic fixtures and fake/local Python children. This
  repository's implementation was not used to send a real WhatsApp message.
  Linux CI is configured; local validation was on macOS.
- Installed wacli reported **0.20.0**. Its version and `send text --help` were
  inspected without accessing any real account. Command flags, JSON envelopes,
  SQLite schema, file writes, retries, and daemon delegation were checked against
  [wacli revision a4f23ee](https://github.com/openclaw/wacli/tree/a4f23eef7395473931e3a44c93eacd6ebebdc313),
  corresponding to the reviewed 0.20.0 source. The complete upstream dependency
  chain was not independently audited; the binary is not bundled or installed.
- The reference interface was read at
  [messages-cli-python revision d3185fe](https://github.com/maralski/messages-cli-python/tree/d3185fe9be9af043f089b89ea3f74a5d98a3ef42).
  Its adapted validation patterns retain MIT attribution.

## Threat model

Protect against accidental sends, scope expansion, ambiguous recipients,
executable substitution through PATH, argument/code injection, unsafe existing
paths, noisy backend errors leaking private data, unbounded outputs, misleading
success, and blind retries after an uncertain send. Treat CLI arguments,
message text, index contents, and backend output as untrusted inputs.

The executing user intentionally selects a trusted backend and account store.
The wrapper is not a sandbox against malicious same-user programs, root,
compromised Python/SQLite/OS libraries, or a malicious upstream executable.
Those principals can already access this user's linked account and messages.

## Findings and dispositions

| Area | Review result and control |
| --- | --- |
| Execution intent | Dry-run default for history and sends; `--execute` required. Dry-run tests forbid filesystem/account access and child launch. |
| Recipient scope | Exact international numbers or phone JIDs only; names, groups, LIDs, broadcasts, and device-qualified JIDs rejected. The imported send function also validates. |
| Process/code injection | No shell, eval, dynamically generated code, pass-through flags, or arbitrary backend commands. Bodies are one fixed argv value; shell metacharacters and Unicode tested through a real fake-backend subprocess. |
| Backend selection | Explicit absolute direct path and caller-provided trusted executable SHA-256. Ownership, permissions, type, hard links, symlinks, size, and metadata checked. Hashing uses an open no-follow descriptor and bounded reads; detected changes fail closed. |
| Ambient configuration | Explicit store; minimal child environment, no inherited proxy, WACLI account settings, or loader variables. New child files have umask `0077`. |
| Daemon delegation | **Fixed during review:** refuse any existing `.send.sock`. A daemon could otherwise run a different binary or have webhooks enabled despite pinning the foreground executable. Concurrent same-user changes remain outside isolation guarantees. |
| Existing backend files | **Fixed during review:** check database sidecars, `LOCK`, `.last-send-at`, and `SESSION_REVOKED` for unsafe files/links before dispatch. No automatic repair, creation, pairing, or lock removal. |
| SQL scope and injection | One supplied index, one exact chat, bounded timezone-aware window and row count. Parameter binding, read-only/query-only connection, trusted schema disabled, restricted authorizer, ordinary-table validation, and progress limits. Views and generated required columns refused. |
| SQLite data exposure | Project only ID, timestamp, direction, and bounded text. No contact enumeration, media-key retrieval, credential query, or rich-message decoding. Deleted/revoked/purged rows excluded. Test fixtures prove chat/date boundaries and committed WAL visibility. |
| Resource limits | Bounded input bytes/characters, binary size/hash reads, child stdout/stderr, child deadline, history row/text limits, and query progress budget. Filesystem stalls and OS/native-library resource behavior are not fully bounded. |
| Privacy of output | Dry-run/errors omit recipients, paths, bodies, raw backend output, and private warnings. History intentionally contains requested text, emitted as escaped JSON. Malformed backend output never appears in errors. |
| Delivery and retry | Exactly one backend invocation per command; no wrapper retry or automatic transport fallback. Strict envelope, exact recipient, boolean success, and bounded message-ID checks. Unknown post-launch outcomes remain unknown; protocol acceptance is not delivery. Backend retries still exist. |
| Child cleanup | Timeout/output-overflow tests exercise termination and pipe cleanup, including a leader that exits while a descendant retains pipes. Interruptions are treated as uncertain sends. |
| Publication | Explicit source/documentation/workflow allowlist; no databases, binaries, real contacts, account paths, pairing artifacts, captured chat output, or credentials included. Ignore rules are additional protection, not a security boundary. |
| CI | Synthetic tests only, read-only contents permission, immutable official action pins, no persisted checkout credentials, no account secrets, no `pull_request_target`, and bounded job duration. |

## Automated security checks

**Bandit 1.9.4**, all default tests, runtime source only, no `nosec` suppressions:

- Zero medium- or high-severity findings.
- Two low-severity/high-confidence findings: **B404** (`subprocess` import) and
  **B603** (the `Popen` call). Both were manually reviewed: the subprocess is
  necessary for the selected transport; executable bytes are pinned; the
  argument list and command are fixed; no shell is used; input stays data;
  inherited configuration is restricted. They are retained as reviewed
  warnings, not represented as a clean scanner run.

**Ruff 0.16.10** static correctness checks (`F,E9`) passed after removing an unused test import.

**detect-secrets 1.5.0** scanned the publication files. Its only candidate value
was the test constant `SECRET = "synthetic-private-value"`, also quoted in this
report: an intentionally fake redaction fixture. It is not a credential.
Developer caches are excluded from both scanning and publication. Manual content review also checks
that only fictional example phone numbers, synthetic bodies, generic paths,
and public source revisions are present. No real account artifacts are included.

Commands used for reproducible checks (the scanners are optional developer tools,
not runtime dependencies):

```sh
python3 -W error::ResourceWarning -m unittest -v
bandit -f json whatsapp_cli.py
detect-secrets scan --all-files --exclude-files '(^|/)(__pycache__|\.git|\.ruff_cache)/'
```

## Residual risks and limitations

1. **Message-body exposure in backend argv.** wacli 0.20.0 has no stdin text
   interface. Local same-user process inspection may reveal bodies/recipients.
   This is documented prominently; confidential argv requirements need a
   different transport or a reviewed upstream change.
2. **Unofficial client and upstream trust.** WhatsApp Web protocol support can
   change or accounts can be restricted. A checksum provides byte integrity,
   not authenticity or safety. Independently verify the release source before
   pinning it. No claim is made that all wacli/whatsmeow dependencies are free of
   vulnerabilities. This is not the supported WhatsApp Business API.
3. **Linked account and local retention.** A send backend accesses credentials,
   writes account/index files, and may cache message text. Privacy depends on
   the account, user, disk, backups, and upstream client. Private modes do not
   encrypt data; the backend may normalize its local store file permissions.
4. **Race and routing limits.** Path preflights cannot stop hostile concurrent
   same-user changes. WhatsApp/backend recipient canonicalization happens
   outside the wrapper. An output recipient mismatch is detected after possible
   dispatch and cannot undo it.
5. **Uncertain outcomes and duplicates.** Timeouts/errors may follow acceptance;
   manual repeats or separate invocations can duplicate messages. The wrapper
   has no durable ledger and the backend has protocol retries. Integrations
   must manage idempotency themselves; there is no exactly-once guarantee.
6. **Cached/deleted history.** Output reflects the index at read time, not full
   server history or current device deletion state. WAL reads may create/use
   coordination sidecars; read-only index access is not zero filesystem writes.
7. **Validation coverage.** No live send, delivery receipt, account pairing,
   attachment transfer, or Windows support was tested or claimed for this
   project. Fake end-to-end tests verify wrapper plumbing and protocol shape,
   not the availability of WhatsApp service.

No unresolved medium/high-severity finding was identified in the reviewed
wrapper. The residual risks above remain part of this release's documented
contract; publishing the source does not remove them.
