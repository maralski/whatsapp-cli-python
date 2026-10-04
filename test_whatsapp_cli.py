"""Synthetic fixtures only: no real WhatsApp stores, accounts, or sends."""

from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

import whatsapp_cli as cli

PHONE = "+12025550123"  # Fictional NANP example number.
JID = "12025550123@s.whatsapp.net"
OTHER = "12025550124@s.whatsapp.net"
START = "2026-01-01T00:00:00Z"
END = "2026-01-02T00:00:00Z"
SECOND = 1767225600
SECRET = "synthetic-private-value"


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # macOS /var and /tmp are symlinks. Fixtures use their direct paths.
        self.root = Path(self.temporary.name).resolve()
        self.root.chmod(0o700)
        self.database = self.root / "messages.sqlite3"

    def make_index(self, path=None):
        path = self.database if path is None else path
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE messages (chat_jid TEXT, msg_id TEXT, ts INTEGER, from_me INTEGER, text TEXT, revoked INTEGER DEFAULT 0, deleted_for_me INTEGER DEFAULT 0, payload_purged_at INTEGER)")
            connection.execute("CREATE INDEX idx_messages_chat_ts ON messages(chat_jid, ts)")
        path.chmod(0o600)
        return path

    def add_message(self, jid=JID, msg_id="ABC123", when=SECOND, outgoing=0, text="fixture", revoked=0, deleted=0, purged=None):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)", (jid, msg_id, when, outgoing, text, revoked, deleted, purged))

    def scope(self, **changes):
        values = dict(chat=JID, since=START, until=END, limit=20)
        values.update(changes)
        return cli.history_scope(**values)

    def store(self):
        root = self.root / "store"
        root.mkdir(mode=0o700)
        for name in ("session.db", "messages.sqlite3"):
            (root / name).write_bytes(b"synthetic fixture; no account credentials")
            (root / name).chmod(0o600)
        return root

    def binary(self):
        binary = self.root / "backend"
        binary.write_bytes(b"synthetic non-runnable backend fixture")
        binary.chmod(0o700)
        return binary, hashlib.sha256(binary.read_bytes()).hexdigest()

    def call(self, arguments, body=b"Example text"):
        output, error = io.StringIO(), io.StringIO()
        code = cli.main(arguments, stdin=io.BytesIO(body), stdout=output, stderr=error)
        return code, output.getvalue(), error.getvalue()


class ValidationTests(Fixtures):
    def test_exact_phone_only(self):
        self.assertEqual(cli.phone_jid(PHONE), JID)
        self.assertEqual(cli.phone_jid(JID), JID)
        for bad in ("Example Person", "person", "12025550123", "+01234567", "+123", "+" + "1" * 16,
                    "1234567@g.us", "1234567@lid", "status@broadcast", "1:2@s.whatsapp.net",
                    JID + "\n", PHONE + " --to other", "$(touch /tmp/no)", "１２３４５６７@s.whatsapp.net"):
            with self.subTest(value=bad), self.assertRaises(cli.SafeError):
                cli.phone_jid(bad)

    def test_scope_bounds_and_timezone(self):
        for changes in (dict(limit=0), dict(limit=201), dict(until=START), dict(since=END),
                        dict(until="2026-02-02T00:00:00Z"), dict(since="2026-01-01T00:00:00"),
                        dict(since="not-a-date"), dict(since="1969-12-31T00:00:00Z")):
            with self.subTest(changes=changes), self.assertRaises(cli.SafeError):
                self.scope(**changes)
        self.assertEqual(self.scope(since="2026-01-01T11:00:00+11:00")[1], SECOND)
        self.assertEqual(self.scope(since="2026-01-01T00:00:00.001Z")[1], SECOND + 1)

    def test_input_bounds_unicode_and_controls(self):
        body = 'Unicode 👋\n"quotes" $(no shell) `no code` \\literal'
        self.assertEqual(cli.input_body(io.BytesIO(body.encode())), body)
        self.assertEqual(len(cli.input_body(io.BytesIO(("👋" * 10000).encode()))), 10000)
        for data in (b"", b" \n", b"\xff", b"a\x00b", b"a" * 10001, b"a" * 40001):
            with self.subTest(size=len(data)), self.assertRaises(cli.SafeError):
                cli.input_body(io.BytesIO(data))

    def test_input_read_is_bounded(self):
        stream = mock.Mock()
        stream.isatty.return_value = False
        stream.read.return_value = b"valid"
        self.assertEqual(cli.input_body(stream), "valid")
        stream.read.assert_called_once_with(40001)
        stream.isatty.return_value = True
        with self.assertRaises(cli.SafeError):
            cli.input_body(stream)

    def test_path_lexical_validation(self):
        for path in ("relative.db", "/a/../b", "https://host/file", "/a\nfile", "/a\x00file", ""):
            with self.subTest(path=path), self.assertRaises(cli.SafeError):
                cli.local_path(path)
        self.assertEqual(cli.local_path("/a ? #/wacli.db"), Path("/a ? #/wacli.db"))

    def test_dry_runs_do_not_touch_files_or_backend(self):
        with mock.patch.object(cli, "checked_path", side_effect=AssertionError("Filesystem inspected")), \
             mock.patch.object(cli.sqlite3, "connect", side_effect=AssertionError("DB opened")), \
             mock.patch.object(cli, "run_bounded", side_effect=AssertionError("Child launched")):
            code, out, err = self.call(["history", "--db", "/nonexistent/" + SECRET, "--chat", PHONE, "--since", START, "--until", END])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["status"], "dry_run")
            code, out, err = self.call(["send", "--store", "/nonexistent/" + SECRET, "--to", PHONE], SECRET.encode())
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(out)["no_preview"])
            self.assertNotIn(SECRET, out + err)
            self.assertNotIn(PHONE, out + err)

    def test_parser_redacts_unknown_values(self):
        for arguments in (["send", "--to", SECRET], ["history", "--db", "/" + SECRET, "--chat", PHONE, "--since", START, "--until", END, "--limit", SECRET],
                          ["send", "--store", "/" + SECRET, "--to", PHONE, "--exec"], ["send", "--store", "/" + SECRET, "--to", PHONE, "--attach", SECRET]):
            code, out, err = self.call(arguments)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertNotIn(SECRET, err)
            self.assertNotIn(PHONE, err)



