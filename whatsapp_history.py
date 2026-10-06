"""Explicit scoped history archive and bounded primary-phone backfill."""
import base64
from contextlib import closing
import hashlib
import json
import os
import platform
import re
import sqlite3
import time
from urllib.parse import quote

BRIDGE_SOURCE_SHA256 = "fd2c3426f147d36deb2f6d3fc92373cf17279f2716590ce9e164d73f51196bee"
TABLES = {
    "history_messages": {"chat", "id", "ts", "outgoing", "text", "truncated", "revoked"},
    "history_coverage": {"chat", "phone_end", "inaccessible", "stop", "updated"},
}
SCHEMA = (
    "CREATE TABLE history_messages(chat TEXT NOT NULL,id TEXT NOT NULL,ts INTEGER NOT NULL,"
    "outgoing INTEGER NOT NULL,text TEXT,truncated INTEGER NOT NULL DEFAULT 0,"
    "revoked INTEGER NOT NULL DEFAULT 0,UNIQUE(chat,id))",
    "CREATE INDEX history_chat_ts ON history_messages(chat,ts)",
    "CREATE TABLE history_coverage(chat TEXT PRIMARY KEY,phone_end INTEGER NOT NULL,"
    "inaccessible INTEGER NOT NULL,stop TEXT NOT NULL,updated INTEGER NOT NULL)",
)


def scope(cli, chat, since, until, limit):
    jid = cli.phone_jid(chat)
    start, end = cli.timestamp(since), cli.timestamp(until)
    if not start < end or not 1 <= limit <= 200:
        raise cli.SafeError("invalid_scope", "Choose one chat, a positive date range and 1–200 rows.")
    # Whole-second storage: round bounds upward, preserving fractional selection.
    def ceil(value):
        epoch = value.timestamp()
        return int(epoch) + int(epoch > int(epoch))
    return jid, ceil(start), ceil(end), limit


