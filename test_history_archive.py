"""Synthetic history only. Never load/connect an account or invoke a sender."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import whatsapp_cli as cli
import whatsapp_history as history

CHAT = "12025550123@s.whatsapp.net"
OTHER = "12025550124@s.whatsapp.net"
START = "2020-01-01T00:00:00Z"
END = "2020-01-03T00:00:00Z"
TS = 1577836800


class HistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.parent = Path(temporary.name).resolve()
        self.parent.chmod(0o700)
        self.store = self.parent / "account"
        cli.initialize_store(str(self.store))
        self.archive = history.Archive(cli, self.store)

    def row(self, name, ts=TS, text="fixture"):
        return {"id": name, "ts": ts, "from_me": False, "text": text}

    def test_pagination_older_than_31_days_and_other_chat_isolation(self):
        self.archive.record(CHAT, [self.row("A"), self.row("B"), self.row("C", TS + 1)])
        self.archive.record(OTHER, [self.row("OTHER", text="unrelated")])
        selected = history.scope(cli, CHAT, START, END, 1)
        first = self.archive.page(selected)
        self.assertEqual(first["messages"][0]["id"], "C")
        self.archive.record(CHAT, [self.row("NEW", TS + 2)])
        second = self.archive.page(selected, first["next_cursor"])
        third = self.archive.page(selected, second["next_cursor"])
        self.assertEqual([second["messages"][0]["id"], third["messages"][0]["id"]], ["B", "A"])
        self.assertIsNone(third["next_cursor"])
        self.assertFalse(third["complete_history"])

    def test_cursor_cannot_expand_chat_or_dates(self):
        self.archive.record(CHAT, [self.row("A"), self.row("B")])
        first = self.archive.page(history.scope(cli, CHAT, START, END, 1))
        for selected, cursor in [(history.scope(cli, OTHER, START, END, 1), first["next_cursor"]),
                                 (history.scope(cli, CHAT, START, END, 1), "bad!"),
                                 (history.scope(cli, CHAT, START, END, 1), "x" * 513)]:
            with self.assertRaises(cli.SafeError):
                self.archive.page(selected, cursor)

    def test_revocation_blocks_replayed_body(self):
        self.archive.record(CHAT, [self.row("A")])
        revoke = self.row("REV", TS + 1, None)
        revoke["revoke_id"] = "A"
        self.archive.record(CHAT, [revoke])
        self.archive.record(CHAT, [self.row("A", text="replayed")])
        self.assertEqual(self.archive.page(history.scope(cli, CHAT, START, END, 20))["messages"], [])

    def test_media_header_can_anchor_but_has_no_text(self):
        self.archive.record(CHAT, [self.row("MEDIA", text=None)])
        self.assertEqual(self.archive.anchors(CHAT)[0]["id"], "MEDIA")
        self.assertEqual(self.archive.page(history.scope(cli, CHAT, START, END, 20))["messages"], [])
        self.assertEqual(self.archive.coverage(CHAT)["text_messages"], 0)

    def test_duplicate_and_revoke_before_original(self):
        self.assertEqual(self.archive.record(CHAT, [self.row("A")]), 1)
        self.assertEqual(self.archive.record(CHAT, [self.row("A")]), 0)
        row = self.row("REVOKE", text=None)
        row["revoke_id"] = "LATE"
        self.archive.record(CHAT, [row])
        self.archive.record(CHAT, [self.row("LATE")])
        self.assertEqual([r["id"] for r in self.archive.page(history.scope(cli, CHAT, START, END, 20))["messages"]], ["A"])

    def test_unknown_schema_or_trigger_is_rejected(self):
        with closing(sqlite3.connect(self.archive.path)) as db:
            db.execute("PRAGMA user_version=77")
        self.archive.path.chmod(0o600)
        with self.assertRaises(ValueError):
            self.archive.record(CHAT, [self.row("A")])
        with closing(sqlite3.connect(self.archive.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 77)
        self.archive.path.unlink()
        self.archive.record(CHAT, [self.row("A")])
        with closing(sqlite3.connect(self.archive.path)) as db:
            db.execute("CREATE TRIGGER unexpected AFTER INSERT ON history_messages BEGIN SELECT 1; END")
        with self.assertRaises(ValueError):
            self.archive.coverage(CHAT)

    def test_symlink_and_unsafe_archive_are_rejected(self):
        outside = self.parent / "outside"
        outside.write_text("private")
        self.archive.path.symlink_to(outside)
        with self.assertRaises(cli.SafeError):
            self.archive.coverage(CHAT)
        self.assertEqual(outside.read_text(), "private")

    def test_dry_runs_do_not_inspect_store_or_spawn(self):
        with patch.object(cli, "history_module", side_effect=AssertionError), patch.object(cli, "run_bounded", side_effect=AssertionError):
            for command in ("fetch", "history-page", "history-status"):
                args = [command, "--store", "/does/not/exist", "--chat", CHAT]
                if command != "history-status":
                    args += ["--since", "1970-01-01T00:00:00Z", "--until", END]
                self.assertEqual(cli.main(args, stdout=io.StringIO(), stderr=io.StringIO()), 0)

    def test_empty_anchor_requires_no_backend_and_no_connection(self):
        with patch.object(cli, "session_identity", return_value="12025550123:1@s.whatsapp.net"), patch.object(history, "verified_bridge", side_effect=AssertionError):
            result = history.fetch(cli, str(self.store), history.scope(cli, CHAT, START, END, 20), pages=1, count=50, seconds=60)
        self.assertEqual(result["stop_reason"], "no_local_anchor")
        self.assertEqual(result["requests"], 0)
        self.assertFalse(self.archive.path.exists())

    def test_backfill_protocol_has_no_body_argv_and_truthful_end(self):
        self.archive.record(CHAT, [self.row("ANCHOR", TS + 100)])
        result = {"status": "received", "rows": [self.row("OLDER", TS + 10)], "phone_reported_end": True,
                  "phone_reports_inaccessible": False, "response_truncated": False}
        with patch.object(cli, "session_identity", return_value="12025550123:1@s.whatsapp.net"), patch.object(history, "verified_bridge", return_value=Path("/fixed/helper")), patch.object(cli, "run_bounded", return_value=(0, json.dumps(result).encode())) as run:
            fetched = history.fetch(cli, str(self.store), history.scope(cli, CHAT, START, END, 20), pages=5, count=50, seconds=60)
        args, timeout, request = run.call_args.args
        self.assertEqual(args, ["/fixed/helper"])
        self.assertNotIn("text", json.loads(request))
        self.assertEqual(json.loads(request)["chat"], CHAT)
        self.assertEqual(fetched["requests"], 1)
        self.assertEqual(fetched["stored"], 1)
        self.assertTrue(fetched["phone_reported_end"])
        self.assertFalse(fetched["complete_history"])

    def test_timeout_does_not_retry_or_claim_completeness(self):
        self.archive.record(CHAT, [self.row("ANCHOR", TS + 100)])
        with patch.object(cli, "session_identity", return_value="12025550123:1@s.whatsapp.net"), patch.object(history, "verified_bridge", return_value=Path("/fixed/helper")), patch.object(cli, "run_bounded", return_value=(0, b'{"status":"response_timeout"}')) as run:
            result = history.fetch(cli, str(self.store), history.scope(cli, CHAT, START, END, 20), pages=5, count=50, seconds=60)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(result["stop_reason"], "response_timeout")

    def test_explicit_index_import_bootstraps_only_selected_chat(self):
        with closing(sqlite3.connect(self.store / "messages.sqlite3")) as db, db:
            for jid, msg_id in [(CHAT, "KNOWN"), (OTHER, "FOREIGN")]:
                db.execute("INSERT INTO messages(chat_jid,msg_id,ts,from_me,text) VALUES (?,?,?,?,?)",
                           (jid, msg_id, TS + 100, 0, "cached fixture"))
        external = self.parent / "old-index.sqlite3"
        external.write_bytes((self.store / "messages.sqlite3").read_bytes())
        external.chmod(0o600)
        with closing(sqlite3.connect(self.store / "messages.sqlite3")) as db, db:
            db.execute("DELETE FROM messages")
        with patch.object(cli, "session_identity", return_value="12025550123:1@s.whatsapp.net"), patch.object(history, "verified_bridge", return_value=Path("/fixed/helper")), patch.object(cli, "run_bounded", return_value=(0, b'{"status":"response_timeout"}')):
            history.fetch(cli, str(self.store), history.scope(cli, CHAT, START, END, 20), pages=1, count=50, seconds=60, anchor_db=external)
        self.assertEqual(self.archive.anchors(CHAT)[0]["id"], "KNOWN")
        self.assertEqual(self.archive.anchors(OTHER), [])

    def test_unsafe_rows_cannot_insert_partial_batch(self):
        for invalid in [self.row("bad/id"), self.row("A", text="\x00"), dict(self.row("A"), from_me=1)]:
            with self.assertRaises((ValueError, cli.SafeError)):
                self.archive.record(CHAT, [self.row("VALID"), invalid])
        self.assertFalse(self.archive.path.exists())


if __name__ == "__main__":
    unittest.main()
