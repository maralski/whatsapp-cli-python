#!/usr/bin/env python3
"""Bounded local WhatsApp history and explicit text sends through wacli."""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from urllib.parse import quote

VERSION = "0.1.0"
MAX_TEXT = 10000
MAX_INPUT_BYTES = 40000
MAX_ROWS = 200
MAX_OUTPUT_BYTES = 65536
MAX_BINARY_BYTES = 256 * 1024 * 1024
SEND_TIMEOUT = 50
HISTORY_TIMEOUT = 3
ID_PATTERN = r"[A-Za-z0-9_-]{1,128}"


class SafeError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default errors echo private values and paths.
        raise SafeError("invalid_arguments", "Invalid arguments; run --help.")


def phone_jid(value):
    if re.fullmatch(r"\+[1-9][0-9]{6,14}", value):
        return value[1:] + "@s.whatsapp.net"
    if re.fullmatch(r"[1-9][0-9]{6,14}@s\.whatsapp\.net", value):
        return value
    raise SafeError("invalid_recipient", "Use an exact international phone number or phone JID; names and groups are unsupported.")


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        result = parsed.astimezone(dt.timezone.utc)
        if result.year < 1970:
            raise ValueError()
        return result
    except (ValueError, OverflowError):
        raise SafeError("invalid_date", "Use an ISO 8601 date from 1970 onward with an explicit timezone.") from None


def history_scope(chat, since, until, limit):
    jid = phone_jid(chat)
    start, end = timestamp(since), timestamp(until)
    if not 1 <= limit <= MAX_ROWS or not start < end or end - start > dt.timedelta(days=31):
        raise SafeError("invalid_scope", "Choose one chat, a positive date window of at most 31 days, and a limit of 1–200.")
    epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    # Stored timestamps are whole Unix seconds; ceil preserves fractional bounds.
    def ceiling(value):
        delta = value - epoch
        return delta.days * 86400 + delta.seconds + int(delta.microseconds > 0)
    return jid, ceiling(start), ceiling(end), limit


def input_body(stream):
    if stream.isatty():
        raise SafeError("invalid_input", "Provide UTF-8 text through standard input.")
    try:
        data = stream.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise ValueError()
        text = data.decode("utf-8", errors="strict")
        validate_text(text)
        return text
    except (ValueError, UnicodeError, OSError):
        raise SafeError("invalid_input", "Provide nonempty UTF-8 text, at most 10,000 characters and 40,000 bytes, without NUL.") from None


def validate_text(text):
    try:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT or "\x00" in text:
            raise ValueError()
        if len(text.encode("utf-8", errors="strict")) > MAX_INPUT_BYTES:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise SafeError("invalid_input", "Provide nonempty UTF-8 text, at most 10,000 characters and 40,000 bytes, without NUL.") from None


def local_path(value):
    """Lexical validation only: safe for dry-run, with no filesystem inspection."""
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value) or "://" in value:
        raise SafeError("invalid_path", "Use an explicit absolute local path without control characters or parent traversal.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise SafeError("invalid_path", "Use an explicit absolute local path without control characters or parent traversal.")
    return path


def checked_path(value, *, directory=False, private=False):
    """Reject symlinks, unexpected owners, unsafe permissions, and special files.

    This is a preflight, not isolation against a compromised same-user process.
    """
    path = local_path(str(value))
    try:
        for parent in reversed(path.parents):
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.getuid()):
                raise ValueError()
            # A root-owned sticky temporary parent is allowed; the leaf is checked.
            if info.st_mode & 0o022 and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
                raise ValueError()
        info = path.lstat()
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not correct_type or info.st_uid not in ((os.getuid(),) if private else (0, os.getuid())):
            raise ValueError()
        if info.st_mode & (0o077 if private else 0o022):
            raise ValueError()
        if not directory and info.st_nlink != 1:
            raise ValueError()
        return path, info
    except (OSError, ValueError):
        raise SafeError("unsafe_path", "Selected path is unavailable or has unsafe type, ownership, links, or permissions. No permissions were changed.") from None