class Archive:
    def __init__(self, cli, store):
        self.cli = cli
        self.store = cli.verify_store(store)
        self.path = self.store / "history.sqlite3"

    def open(self, *, write=False):
        created = False
        if not os.path.lexists(self.path):
            if not write:
                return None
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            created = True
        self.cli.checked_path(self.path, private=True)
        for suffix in ("-wal", "-shm", "-journal"):
            if os.path.lexists(str(self.path) + suffix):
                self.cli.checked_path(str(self.path) + suffix, private=True)
        uri = "file:" + quote(str(self.path), safe="/") + ("?mode=rw" if write else "?mode=ro")
        db = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            deadline = time.monotonic() + 3
            if hasattr(db, "setlimit"):
                db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1024 * 1024)
            schema = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if not schema and write and created:
                with db:
                    for sql in SCHEMA:
                        db.execute(sql)
                    db.execute("PRAGMA user_version=1")
                schema = db.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise ValueError()
            tables = {name for kind, name, sql in schema if kind == "table" and sql.upper().startswith("CREATE TABLE")}
            if tables != set(TABLES) or any(kind not in ("table", "index") for kind, _, _ in schema):
                raise ValueError()
            for table, columns in TABLES.items():
                fields = db.execute("PRAGMA table_xinfo(" + table + ")").fetchall()
                if {row[1] for row in fields if row[6] == 0} != columns or any(row[6] for row in fields):
                    raise ValueError()
            return db
        except Exception:
            db.close()
            raise

    def record(self, chat, rows, *, since=0, until=2**63-1):
        chat = self.cli.phone_jid(chat)
        if not isinstance(rows, list) or len(rows) > 200:
            raise ValueError()
        checked = []
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                    or not re.fullmatch(self.cli.ID_PATTERN, row["id"])
                    or type(row.get("ts")) is not int or not 0 < row["ts"] <= int(time.time()) + 300
                    or type(row.get("from_me")) is not bool or type(row.get("text_truncated", False)) is not bool):
                raise ValueError()
            text = row.get("text")
            if text is not None:
                self.cli.validate_text(text)
            revoke = row.get("revoke_id")
            if revoke is not None and (not isinstance(revoke, str) or not re.fullmatch(self.cli.ID_PATTERN, revoke)):
                raise ValueError()
            checked.append((row, text if since <= row["ts"] < until else None, revoke))
        added = 0
        with closing(self.open(write=True)) as db, db:
            for row, text, revoke in checked:
                before = db.execute("SELECT 1 FROM history_messages WHERE chat=? AND id=?", (chat, row["id"])).fetchone()
                db.execute("INSERT INTO history_messages(chat,id,ts,outgoing,text,truncated) VALUES (?,?,?,?,?,?) "
                           "ON CONFLICT(chat,id) DO UPDATE SET text=CASE WHEN revoked=0 THEN COALESCE(history_messages.text,excluded.text) ELSE NULL END,"
                           "truncated=MAX(history_messages.truncated,excluded.truncated)",
                           (chat, row["id"], row["ts"], int(row["from_me"]), text, int(row.get("text_truncated", False))))
                added += int(before is None)
                if revoke:
                    db.execute("INSERT INTO history_messages(chat,id,ts,outgoing,revoked) VALUES (?,?,?,0,1) "
                               "ON CONFLICT(chat,id) DO UPDATE SET text=NULL,revoked=1", (chat, revoke, row["ts"]))
        return added

    def anchors(self, chat):
        db = self.open()
        if db is None:
            return []
        with closing(db):
            rows = db.execute("SELECT id,ts,outgoing FROM history_messages WHERE chat=? AND revoked=0 ORDER BY ts,id LIMIT 2", (chat,)).fetchall()
        for msg_id, ts, outgoing in rows:
            if (not isinstance(msg_id, str) or not re.fullmatch(self.cli.ID_PATTERN, msg_id)
                    or type(ts) is not int or not 0 < ts <= int(time.time()) + 300
                    or type(outgoing) is not int or outgoing not in (0, 1)):
                raise ValueError()
        return [{"id": msg_id, "ts": ts, "from_me": bool(outgoing)} for msg_id, ts, outgoing in rows]

    def seed(self, chat):
        # The legacy cache has independently verified ordinary columns/authorizer.
        since = "1970-01-01T00:00:00+00:00"
        until = self.cli.dt.datetime.now(self.cli.dt.timezone.utc).isoformat()
        rows = self.cli.read_history(self.store / "messages.sqlite3", scope(self.cli, chat, since, until, 200))
        if rows:
            self.record(chat, [{"id": row["id"], "ts": int(self.cli.timestamp(row["timestamp"]).timestamp()),
                                "from_me": row["direction"] == "sent", "text": row["text"],
                                "text_truncated": row["text_truncated"]} for row in rows])

    def coverage(self, chat, stop=None, *, phone_end=False, inaccessible=False):
        if stop is not None:
            with closing(self.open(write=True)) as db, db:
                db.execute("INSERT INTO history_coverage VALUES (?,?,?,?,?) ON CONFLICT(chat) DO UPDATE SET "
                           "phone_end=MAX(phone_end,excluded.phone_end),inaccessible=excluded.inaccessible,"
                           "stop=excluded.stop,updated=excluded.updated", (chat, int(phone_end), int(inaccessible), stop, int(time.time())))
        result = {"status": "local", "complete_history": False, "phone_reported_end": False,
                  "phone_reports_inaccessible": False, "anchor_ready": False, "headers": 0, "text_messages": 0}
        db = self.open()
        if db is None:
            return result
        with closing(db):
            count = db.execute("SELECT COUNT(*),COUNT(text),MIN(ts),MAX(ts) FROM history_messages WHERE chat=? AND revoked=0", (chat,)).fetchone()
            state = db.execute("SELECT phone_end,inaccessible,stop,updated FROM history_coverage WHERE chat=?", (chat,)).fetchone()
        result.update(headers=count[0], text_messages=count[1], oldest_timestamp=count[2], newest_timestamp=count[3], anchor_ready=count[0] > 0)
        if state:
            result.update(phone_reported_end=bool(state[0]), phone_reports_inaccessible=bool(state[1]), last_stop=state[2], updated=state[3])
        return result

    def page(self, selected, cursor=None):
        chat, start, end, limit = selected
        binding = hashlib.sha256(json.dumps([chat, start, end]).encode()).hexdigest()
        db = self.open()
        if db is None:
            return {"status": "local", "messages": [], "next_cursor": None, "complete_history": False}
        with closing(db):
            with db:
                maximum = db.execute("SELECT COALESCE(MAX(rowid),0) FROM history_messages WHERE chat=?", (chat,)).fetchone()[0]
                snapshot, last_ts, last_row = maximum, end, maximum + 1
                if cursor:
                    try:
                        if len(cursor) > 512:
                            raise ValueError()
                        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                        if not isinstance(data, list) or len(data) != 4 or data[0] != binding or any(type(v) is not int for v in data[1:]):
                            raise ValueError()
                        snapshot, last_ts, last_row = data[1:]
                        if not (0 <= snapshot <= maximum and start <= last_ts < end and 1 <= last_row <= snapshot):
                            raise ValueError()
                    except (ValueError, UnicodeError, json.JSONDecodeError):
                        raise self.cli.SafeError("invalid_cursor", "Cursor is invalid or belongs to another chat or date range.") from None
                rows = db.execute("SELECT rowid,id,ts,outgoing,text,truncated FROM history_messages WHERE chat=? AND ts>=? AND ts<? "
                                  "AND rowid<=? AND revoked=0 AND text IS NOT NULL AND (ts<? OR (ts=? AND rowid<?)) "
                                  "ORDER BY ts DESC,rowid DESC LIMIT ?", (chat, start, end, snapshot, last_ts, last_ts, last_row, limit + 1)).fetchall()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = base64.urlsafe_b64encode(json.dumps([binding, snapshot, last[2], last[0]]).encode()).decode()
        messages = []
        for _, msg_id, ts, outgoing, text, truncated in rows[:limit]:
            self.cli.validate_text(text)
            if not re.fullmatch(self.cli.ID_PATTERN, msg_id) or type(ts) is not int or outgoing not in (0, 1):
                raise ValueError()
            messages.append({"id": msg_id, "timestamp": self.cli.dt.datetime.fromtimestamp(ts, self.cli.dt.timezone.utc).isoformat(),
                             "direction": "sent" if outgoing else "received", "text": text, "text_truncated": bool(truncated)})
        return {"status": "local", "messages": messages, "next_cursor": next_cursor, "complete_history": False}


