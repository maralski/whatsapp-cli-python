"""Isolated, text-only direct protocol worker. Never invoke other WhatsApp CLIs."""
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import json
import logging
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys
import threading
import time
import types
from contextlib import closing

VERSION = "0.5.2"
NATIVE_HASHES = {
    ("Darwin", "arm64"): ("neonize-darwin-arm64.dylib", "214abb402cd995bf746a3ed1f033248403dd0c59289231b239cb7f1daf991f59"),
    ("Darwin", "x86_64"): ("neonize-darwin-amd64.dylib", "e22c928b88a9c5bc8166d5a41b5357526e9014ef0acfd63899f4460729244b37"),
    ("Linux", "x86_64"): ("neonize-linux-amd64.so", "56b0636515068e8f8383e5448b6cfd2516ea923fa6c68ce4a2e516da96ec405d"),
    ("Linux", "aarch64"): ("neonize-linux-arm64.so", "25ea97e7c7933396a655e00a716f79da9095500cfb1cac053cb3addd82bd453c"),
}
# Generated from requirements.lock. Drift requires an explicit reviewed update.
PINNED_PACKAGES = {'anyio': '4.15.1', 'beautifulsoup4': '4.15.0', 'certifi': '2026.7.22', 'charset-normalizer': '3.5.2', 'h11': '0.16.0', 'httpcore': '1.0.9', 'httpx': '0.28.1', 'idna': '3.20', 'linkpreview': '0.12.1', 'neonize': '0.5.2', 'phonenumbers': '9.0.40', 'pillow': '12.3.0', 'protobuf': '7.36.2', 'python-magic': '0.4.27', 'requests': '2.34.2', 'segno': '1.6.6', 'soupsieve': '2.10', 'tqdm': '4.70.1', 'typing-extensions': '4.16.0', 'urllib3': '2.8.0'}


