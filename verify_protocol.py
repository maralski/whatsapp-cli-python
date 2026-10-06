"""Offline installed-wheel/protobuf smoke check. Never construct/connect a client."""
import importlib.util
from pathlib import Path
import tempfile
import time
import sqlite3
from contextlib import closing

spec = importlib.util.spec_from_file_location("reviewed_worker", Path(__file__).with_name("whatsapp_backend.py"))
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)
cli = backend.local_cli()
protocol = backend.load_protocol(cli)
from neonize.proto.Neonize_pb2 import HistorySync, Message, MessageInfo, MessageSource
from neonize.proto.waHistorySync.WAWebProtobufsHistorySync_pb2 import HistorySync as HistoryData, Conversation, HistorySyncMsg
from neonize.proto.waWeb.WAWebProtobufsWeb_pb2 import WebMessageInfo
from neonize.proto.waCommon.WACommon_pb2 import MessageKey

jid = "12025550123@s.whatsapp.net"
now = int(time.time())
with tempfile.TemporaryDirectory() as temporary:
    store = Path(temporary).resolve() / "synthetic-store"
    cli.initialize_store(str(store))
    index = backend.TextIndex(store, jid, 20)
    index.live(None, Message(Info=MessageInfo(ID="LIVE", Timestamp=now,
        MessageSource=MessageSource(Chat=protocol.JID(User="12025550123", Server="s.whatsapp.net"), IsFromMe=False, IsGroup=False)), Message=protocol.Message(conversation="synthetic live text")))
    index.history(None, HistorySync(Data=HistoryData(conversations=[Conversation(ID=jid, messages=[HistorySyncMsg(message=WebMessageInfo(key=MessageKey(ID="HISTORY", remoteJID=jid, fromMe=True), messageTimestamp=now, message=protocol.Message(conversation="synthetic history text")))])])))
    index.payload("REVOKE", now, False, protocol.Message(protocolMessage={"type": 0, "key": {"ID": "LIVE", "remoteJID": jid}}))
    assert index.stored == 2 and not index.failed
    rows = cli.read_history(store / "messages.sqlite3", (jid, now - 1, now + 1, 20))
    assert len(rows) == 1 and rows[0]["id"] == "HISTORY"
    lid = "76543210987654321@lid"
    with closing(sqlite3.connect(store / "session.db")) as db, db:
        db.execute("CREATE TABLE whatsmeow_lid_map(lid TEXT,pn TEXT)")
        db.execute("INSERT INTO whatsmeow_lid_map VALUES (?,?)", (lid, jid))
    alias_index = backend.TextIndex(store, jid, 20, aliases=backend.chat_aliases(store, jid))
    alias_index.live(None, Message(Info=MessageInfo(ID="LID_LIVE", Timestamp=now,
        MessageSource=MessageSource(Chat=protocol.JID(User=lid.split("@")[0], Server="lid"), IsFromMe=False, IsGroup=False)), Message=protocol.Message(conversation="synthetic LID text")))
    alias_index.history(None, HistorySync(Data=HistoryData(conversations=[Conversation(ID=lid, messages=[HistorySyncMsg(message=WebMessageInfo(key=MessageKey(ID="LID_HISTORY", remoteJID=lid, fromMe=False), messageTimestamp=now, message=protocol.Message(conversation="synthetic LID history")))])])))
    assert alias_index.stored == 2 and not alias_index.failed
    alias_index.payload("LID_REVOKE", now, False, protocol.Message(protocolMessage={"type": 0, "key": {"ID": "LID_HISTORY", "remoteJID": lid}}))
    rows = cli.read_history(store / "messages.sqlite3", (jid, now - 1, now + 1, 20))
    assert {row["id"] for row in rows} == {"HISTORY", "LID_LIVE"}
    archive = cli.history_module().Archive(cli, store)
    archived_index = backend.TextIndex(store, jid, 2, archive=archive)
    archived_index.payload("OLD_ARCHIVE", now - 90 * 86400, False, protocol.Message(conversation="synthetic older text"))
    archived_index.payload("MEDIA_ANCHOR", now, False, protocol.Message(imageMessage={"caption": "never archive media caption"}))
    archived_index.payload("OVER_HEADER_BUDGET", now, False, protocol.Message(conversation="must not enter archive"))
    assert not archived_index.failed and archived_index.headers == 2
    coverage = archive.coverage(jid)
    assert coverage["headers"] == 2 and coverage["text_messages"] == 1
    assert archive.anchors(jid)[0]["id"] == "OLD_ARCHIVE"
    assert cli.session_identity(store) is None
    assert not (store / ".cli.lock").exists()
assert backend.disabled is __import__("neonize.download", fromlist=["download"]).download
try:
    __import__("neonize.download", fromlist=["download"]).download()
except RuntimeError:
    pass
else:
    raise AssertionError("Runtime download enabled")
print("Verified native import, real protobuf event parsing, scoped indexing, revocation, and disabled runtime downloads; no account connection.")
