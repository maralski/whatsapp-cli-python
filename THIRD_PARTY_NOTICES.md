# History backend dependencies

The optional history backend links Whatsmeow at commit
[`35ae40906e74`](https://github.com/tulir/whatsmeow/tree/35ae40906e74),
the same revision used by Neonize 0.5.2. Whatsmeow is distributed under
[Mozilla Public License 2.0](https://github.com/tulir/whatsmeow/blob/35ae40906e74/LICENSE).
Its corresponding source is available at that exact revision; the module and
checksum pins are in `history_bridge/go.mod` and `go.sum`. Go and other linked
modules retain their own license terms. Anyone distributing compiled binaries
must preserve the applicable notices and source availability obligations.

The backfill design was independently implemented after inspecting
[wacli](https://github.com/openclaw/wacli/tree/a4f23eef7395473931e3a44c93eacd6ebebdc313).
wacli is MIT licensed, Copyright (c) 2026 Peter Steinberger. Its bounded anchor,
identity and coverage approach informed the design. Its presence, offline replay,
history-notification handling and primary-phone recovery paths also informed the
independent scoped refresh implementation; this project does not import,
invoke or ship wacli code or executables.