def local_cli():
    spec = importlib.util.spec_from_file_location("whatsapp_cli_private", Path(__file__).with_name("whatsapp_cli.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def disabled(*args, **kwargs):
    raise RuntimeError("Runtime downloads and media operations are disabled")


def load_protocol(cli):
    """Fail closed BEFORE importing Neonize's eager native/download loader."""
    for name, version in PINNED_PACKAGES.items():
        if importlib.metadata.version(name) != version:
            raise RuntimeError("Dependency version differs from the reviewed lock")
    filename, expected = NATIVE_HASHES[(platform.system(), platform.machine())]
    distribution = importlib.metadata.distribution("neonize")
    native = Path(distribution.locate_file("neonize")) / filename
    path, before = cli.checked_path(native)
    if not 0 < before.st_size <= 64 * 1024 * 1024:
        raise RuntimeError("Native library size invalid")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise RuntimeError("Native library changed")
        digest = hashlib.file_digest(source, "sha256").hexdigest() if hasattr(hashlib, "file_digest") else hashlib.sha256(source.read(64 * 1024 * 1024 + 1)).hexdigest()
        after = os.fstat(source.fileno())
    latest = path.lstat()
    if digest != expected or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) or (latest.st_ino, latest.st_size, latest.st_mtime_ns, latest.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise RuntimeError("Native library verification failed")
    if any(name.startswith("neonize") for name in sys.modules):
        raise RuntimeError("Protocol must be loaded only through the guarded worker")
    downloader = types.ModuleType("neonize.download")
    downloader.__GONEONIZE_VERSION__ = VERSION
    downloader.download = disabled
    # These shims precede __init__.py; importing download first would already load C.
    sys.modules["neonize.download"] = downloader
    platform_module = types.ModuleType("neonize.utils.platform")
    platform_module.generated_name = lambda *args, **kwargs: filename
    platform_module.is_executable_installed = lambda *args, **kwargs: False
    sys.modules["neonize.utils.platform"] = platform_module
    # Text operations do not need libmagic. Media calls are deliberately unavailable.
    media = types.ModuleType("magic")
    media.from_file = media.from_buffer = disabled
    sys.modules["magic"] = media
    from neonize.client import NewClient
    from neonize.events import ConnectedEv, HistorySyncEv, LoggedOutEv, MessageEv
    from neonize.proto.Neonize_pb2 import JID
    from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
    return types.SimpleNamespace(NewClient=NewClient, ConnectedEv=ConnectedEv,
                                 HistorySyncEv=HistorySyncEv, LoggedOutEv=LoggedOutEv,
                                 MessageEv=MessageEv, JID=JID, Message=Message)


def plain_text(message):
    # Do not unpack disappearing/view-once/media/edited payloads or index them.
    fields = {descriptor.name for descriptor, value in message.ListFields()}
    if fields == {"conversation"}:
        return message.conversation
    if fields == {"extendedTextMessage"}:
        context = message.extendedTextMessage.contextInfo
        if context.expiration or context.ephemeralSettingTimestamp:
            return None
        return message.extendedTextMessage.text
    return None


class TextIndex:
    """Bounded selected-chat cache; no credentials, contact enumeration or media."""
    def __init__(self, store, chat, limit):
        self.path = store / "messages.sqlite3"
        self.chat = chat
        self.limit = limit
        self.stored = 0
        self.failed = False
        self.lock = threading.Lock()

    def record(self, msg_id, timestamp, outgoing, text):
        if not isinstance(msg_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", msg_id):
            return
        if not isinstance(text, str) or not text.strip() or "\x00" in text or not 0 < timestamp <= int(time.time()) + 300 or timestamp < int(time.time()) - 31 * 86400:
            return
        with self.lock:
            if self.stored >= self.limit:
                return
            try:
                with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                    cursor = db.execute("INSERT OR IGNORE INTO messages(chat_jid,msg_id,ts,from_me,text) VALUES (?,?,?,?,?)", (self.chat, msg_id, timestamp, int(bool(outgoing)), text[:10000]))
                    self.stored += cursor.rowcount
                    db.execute("DELETE FROM messages WHERE ts<?", (int(time.time()) - 31 * 86400,))
                    db.execute("DELETE FROM messages WHERE rowid IN (SELECT rowid FROM messages ORDER BY ts DESC,rowid DESC LIMIT -1 OFFSET 10000)")
            except (sqlite3.Error, ValueError, UnicodeError):
                self.failed = True

    def revoke(self, msg_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", msg_id):
            return
        with self.lock:
            try:
                with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                    db.execute("INSERT INTO messages(chat_jid,msg_id,ts,from_me,text,revoked) VALUES (?,?,?,0,NULL,1) ON CONFLICT(chat_jid,msg_id) DO UPDATE SET revoked=1,text=NULL", (self.chat, msg_id, int(time.time())))
                    db.execute("DELETE FROM messages WHERE ts<?", (int(time.time()) - 31 * 86400,))
                    db.execute("DELETE FROM messages WHERE rowid IN (SELECT rowid FROM messages ORDER BY ts DESC,rowid DESC LIMIT -1 OFFSET 10000)")
            except sqlite3.Error:
                self.failed = True

    def payload(self, msg_id, timestamp, outgoing, message):
        if message.HasField("protocolMessage"):
            protocol = message.protocolMessage
            if protocol.type == 0 and protocol.key.remoteJID in ("", self.chat):
                self.revoke(protocol.key.ID)
            return
        self.record(msg_id, timestamp, outgoing, plain_text(message))

    def live(self, client, event):
        source = event.Info.MessageSource
        if source.IsGroup or any((event.IsEphemeral, event.IsViewOnce, event.IsViewOnceV2, event.IsViewOnceV2Extension, event.IsEdit)):
            return
        chat = source.Chat.User + "@" + source.Chat.Server
        # Never infer that an unrelated LID maps to the selected phone.
        if chat == self.chat:
            self.payload(event.Info.ID, event.Info.Timestamp, source.IsFromMe, event.Message)

    def history(self, client, event):
        scanned = 0
        for conversation in event.Data.conversations:
            if conversation.ID != self.chat or conversation.ephemeralExpiration:
                continue
            for item in conversation.messages:
                scanned += 1
                if scanned > 10000 or self.stored >= self.limit:
                    return
                msg = item.message
                if msg.key.remoteJID != self.chat:
                    continue
                self.payload(msg.key.ID, msg.messageTimestamp, msg.key.fromMe, msg.message)


def operate(request, cli, protocol):
    command = request["command"]
    store = cli.verify_store(request["store"])
    identity = cli.session_identity(store)
    if identity != request["identity"] or (command == "pair") != (identity is None):
        return {"error": "pair_required"}
    client_jid = None
    if identity:
        user_device, server = identity.split("@")
        user, device = user_device.split(":")
        client_jid = protocol.JID(User=user, Device=int(device), Server=server, IsEmpty=False, RawAgent=0, Integrator=0)
    client = protocol.NewClient(str(store / "session.db"), jid=client_jid)
    connected, finished, unsafe_auth = threading.Event(), threading.Event(), threading.Event()
    client.event(protocol.ConnectedEv)(lambda client, event: connected.set())
    client.event(protocol.LoggedOutEv)(lambda client, event: unsafe_auth.set())

    def qr(client, data):
        if command != "pair":
            unsafe_auth.set()
            return
        import segno
        try:
            with open("/dev/tty", "w") as terminal:
                if not terminal.isatty():
                    raise OSError()
                segno.make_qr(data).terminal(out=terminal, compact=True)
        except Exception:
            unsafe_auth.set()
    client.event.qr(qr)
    index = TextIndex(store, request["jid"], request["limit"]) if command in ("send", "sync") else None
    if command == "sync":
        client.event(protocol.MessageEv)(index.live)
        client.event(protocol.HistorySyncEv)(index.history)

    def connect():
        try:
            client.connect()
        except Exception:
            unsafe_auth.set()
        finally:
            finished.set()
    thread = threading.Thread(target=connect, daemon=True)
    thread.start()
    deadline = time.monotonic() + (120 if command == "pair" else 20)
    try:
        while not connected.wait(0.1):
            if unsafe_auth.is_set() or finished.is_set() or time.monotonic() >= deadline:
                return {"error": "not_connected"}
        if unsafe_auth.is_set():
            return {"error": "pair_required"}
        if command == "pair":
            if cli.session_identity(store) is None:
                return {"error": "pair_required"}
            return {"status": "linked"}
        me = client.get_me().JID
        if me.User + ":" + str(me.Device) + "@" + me.Server != identity:
            return {"error": "pair_required"}
        if command == "sync":
            deadline = time.monotonic() + request["seconds"]
            while time.monotonic() < deadline and not unsafe_auth.is_set() and not finished.is_set():
                time.sleep(0.1)
            if index.failed or unsafe_auth.is_set() or finished.is_set():
                return {"error": "sync_failed"}
            return {"status": "synced", "stored": index.stored, "complete_history": False}
        recipient = cli.phone_jid(request["jid"])
        if me.User + "@" + me.Server == recipient and request.get("allow_self") is not True:
            return {"error": "pair_required"}
        cli.validate_text(request["text"])
        user, server = recipient.split("@")
        response = client.send_message(protocol.JID(User=user, Server=server, IsEmpty=False, RawAgent=0, Device=0, Integrator=0), protocol.Message(conversation=request["text"]), link_preview=False)
        if not re.fullmatch(cli.ID_PATTERN, response.ID):
            return {"error": "send_unknown"}
        # Acceptance remains true if only our local cache write failed.
        index.record(response.ID, int(time.time()), True, request["text"])
        return {"status": "accepted", "message_id": response.ID, "delivery_confirmed": False, "local_store_warning": index.failed}
    finally:
        client.stop()
        thread.join(timeout=2)


def main():
    # Preserve a dedicated result pipe; suppress Python AND native fd-level logs.
    result_fd = os.dup(1)
    os.set_inheritable(result_fd, False)
    with open(os.devnull, "wb") as quiet:
        os.dup2(quiet.fileno(), 1)
        os.dup2(quiet.fileno(), 2)
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    result = {"error": "backend_unavailable"}
    try:
        raw = sys.stdin.buffer.read(250001)
        if len(raw) > 250000:
            raise ValueError()
        request = json.loads(raw)
        if request["command"] not in ("send", "pair", "sync") or not 1 <= request["seconds"] <= 120 or not 1 <= request["limit"] <= 200:
            raise ValueError()
        cli = local_cli()
        store = cli.verify_store(request["store"])
        fd = os.open(store / ".cli.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                result = {"error": "backend_busy"}
            else:
                protocol = load_protocol(cli)
                result = operate(request, cli, protocol)
    except Exception:
        # Even malformed requests and native exceptions must not echo private data.
        result = {"error": "send_unknown" if isinstance(locals().get("request"), dict) and request.get("command") == "send" else "backend_unavailable"}
    encoded = json.dumps(result, ensure_ascii=True).encode() + b"\n"
    os.write(result_fd, encoded)
    os.close(result_fd)
    # Native threads can remain blocked: the parent owns process-group cleanup.
    os._exit(0)


if __name__ == "__main__":
    main()
