"""Neonize 0.5.2 wire units and metadata; fictional selected chats only."""
import json
import time
import types
from unittest import mock
import unittest

import whatsapp_backend as backend
import whatsapp_cli as cli
import whatsapp_history as history
from test_direct_backend import FakeMessage
import test_history_archive as fixtures

CHAT, OTHER = fixtures.CHAT, fixtures.OTHER


def event(identifier, chat, timestamp, message):
    user, server = chat.split("@")
    return types.SimpleNamespace(Info=types.SimpleNamespace(ID=identifier, Timestamp=timestamp,
        MessageSource=types.SimpleNamespace(Chat=types.SimpleNamespace(User=user, Server=server), IsFromMe=False, IsGroup=False)),
        Message=message, IsEphemeral=False, IsViewOnce=False, IsViewOnceV2=False,
        IsViewOnceV2Extension=False, IsEdit=False)


class WireFormatTests(unittest.TestCase):
    setUp = fixtures.HistoryTests.setUp

    def test_live_milliseconds_store_seconds_in_both_indexes(self):
        now = int(time.time())
        index = backend.TextIndex(self.store, CHAT, 20, archive=self.archive)
        index.live(None, event("LIVE_MS", CHAT, now * 1000 + 987, FakeMessage("new incoming")))
        self.assertFalse(index.failed)
        self.assertEqual(index.stored, 1)
        self.assertEqual(index.selected_live_events, 1)
        self.assertEqual(self.archive.anchors(CHAT)[0]["ts"], now)
        rows = cli.read_history(self.store / "messages.sqlite3", (CHAT, now - 1, now + 1, 20))
        self.assertEqual(rows[0]["timestamp"], cli.dt.datetime.fromtimestamp(now, cli.dt.timezone.utc).isoformat())

    def test_metadata_values_are_not_read_or_archived(self):
        class UnreadableMetadata:
            def __getattribute__(self, name):
                raise AssertionError("metadata value must not be inspected")
        message = FakeMessage("plain selected text", messageContextInfo=UnreadableMetadata())
        self.assertEqual(backend.plain_text(message), "plain selected text")
        now = int(time.time())
        index = backend.TextIndex(self.store, CHAT, 20, archive=self.archive)
        index.live(None, event("NORMAL_METADATA", CHAT, now * 1000, message))
        page = self.archive.page(history.scope(cli, CHAT, "1970-01-01T00:00:00Z", "2099-01-01T00:00:00Z", 20))
        self.assertEqual(page["messages"][0]["text"], "plain selected text")
        self.assertNotIn("messageContextInfo", json.dumps(page))

    def test_metadata_does_not_enable_media_or_wrappers(self):
        for extra in ("imageMessage", "ephemeralMessage", "viewOnceMessage", "editedMessage"):
            message = FakeMessage("must not expose", messageContextInfo=object(), **{extra:object()})
            self.assertIsNone(backend.plain_text(message))

    def test_history_remains_seconds_and_chat_scope_remains_exact(self):
        now = int(time.time())
        index = backend.TextIndex(self.store, CHAT, 20, archive=self.archive)
        def item(jid):
            return types.SimpleNamespace(message=types.SimpleNamespace(key=types.SimpleNamespace(ID="HISTORY_SEC", remoteJID=jid, fromMe=False),
                messageTimestamp=now, message=FakeMessage("historical text", messageContextInfo=object())))
        index.history(None, types.SimpleNamespace(Data=types.SimpleNamespace(conversations=[
            types.SimpleNamespace(ID=CHAT, ephemeralExpiration=0, messages=[item(CHAT), item(OTHER)])])))
        index.live(None, event("FOREIGN", OTHER, now * 1000, FakeMessage("unrelated")))
        self.assertEqual(self.archive.anchors(CHAT)[0]["ts"], now)
        self.assertEqual(index.selected_history_events, 1)
        self.assertEqual(index.selected_live_events, 0)
        self.assertEqual(self.archive.anchors(OTHER), [])

    def test_old_live_payload_can_enter_archive_without_rolling_cache(self):
        old = int(time.time()) - 90 * 86400
        index = backend.TextIndex(self.store, CHAT, 20, archive=self.archive)
        index.live(None, event("OLD_LIVE", CHAT, old * 1000, FakeMessage("older queued text")))
        self.assertFalse(index.failed)
        self.assertEqual(index.stored, 0)
        self.assertEqual(index.headers, 1)
        self.assertEqual(self.archive.anchors(CHAT)[0]["ts"], old)

    def test_invalid_live_units_do_not_archive_and_future_bounds_stay(self):
        for timestamp in (0, -1, True, "private", 1.5):
            index = backend.TextIndex(self.store, CHAT, 20, archive=self.archive)
            index.live(None, event("INVALID", CHAT, timestamp, FakeMessage("bad")))
            self.assertEqual(index.headers, 0)
        index = backend.TextIndex(self.store, CHAT, 20, archive=self.archive)
        index.live(None, event("FUTURE", CHAT, (int(time.time()) + 3600) * 1000, FakeMessage("future")))
        self.assertTrue(index.failed)
        self.assertEqual(self.archive.anchors(CHAT), [])

    def test_sync_diagnostics_are_validated_without_private_details(self):
        for values, valid in [({"selected_live_events": 0, "selected_history_events": 1, "archived_headers": 1}, True),
                              ({"selected_live_events": True}, False), ({"selected_history_events": 10001}, False),
                              ({"archived_headers": 201}, False)]:
            response = {"status":"synced", "stored":0, **values}
            with mock.patch.object(cli, "session_identity", return_value="12025550123:1@s.whatsapp.net"), mock.patch.object(cli, "run_bounded", return_value=(0,json.dumps(response).encode())):
                if valid:
                    result=cli.backend_operation("sync", str(self.store), jid=CHAT)
                    self.assertEqual(result["selected_history_events"],1)
                else:
                    with self.assertRaises(cli.SafeError):cli.backend_operation("sync", str(self.store), jid=CHAT)


if __name__ == "__main__":
    unittest.main()