def read_history(database, scope):
    """Read only the supplied index; do not discover credentials or invoke wacli."""
    path, _ = checked_path(database)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if os.path.lexists(sidecar):
            checked_path(sidecar)
    connection = None
    deadline = time.monotonic() + HISTORY_TIMEOUT
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return int(steps >= 10000 or time.monotonic() >= deadline)

    try:
        uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        connection.set_progress_handler(progress, 1000)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        if hasattr(connection, "setlimit"):
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
        schema = connection.execute("SELECT type, sql FROM sqlite_master WHERE name='messages'").fetchone()
        if not schema or schema[0] != "table" or not schema[1].upper().startswith("CREATE TABLE"):
            raise ValueError()
        fields = connection.execute("PRAGMA table_xinfo(messages)").fetchall()
        columns = {row[1] for row in fields if row[6] == 0}
        required = {"chat_jid", "msg_id", "ts", "from_me", "text", "revoked", "deleted_for_me", "payload_purged_at"}
        if not required <= columns:
            raise ValueError()

        def authorize(action, first, second, database_name, source):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and first == "messages" and second in required | {"rowid", "ROWID"} and source is None:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and second == "substr":
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        connection.set_authorizer(authorize)
        jid, start, end, limit = scope
        rows = connection.execute(
            "SELECT substr(msg_id,1,129), ts, from_me, substr(text,1,?) FROM messages "
            "WHERE chat_jid=? AND ts>=? AND ts<? AND revoked=0 AND deleted_for_me=0 "
            "AND payload_purged_at IS NULL ORDER BY ts DESC, rowid DESC LIMIT ?",
            (MAX_TEXT + 1, jid, start, end, limit),
        ).fetchall()
        result = []
        for msg_id, when, outgoing, text in rows:
            if not isinstance(msg_id, str) or not re.fullmatch(ID_PATTERN, msg_id):
                raise ValueError()
            if type(when) is not int or type(outgoing) is not int or outgoing not in (0, 1):
                raise ValueError()
            if text is not None and not isinstance(text, str):
                raise ValueError()
            result.append({
                "id": msg_id,
                "timestamp": dt.datetime.fromtimestamp(when, dt.timezone.utc).isoformat(),
                "direction": "sent" if outgoing else "received",
                "text": text[:MAX_TEXT] if text is not None else None,
                "text_available": text is not None,
                "text_truncated": text is not None and len(text) > MAX_TEXT,
            })
        return result
    except (sqlite3.Error, OSError, ValueError, OverflowError, TypeError):
        raise SafeError("history_unavailable", "History unavailable: check access, compatible index schema, and scope. No retry performed.") from None
    finally:
        if connection is not None:
            connection.close()


def verify_binary(value, expected):
    if not expected or not re.fullmatch(r"[0-9a-fA-F]{64}", expected):
        raise SafeError("binary_unverified", "An independently verified wacli executable SHA-256 is required for execution.")
    path, before = checked_path(value)
    try:
        if not before.st_mode & 0o111 or not 0 < before.st_size <= MAX_BINARY_BYTES:
            raise ValueError()
        digest = hashlib.sha256()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            opened = os.fstat(source.fileno())
            if fingerprint(opened) != fingerprint(before):
                raise ValueError()
            total = 0
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_BINARY_BYTES:
                    raise ValueError()
                digest.update(block)
            after = os.fstat(source.fileno())
        if digest.hexdigest() != expected.lower() or fingerprint(after) != fingerprint(opened) or fingerprint(path.lstat()) != fingerprint(after):
            raise ValueError()
        return path
    except (ValueError, OSError):
        raise SafeError("binary_unverified", "The selected executable is unsafe, changed during verification, or does not match its trusted SHA-256.") from None


def fingerprint(info):
    # Reading the file may update atime; that is not an executable modification.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def verify_store(value):
    path, _ = checked_path(value, directory=True, private=True)
    # A running daemon could be a different executable and have webhooks enabled.
    # Keep execution confined to the pinned foreground backend.
    if os.path.lexists(path / ".send.sock"):
        raise SafeError("backend_busy", "A send delegate socket exists in this store. Stop the owning sync process through its normal controls before using this CLI.")
    # Stat only; do not read credentials. No automatic auth, creation, or chmod.
    for name in ("session.db", "wacli.db"):
        checked_path(path / name, private=True)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = path / (name + suffix)
            if os.path.lexists(sidecar):
                checked_path(sidecar, private=True)
    for name in ("LOCK", ".last-send-at", "SESSION_REVOKED"):
        item = path / name
        if os.path.lexists(item):
            checked_path(item, private=True)
    return path


