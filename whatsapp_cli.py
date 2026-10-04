#!/usr/bin/env python3
"""Private account linking, bounded history, and direct WhatsApp text sends."""

import argparse
from contextlib import closing
import datetime as dt
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

VERSION = "0.2.0"
MAX_TEXT = 10000
MAX_INPUT_BYTES = 40000
MAX_ROWS = 200
MAX_OUTPUT_BYTES = 65536
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
    """Read only the supplied index; do not connect or discover credentials."""
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


def verify_store(value):
    path, _ = checked_path(value, directory=True, private=True)
    if "?" in str(path) or "#" in str(path):
        raise SafeError("invalid_path", "Account store paths cannot contain URL query or fragment characters.")
    for name in ("account.json", "session.db", "messages.sqlite3", ".cli.lock"):
        item = path / name
        if name != ".cli.lock" or os.path.lexists(item):
            checked_path(item, private=True)
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.lexists(str(item) + suffix):
                checked_path(str(item) + suffix, private=True)
    try:
        if (path / "account.json").stat().st_size > 1024:
            raise ValueError()
        if json.loads((path / "account.json").read_text()) != {"format": "whatsapp-cli-python", "version": 1}:
            raise ValueError()
    except (ValueError, OSError):
        raise SafeError("foreign_store", "Use a store created by init. Existing account stores are not migrated automatically.") from None
    return path


def session_identity(store):
    """Read only the public device address, never session key columns."""
    connection = None
    try:
        connection = sqlite3.connect("file:" + quote(str(store / "session.db"), safe="/") + "?mode=ro", uri=True, timeout=1)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.set_progress_handler(lambda: 1, 10000)
        schema = connection.execute("SELECT type,sql FROM sqlite_master WHERE name='whatsmeow_device'").fetchone()
        if schema is None:
            return None
        if schema[0] != "table" or not schema[1].upper().startswith("CREATE TABLE"):
            raise ValueError()
        fields = connection.execute("PRAGMA table_xinfo(whatsmeow_device)").fetchall()
        if not any(row[1] == "jid" and row[6] == 0 for row in fields):
            raise ValueError()
        rows = connection.execute("SELECT jid FROM whatsmeow_device LIMIT 2").fetchall()
        if not rows:
            return None
        if len(rows) != 1 or not isinstance(rows[0][0], str) or not re.fullmatch(r"[1-9][0-9]{6,14}:[0-9]{1,5}@s\.whatsapp\.net", rows[0][0]):
            raise ValueError()
        return rows[0][0]
    except (sqlite3.Error, ValueError, TypeError):
        raise SafeError("session_unavailable", "A single compatible account session is required; no automatic pairing or repair attempted.") from None
    finally:
        if connection is not None:
            connection.close()


def initialize_store(value):
    path = local_path(value)
    if "?" in str(path) or "#" in str(path):
        raise SafeError("invalid_path", "Account store paths cannot contain URL query or fragment characters.")
    checked_path(path.parent, directory=True)
    if os.path.lexists(path):
        raise SafeError("store_exists", "Initialization requires a new store directory; existing data is never overwritten.")
    old_umask = os.umask(0o077)
    try:
        path.mkdir(mode=0o700)
        (path / "account.json").write_text(json.dumps({"format": "whatsapp-cli-python", "version": 1}))
        with closing(sqlite3.connect(path / "session.db")) as connection, connection:
            connection.execute("PRAGMA user_version=0")
        with closing(sqlite3.connect(path / "messages.sqlite3")) as connection, connection:
            connection.execute("CREATE TABLE messages (chat_jid TEXT NOT NULL, msg_id TEXT NOT NULL, ts INTEGER NOT NULL, from_me INTEGER NOT NULL, text TEXT, revoked INTEGER DEFAULT 0, deleted_for_me INTEGER DEFAULT 0, payload_purged_at INTEGER, UNIQUE(chat_jid,msg_id))")
            connection.execute("CREATE INDEX idx_messages_chat_ts ON messages(chat_jid,ts)")
        return {"status": "initialized", "linked": False}
    finally:
        os.umask(old_umask)


def run_bounded(argv, timeout, request=None):
    """One POSIX child; bound captured output and redact all backend errors."""
    # Do not pass ambient proxies, loader hooks, account overrides, or secrets.
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "HOME": str(Path.home())}
    process = None
    try:
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE if request is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment, shell=False,
            close_fds=True, start_new_session=True, umask=0o077,
        )
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            pending = memoryview(request) if request is not None else None
            if pending is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                for key, _ in selector.select(remaining):
                    if key.data == "stdin":
                        if pending:
                            try:
                                pending = pending[os.write(key.fileobj.fileno(), pending[:8192]):]
                            except BlockingIOError:
                                continue
                        if not pending:
                            selector.unregister(key.fileobj)
                            process.stdin.close()
                        continue
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
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            process.stdout.close()
            process.stderr.close()