class HistoryTests(Fixtures):
    def test_single_chat_dates_order_limit_and_deleted_filter(self):
        self.make_index()
        for key, offset in (("before", -1), ("start", 0), ("second", 1), ("end", 86400)):
            self.add_message(msg_id=key, when=SECOND + offset)
        self.add_message(jid=OTHER, msg_id="other")
        self.add_message(msg_id="revoked", revoked=1)
        self.add_message(msg_id="deleted", deleted=1)
        self.add_message(msg_id="purged", purged=SECOND)
        rows = cli.read_history(self.database, self.scope())
        self.assertEqual([row["id"] for row in rows], ["second", "start"])
        self.assertEqual(len(cli.read_history(self.database, self.scope(limit=1))), 1)

    def test_null_truncation_unicode_and_sent_direction(self):
        self.make_index()
        self.add_message(msg_id="null", text=None)
        self.add_message(msg_id="long", text="👋" * 10001, outgoing=1)
        rows = cli.read_history(self.database, self.scope())
        self.assertEqual(len(rows[0]["text"]), 10000)
        self.assertTrue(rows[0]["text_truncated"])
        self.assertEqual(rows[0]["direction"], "sent")
        self.assertIsNone(rows[1]["text"])
        self.assertFalse(rows[1]["text_available"])

    def test_fractional_date_bounds(self):
        self.make_index()
        self.add_message(msg_id="zero", when=SECOND)
        self.add_message(msg_id="one", when=SECOND + 1)
        rows = cli.read_history(self.database, self.scope(since="2026-01-01T00:00:00.001Z", until="2026-01-01T00:00:01.001Z"))
        self.assertEqual([row["id"] for row in rows], ["one"])

    def test_missing_database_not_created(self):
        with self.assertRaises(cli.SafeError):
            cli.read_history(self.database, self.scope())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_read_does_not_change_index_contents(self):
        self.make_index()
        self.add_message()
        digest = hashlib.sha256(self.database.read_bytes()).digest()
        cli.read_history(self.database, self.scope())
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).digest(), digest)
        self.assertEqual({p.name for p in self.root.iterdir()}, {"messages.sqlite3"})

    def test_uri_path_with_question_and_hash(self):
        folder = self.root / "fixture ? #"
        folder.mkdir(mode=0o700)
        self.database = folder / "messages.sqlite3"
        self.make_index()
        self.add_message()
        self.assertEqual(len(cli.read_history(self.database, self.scope())), 1)

    def test_sqlite_sidecar_symlink_rejected(self):
        self.make_index()
        sidecar = Path(str(self.database) + "-wal")
        sidecar.symlink_to(self.database)
        with self.assertRaises(cli.SafeError):
            cli.read_history(self.database, self.scope())

    def test_wal_reads_include_committed_history(self):
        self.make_index()
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO messages(chat_jid,msg_id,ts,from_me,text) VALUES (?,?,?,?,?)", (JID, "wal", SECOND, 0, "committed WAL fixture"))
            writer.commit()
            before = hashlib.sha256(self.database.read_bytes()).digest()
            rows = cli.read_history(self.database, self.scope())
            self.assertEqual(rows[0]["id"], "wal")
            self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).digest())

    def test_view_cannot_read_other_tables(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE credentials (value TEXT)")
            connection.execute("INSERT INTO credentials VALUES (?)", (SECRET,))
            connection.execute("CREATE VIEW messages AS SELECT value AS text FROM credentials")
        with self.assertRaises(cli.SafeError):
            cli.read_history(self.database, self.scope())

    def test_generated_column_and_malformed_schema_rejected(self):
        for schema in ("CREATE TABLE messages (text TEXT)",
                       "CREATE TABLE messages (chat_jid TEXT, msg_id TEXT, ts INTEGER, from_me INTEGER, text TEXT GENERATED ALWAYS AS ('private'), revoked INTEGER, deleted_for_me INTEGER, payload_purged_at INTEGER)"):
            with self.subTest(schema=schema):
                if self.database.exists():
                    self.database.unlink()
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute(schema)
                with self.assertRaises(cli.SafeError):
                    cli.read_history(self.database, self.scope())

    def test_blob_bad_id_and_invalid_direction_fail_redacted(self):
        self.make_index()
        for changes in (dict(text=b"blob"), dict(msg_id=SECRET + "\x1b"), dict(outgoing=9)):
            with self.subTest(changes=changes):
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute("DELETE FROM messages")
                self.add_message(**changes)
                code, out, err = self.call(["history", "--db", str(self.database), "--chat", PHONE, "--since", START, "--until", END, "--execute"])
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertNotIn(SECRET, err)
                self.assertNotIn(str(self.root), err)

    def test_history_timeout_fails_closed(self):
        self.make_index()
        with mock.patch.object(cli.time, "monotonic", side_effect=[0, 10] + [10] * 100), self.assertRaises(cli.SafeError):
            # Enough rows to trigger a progress callback.
            with closing(sqlite3.connect(self.database)) as connection, connection:
                connection.executemany("INSERT INTO messages(chat_jid,msg_id,ts,from_me,text) VALUES (?,?,?,?,?)", ((JID, str(i), SECOND, 0, "x") for i in range(1000)))
            cli.read_history(self.database, self.scope(limit=200))

    def test_json_escapes_terminal_control_characters(self):
        self.make_index()
        self.add_message(text="control \x1b[2J\n\u202e unicode")
        code, out, err = self.call(["history", "--db", str(self.database), "--chat", PHONE, "--since", START, "--until", END, "--execute"])
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertIn("\\u001b", out)


class ChildProcessTests(Fixtures):
    """Real subprocess plumbing, but only synthetic Python processes."""

    def run_python(self, source, timeout=3):
        return cli.run_bounded([sys.executable, "-I", "-c", source], timeout)

    def test_small_child_and_nonzero_exit(self):
        code, body = self.run_python("import sys; print('synthetic'); print('ignored-private-stderr', file=sys.stderr); sys.exit(7)")
        self.assertEqual(code, 7)
        self.assertEqual(body, b"synthetic\n")

    def test_environment_and_launch_options(self):
        with mock.patch.dict(os.environ, {"WACLI_STORE_DIR": SECRET, "WACLI_READONLY": "1", "HTTPS_PROXY": SECRET, "DYLD_INSERT_LIBRARIES": SECRET}):
            # Injecting DYLD_* into the parent can affect subprocess launch on
            # some platforms; the child receives only the explicit environment.
            code, raw = self.run_python("import os,json; print(json.dumps(dict(os.environ)))")
        self.assertEqual(code, 0)
        environment = json.loads(raw)
        self.assertNotIn(SECRET, raw.decode())
        self.assertNotIn("WACLI_READONLY", environment)
        self.assertEqual(environment["PATH"], "/usr/bin:/bin")

    def test_child_creates_private_files(self):
        path = self.root / "child-created"
        source = "import os; p=" + repr(str(path)) + "; open(p,'w').write('synthetic'); print(oct(os.stat(p).st_mode & 0o777))"
        code, raw = self.run_python(source)
        self.assertEqual(code, 0)
        self.assertEqual(raw.strip(), b"0o600")

    def test_timeout_and_output_limits(self):
        for source, timeout in (("import time; time.sleep(5)", 0.05),
                                ("print('x' * 70000)", 3),
                                ("import sys; print('x' * 70000, file=sys.stderr)", 3)):
            with self.subTest(source=source), self.assertRaises(cli.SafeError) as error:
                self.run_python(source, timeout)
            self.assertEqual(error.exception.code, "send_unknown")

    def test_spawn_failure_is_not_started(self):
        with self.assertRaises(cli.SafeError) as error:
            cli.run_bounded([str(self.root / "missing")], 1)
        self.assertEqual(error.exception.code, "send_not_started")

    def test_child_pipes_after_leader_exit_are_bounded(self):
        if not hasattr(os, "fork"):
            self.skipTest("POSIX fork unavailable")
        source = "import os,time; pid=os.fork(); time.sleep(5) if pid == 0 else None"
        with self.assertRaises(cli.SafeError) as error:
            self.run_python(source, 0.1)
        self.assertEqual(error.exception.code, "send_unknown")


if __name__ == "__main__":
    unittest.main()
