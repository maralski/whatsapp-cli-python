"""Scoped synthetic refresh results only; no account or network access."""
from contextlib import closing
import io
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

import whatsapp_cli as cli
import whatsapp_history as history
from test_history_archive import HistoryTests, CHAT, OTHER


class RefreshTests(unittest.TestCase):
    setUp = HistoryTests.setUp
    row = HistoryTests.row

    def result(self, rows=None):
        return {"status": "refreshed", "rows": [] if rows is None else rows, "response_truncated": False,
                "refresh": {"selected_live_events": 0, "selected_history_events": 0, "selected_undecryptable_events": 0,
                            "recovery_requests": 0, "recovery_failures": 0, "history_downloads": 0,
                            "history_download_failures": 0, "presence_failures": 0, "offline_replay_announced": True,
                            "offline_replay_completed": True, "presence_available_sent": True, "presence_unavailable_sent": True}}

    def invoke(self, data):
        with patch.object(cli, "session_identity", return_value="12025550125:1@s.whatsapp.net"), \
                patch.object(history, "verified_bridge", return_value=self.parent / "synthetic-helper"), \
                patch.object(cli, "run_bounded", return_value=(0, json.dumps(data).encode())) as run:
            result = history.refresh(cli, str(self.store), CHAT, seconds=30, limit=20)
        return result, run

    def test_refresh_has_no_anchor_human_body_or_argv_identifiers(self):
        row = self.row("REAL_SELECTED_ID", ts=int(time.time())-1, text="fresh fixture")
        result, run = self.invoke(self.result([row]))
        args, timeout, encoded = run.call_args.args
        self.assertEqual(args, [str(self.parent / "synthetic-helper")])
        request = json.loads(encoded)
        self.assertEqual(request["mode"], "refresh")
        self.assertEqual(request["chat"], CHAT)
        self.assertNotIn("anchor", request)
        self.assertNotIn("text", request)
        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["archived_headers"], 1)
        self.assertFalse(result["complete_history"])
        self.assertFalse(result["recent_coverage_verified"])
        with closing(sqlite3.connect(self.store / "messages.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT chat_jid,text FROM messages").fetchall(), [(CHAT, "fresh fixture")])
        self.assertEqual(self.archive.anchors(OTHER), [])

    def test_empty_replay_completion_never_certifies_latest(self):
        result, run = self.invoke(self.result())
        self.assertTrue(result["offline_replay_completed"])
        self.assertEqual(result["stored"], 0)
        self.assertFalse(result["recent_coverage_verified"])
        self.assertFalse(result["complete_history"])
        self.assertEqual(run.call_count, 1)

    def test_refresh_dry_run_does_not_read_or_load_transport(self):
        with patch.object(cli, "history_module", side_effect=AssertionError), patch.object(cli, "backend_operation", side_effect=AssertionError):
            output = io.StringIO()
            code = cli.main(["sync", "--refresh", "--store", "/does/not/exist", "--chat", CHAT], stdout=output, stderr=io.StringIO())
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["announces_online_presence"])

    def test_refresh_rejects_unsafe_rows_and_untrusted_diagnostics(self):
        bad = [self.result([self.row("bad/id")]), self.result([self.row("ID", text="\x00")])]
        invalid_counter = self.result()
        invalid_counter["refresh"]["recovery_requests"] = True
        bad.append(invalid_counter)
        unknown = self.result()
        unknown["refresh"]["raw_error"] = "synthetic-private-value"
        bad.append(unknown)
        for data in bad:
            with self.assertRaises((ValueError, cli.SafeError)):
                self.invoke(data)
        self.assertFalse(self.archive.path.exists())

    def test_refresh_backend_error_is_redacted_and_never_retried(self):
        with patch.object(cli, "session_identity", return_value="12025550125:1@s.whatsapp.net"), \
                patch.object(history, "verified_bridge", return_value=self.parent / "synthetic-helper"), \
                patch.object(cli, "run_bounded", return_value=(0, b'{"error":"synthetic-private-value"}')) as run:
            with self.assertRaises(cli.SafeError) as error:
                history.refresh(cli, str(self.store), CHAT, seconds=30, limit=20)
        self.assertEqual(error.exception.code, "sync_failed")
        self.assertNotIn("synthetic-private-value", str(error.exception))
        self.assertEqual(run.call_count, 1)

    def test_missing_helper_does_not_fallback_or_build(self):
        with patch.object(cli, "session_identity", return_value="12025550125:1@s.whatsapp.net"), \
                patch.object(history, "verified_bridge", side_effect=ValueError), patch.object(cli, "run_bounded", side_effect=AssertionError):
            with self.assertRaises(cli.SafeError) as error:
                history.refresh(cli, str(self.store), CHAT, seconds=30, limit=20)
        self.assertEqual(error.exception.code, "history_backend_unavailable")

    def test_account_change_blocks_indexing(self):
        with patch.object(cli, "session_identity", side_effect=["12025550125:1@s.whatsapp.net", "12025550126:1@s.whatsapp.net"]), \
                patch.object(history, "verified_bridge", return_value=self.parent / "synthetic-helper"), \
                patch.object(cli, "run_bounded", return_value=(0, json.dumps(self.result([self.row("REAL_ID")])).encode())):
            with self.assertRaises(cli.SafeError) as error:
                history.refresh(cli, str(self.store), CHAT, seconds=30, limit=20)
        self.assertEqual(error.exception.code, "account_changed")
        self.assertFalse(self.archive.path.exists())

    def test_revocation_preserves_tombstones_in_both_indexes(self):
        now = int(time.time())-1
        self.invoke(self.result([self.row("ORIGINAL", now, "private fixture")]))
        revoke = self.row("REVOKE", now, None)
        revoke["revoke_id"] = "ORIGINAL"
        self.invoke(self.result([revoke]))
        self.invoke(self.result([self.row("ORIGINAL", now, "replayed fixture")]))
        with closing(sqlite3.connect(self.store / "messages.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT text,revoked FROM messages WHERE msg_id='ORIGINAL'").fetchone(), (None, 1))
        with closing(self.archive.open()) as db:
            self.assertEqual(db.execute("SELECT text,revoked FROM history_messages WHERE id='ORIGINAL'").fetchone(), (None, 1))


# Avoid rediscovering the imported archive fixture class.
del HistoryTests
