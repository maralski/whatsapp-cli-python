"""Offline installed-wheel/protobuf smoke check. Never construct/connect a client."""
import importlib.util
from pathlib import Path
import tempfile
import time

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