def verified_bridge(cli, store):
    directory = store.parent / "history-runtime"
    cli.checked_path(directory, directory=True, private=True)
    path = directory / "whatsapp-history"
    _, binary = cli.checked_path(path, private=True)
    manifest_path = directory / "manifest.json"
    _, manifest_info = cli.checked_path(manifest_path, private=True)
    if manifest_info.st_size > 4096 or binary.st_size > 64 * 1024 * 1024 or not binary.st_mode & 0o111:
        raise ValueError()
    manifest = json.loads(manifest_path.read_text())
    if (manifest.get("abi") != 1 or manifest.get("source_sha256") != BRIDGE_SOURCE_SHA256
            or manifest.get("go") != "1.27.1"
            or manifest.get("platform") != [platform.system(), platform.machine()]
            or manifest.get("binary_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()):
        raise ValueError()
    code, raw = cli.run_bounded([str(path), "--version"], 3)
    if code or json.loads(raw) != {"abi": 1, "source_sha256": BRIDGE_SOURCE_SHA256, "go": "go1.27.1"}:
        raise ValueError()
    return path


def fetch(cli, store, selected, *, pages, count, seconds, anchor_db=None):
    chat, start, end, _ = selected
    archive = Archive(cli, store)
    identity = cli.session_identity(archive.store)
    if identity is None:
        raise cli.SafeError("pair_required", "History fetching requires the existing account to be linked.")
    archive.seed(chat)
    if anchor_db is not None:
        # Explicit import of one chat from a compatible plaintext message index.
        # No session tables, keys, media fields or external CLI are accessed.
        imported = cli.read_history(anchor_db, selected)
        if imported:
            archive.record(chat, [{"id": row["id"], "ts": int(cli.timestamp(row["timestamp"]).timestamp()),
                                   "from_me": row["direction"] == "sent", "text": row["text"],
                                   "text_truncated": row["text_truncated"]} for row in imported])
    anchors = archive.anchors(chat)
    if not anchors:
        return {"status": "blocked", "stop_reason": "no_local_anchor", "complete_history": False,
                "requests": 0, "stored": 0, "next_step": "Run sync for this chat while a new message arrives, then fetch again. No relinking is needed."}
    try:
        backend = verified_bridge(cli, archive.store)
    except (OSError, ValueError, cli.SafeError):
        raise cli.SafeError("history_backend_unavailable", "Build and explicitly install the reviewed history backend; runtime downloads are disabled.") from None
    started = time.monotonic()
    total, requests, phone_end, inaccessible = 0, 0, False, False
    stop = "page_budget"
    for _ in range(pages):
        remaining = int(seconds - (time.monotonic() - started))
        if remaining < 1:
            stop = "time_budget"
            break
        anchor = archive.anchors(chat)[0]
        request = {"store": str(archive.store), "identity": identity, "chat": chat, "anchor": anchor, "count": count, "seconds": remaining}
        try:
            code, raw = cli.run_bounded([str(backend)], remaining + 25, json.dumps(request).encode(), max_output_bytes=4 * 1024 * 1024)
            data = json.loads(raw)
        except cli.SafeError:
            raise cli.SafeError("history_interrupted", "History request interrupted; no automatic retry or human message was sent.") from None
        requests += 1
        if code or not isinstance(data, dict):
            raise ValueError()
        error = data.get("error")
        if error:
            labels = {"store_access_denied", "backend_busy", "account_changed", "not_connected", "connect_timeout", "account_stopped", "history_download_failed", "history_request_failed", "store_unavailable", "unsafe_store"}
            raise cli.SafeError(error if error in labels else "history_failed", "History operation failed; no automatic retry, relinking or permission changes.")
        if data.get("status") == "response_timeout":
            stop = "response_timeout"
            break
        if data.get("status") != "received" or type(data.get("phone_reported_end")) is not bool or type(data.get("phone_reports_inaccessible")) is not bool:
            raise ValueError()
        rows = data.get("rows", [])
        if not isinstance(rows, list) or len(rows) > count:
            raise ValueError()
        # The phone returns complete batches, which may cross the stop boundary.
        # Archive only this selected chat's bounded batch; page() applies exact
        # date filtering. Keeping fetched bodies permits later local date ranges.
        added = archive.record(chat, rows)
        total += added
        phone_end, inaccessible = data["phone_reported_end"], data["phone_reports_inaccessible"]
        older = archive.anchors(chat)[0]["ts"] < anchor["ts"]
        if data.get("response_truncated") is True:
            stop = "response_truncated"
        elif phone_end:
            stop = "phone_reported_end"
        elif inaccessible:
            stop = "phone_reports_inaccessible"
        elif archive.anchors(chat)[0]["ts"] <= start:
            stop = "since_reached"
        elif not added or not older:
            stop = "no_progress"
        else:
            continue
        break
    coverage = archive.coverage(chat, stop, phone_end=phone_end, inaccessible=inaccessible)
    return {"status": "fetched", "requests": requests, "stored": total, "stop_reason": stop,
            "phone_reported_end": coverage["phone_reported_end"], "complete_history": False}