def run_bounded(argv, timeout):
    """One POSIX child; bound captured output and redact all backend errors."""
    # Do not pass ambient proxies, dynamic-loader hooks, account overrides,
    # WACLI options, or secrets through to the backend.
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": str(Path.home())}
    process = None
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment, shell=False,
            close_fds=True, start_new_session=True, umask=0o077,
        )
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    target = captured[key.data]
                    target.extend(chunk)
                    if len(target) > MAX_OUTPUT_BYTES:
                        raise ValueError()
        result = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        return result, bytes(captured["stdout"])
    except OSError:
        if process is None:
            raise SafeError("send_not_started", "Unable to launch the verified backend. No send was started.") from None
        raise SafeError("send_unknown", "Send outcome is unknown. Inspect the actual chat before any manual repeat.") from None
    except (ValueError, TimeoutError, subprocess.TimeoutExpired, KeyboardInterrupt):
        raise SafeError("send_unknown", "Send outcome is unknown. Inspect the actual chat before any manual repeat.") from None
    finally:
        if process is not None:
            # Kill the process group even if the leader exited with open child pipes.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


def send_text(binary, checksum, store, jid, text):
    jid = phone_jid(jid)
    validate_text(text)
    path = verify_binary(binary, checksum)
    selected_store = verify_store(store)
    argv = [str(path), "--store", str(selected_store), "--json", "--timeout", "40s",
            "send", "text", "--to", jid, "--message", text, "--no-preview"]
    code, raw = run_bounded(argv, SEND_TIMEOUT)
    try:
        envelope = json.loads(raw)
        data = envelope["data"]
        if code != 0 or envelope["success"] is not True or not isinstance(data, dict):
            raise ValueError()
        if data.get("sent") is not True or data.get("to") != jid:
            raise ValueError()
        if not isinstance(data.get("id"), str) or not re.fullmatch(ID_PATTERN, data["id"]):
            raise ValueError()
        # A local index write failure does not undo protocol acceptance.
        return {"status": "accepted", "message_id": data["id"], "delivery_confirmed": False,
                "local_store_warning": bool(data.get("store_warning"))}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise SafeError("send_unknown", "Send outcome is unknown. Inspect the actual chat before any manual repeat.") from None


def parser():
    result = Parser(description=__doc__, allow_abbrev=False)
    result.add_argument("--version", action="version", version="whatsapp-cli-python " + VERSION)
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    history = commands.add_parser("history", help="Preview or read one bounded local chat", allow_abbrev=False)
    history.add_argument("--db", required=True, help="Absolute path to the wacli message index (wacli.db)")
    history.add_argument("--chat", required=True, help="Exact international phone number or phone JID")
    history.add_argument("--since", required=True)
    history.add_argument("--until", required=True)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--execute", action="store_true")
    send = commands.add_parser("send", help="Preview or send UTF-8 stdin to one exact recipient", allow_abbrev=False)
    send.add_argument("--to", required=True)
    send.add_argument("--store", required=True, help="Absolute path to an already linked private wacli store")
    send.add_argument("--wacli", help="Absolute direct path to a trusted wacli 0.20.0 executable")
    send.add_argument("--wacli-sha256", help="Independently verified executable SHA-256 (not archive checksum)")
    send.add_argument("--execute", action="store_true")
    return result


def emit(value, stream):
    # Escape control characters and Unicode for safe JSON display in terminals.
    print(json.dumps(value, ensure_ascii=True), file=stream)


def main(argv=None, stdin=None, stdout=None, stderr=None):
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = parser().parse_args(argv)
        if os.name != "posix":
            raise SafeError("unsupported_platform", "This version supports macOS and Linux only.")
        if args.command == "history":
            local_path(args.db)
            scope = history_scope(args.chat, args.since, args.until, args.limit)
            if not args.execute:
                emit({"status": "dry_run", "command": "history", "limit": args.limit}, stdout)
            else:
                # Buffer all results before printing to avoid partial results on error.
                for row in read_history(args.db, scope):
                    emit(row, stdout)
        else:
            jid = phone_jid(args.to)
            local_path(args.store)
            body = input_body(sys.stdin.buffer if stdin is None else stdin)
            if not args.execute:
                emit({"status": "dry_run", "command": "send", "message_chars": len(body), "no_preview": True}, stdout)
            else:
                if args.wacli is None:
                    raise SafeError("binary_unverified", "Specify --wacli and its independently verified --wacli-sha256 to execute.")
                emit(send_text(args.wacli, args.wacli_sha256, args.store, jid, body), stdout)
        return 0
    except SafeError as error:
        emit({"error": error.code, "message": str(error)}, stderr)
        return 1
    except (OSError, UnicodeError, ValueError, KeyboardInterrupt):
        emit({"error": "operation_failed", "message": "Operation interrupted or output unavailable. If a send began, inspect the chat before repeating."}, stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