def backend_operation(command, store, *, jid=None, text=None, allow_self=False, seconds=30, limit=200):
    selected = verify_store(store)
    identity = session_identity(selected)
    if command == "pair":
        if identity:
            raise SafeError("already_linked", "Store already linked; pairing a second account is forbidden.")
        # QR credentials must never enter logs or pipes. The worker opens /dev/tty.
        try:
            with open("/dev/tty", "r+") as terminal:
                if not terminal.isatty():
                    raise OSError()
        except OSError:
            raise SafeError("terminal_required", "Run pair yourself in an interactive terminal to scan the QR code.") from None
    elif not identity:
        raise SafeError("not_linked", "Run pair explicitly in an interactive terminal first; ordinary commands never pair.")
    if jid is not None:
        jid = phone_jid(jid)
    if command == "send":
        validate_text(text)
        if identity.split(":")[0] + "@s.whatsapp.net" == jid and not allow_self:
            raise SafeError("self_send_blocked", "Sending to the linked account requires explicit --allow-self.")
    request = {"command": command, "store": str(selected), "identity": identity,
               "jid": jid, "text": text, "seconds": seconds, "limit": limit, "allow_self": allow_self}
    worker = Path(__file__).absolute().with_name("whatsapp_backend.py")
    checked_path(worker)
    code, raw = run_bounded([sys.executable, "-I", "-B", str(worker)],
                            150 if command == "pair" else seconds + 25 if command == "sync" else SEND_TIMEOUT,
                            json.dumps(request, ensure_ascii=True).encode())
    try:
        data = json.loads(raw)
        if code != 0 or not isinstance(data, dict):
            raise ValueError()
        if data.get("error") in {"backend_unavailable", "backend_busy", "not_connected", "pair_required", "sync_failed"}:
            raise SafeError(data["error"], "Direct backend unavailable or account operation stopped; no automatic retry.")
        expected = {"send": "accepted", "sync": "synced", "pair": "linked"}[command]
        if data.get("status") != expected:
            raise ValueError()
        if command == "send":
            if data.get("delivery_confirmed") is not False or not isinstance(data.get("message_id"), str) or not re.fullmatch(ID_PATTERN, data["message_id"]):
                raise ValueError()
            return {"status": "accepted", "message_id": data["message_id"], "delivery_confirmed": False,
                    "local_store_warning": data.get("local_store_warning") is True}
        if command == "sync":
            if type(data.get("stored")) is not int or not 0 <= data["stored"] <= limit:
                raise ValueError()
            return {"status": "synced", "stored": data["stored"], "complete_history": False}
        return {"status": "linked"}
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise SafeError("send_unknown" if command == "send" else "operation_unknown", "Operation outcome unknown. Inspect account state before any manual repeat.") from None


def send_text(store, jid, text, *, allow_self=False):
    return backend_operation("send", store, jid=jid, text=text, allow_self=allow_self)


def parser():
    result = Parser(description=__doc__, allow_abbrev=False)
    result.add_argument("--version", action="version", version="whatsapp-cli-python " + VERSION)
    commands = result.add_subparsers(dest="command", required=True, parser_class=Parser)
    history = commands.add_parser("history", help="Preview or read one bounded local chat", allow_abbrev=False)
    history.add_argument("--db", required=True, help="Absolute path to messages.sqlite3")
    history.add_argument("--chat", required=True)
    history.add_argument("--since", required=True)
    history.add_argument("--until", required=True)
    history.add_argument("--limit", type=int, default=20)
    history.add_argument("--execute", action="store_true")
    for name in ("init", "status", "pair", "sync", "send"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--store", required=True, help="Absolute path to a private CLI account store")
        command.add_argument("--execute", action="store_true")
        if name in ("send", "sync"):
            command.add_argument("--to" if name == "send" else "--chat", required=True)
        if name == "send":
            command.add_argument("--allow-self", action="store_true")
        if name == "sync":
            command.add_argument("--seconds", type=int, default=30, help="Receive selected-chat text events for 1–120 seconds")
            command.add_argument("--limit", type=int, default=200, help="Maximum selected-chat rows stored (1–200)")
    return result


def emit(value, stream):
    # Escape control characters and Unicode for safe JSON display in terminals.
    print(json.dumps(value, ensure_ascii=True), file=stream)


def main(argv=None, stdin=None, stdout=None, stderr=None):
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        args = parser().parse_args(argv)
        if sys.version_info < (3, 11):
            raise SafeError("unsupported_python", "The pinned direct backend requires Python 3.11 or newer.")
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
            local_path(args.store)
            body = input_body(sys.stdin.buffer if stdin is None else stdin) if args.command == "send" else None
            jid = phone_jid(args.to if args.command == "send" else args.chat) if args.command in ("send", "sync") else None
            if args.command == "sync" and (not 1 <= args.seconds <= 120 or not 1 <= args.limit <= MAX_ROWS):
                raise SafeError("invalid_scope", "Sync requires 1–120 seconds and a row limit of 1–200.")
            if not args.execute:
                data = {"status": "dry_run", "command": args.command}
                if body is not None:
                    data.update(message_chars=len(body), no_preview=True, allow_self=args.allow_self)
                emit(data, stdout)
            elif args.command == "init":
                emit(initialize_store(args.store), stdout)
            elif args.command == "status":
                emit({"status": "local", "linked": session_identity(verify_store(args.store)) is not None,
                      "online_checked": False}, stdout)
            elif args.command == "send":
                emit(send_text(args.store, jid, body, allow_self=args.allow_self), stdout)
            else:
                emit(backend_operation(args.command, args.store, jid=jid,
                                       seconds=getattr(args, "seconds", 30), limit=getattr(args, "limit", 200)), stdout)
        return 0
    except SafeError as error:
        emit({"error": error.code, "message": str(error)}, stderr)
        return 1
    except (OSError, UnicodeError, ValueError, KeyboardInterrupt):
        emit({"error": "operation_failed", "message": "Operation interrupted or output unavailable. If a send began, inspect the chat before repeating."}, stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
