"""Security/lifecycle tests with synthetic sessions; never connect to WhatsApp."""
from contextlib import closing
import io
import json
import os
import stat
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

import whatsapp_cli as cli
import whatsapp_backend as backend

JID = "12025550123@s.whatsapp.net"
IDENTITY = "12025550124:7@s.whatsapp.net"
SECRET = "synthetic-private-value"


class DirectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.root.chmod(0o700)
        self.store = self.root / "store"
        cli.initialize_store(str(self.store))

    def linked(self, identities=(IDENTITY,)):
        with closing(sqlite3.connect(self.store / "session.db")) as db, db:
            db.execute("CREATE TABLE whatsmeow_device(jid TEXT)")
            db.executemany("INSERT INTO whatsmeow_device VALUES (?)", [(x,) for x in identities])

    def test_unsupported_python_stops_before_state_access(self):
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.sys, "version_info", (3, 10)), mock.patch.object(cli, "checked_path", side_effect=AssertionError):
            self.assertEqual(cli.main(["status", "--store", str(self.store), "--execute"], stdout=output, stderr=error), 1)
        self.assertEqual(json.loads(error.getvalue())["error"], "unsupported_python")

    def test_private_init_no_overwrite(self):
        self.assertEqual(cli.verify_store(self.store), self.store)
        self.assertIsNone(cli.session_identity(self.store))
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o700)
        for path in self.store.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(cli.SafeError):
            cli.initialize_store(str(self.store))

    def test_local_status_never_launches_or_shows_identity(self):
        self.linked()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "run_bounded", side_effect=AssertionError):
            self.assertEqual(cli.main(["status", "--store", str(self.store), "--execute"], stdout=out, stderr=err), 0)
        self.assertEqual(json.loads(out.getvalue()), {"status": "local", "linked": True, "online_checked": False})
        self.assertNotIn(IDENTITY, out.getvalue() + err.getvalue())

    def test_missing_session_never_pairs_implicitly(self):
        with mock.patch.object(cli, "run_bounded") as run:
            with self.assertRaises(cli.SafeError) as error:
                cli.send_text(self.store, JID, SECRET)
            self.assertEqual(error.exception.code, "not_linked")
            run.assert_not_called()

    def test_ambiguous_or_invalid_accounts_rejected(self):
        for identities in ((IDENTITY, IDENTITY), (SECRET,)):
            with self.subTest(identities=identities):
                self.linked(identities)
                with self.assertRaises(cli.SafeError):
                    cli.session_identity(self.store)
                with closing(sqlite3.connect(self.store / "session.db")) as db, db:
                    db.execute("DROP TABLE whatsmeow_device")

    def test_generated_identity_column_and_views_rejected(self):
        for definition in ("CREATE VIEW whatsmeow_device AS SELECT '" + IDENTITY + "' AS jid",
                           "CREATE TABLE whatsmeow_device(x TEXT,jid TEXT GENERATED ALWAYS AS (x) VIRTUAL)"):
            with closing(sqlite3.connect(self.store / "session.db")) as db, db:
                db.execute(definition)
            with self.assertRaises(cli.SafeError):
                cli.session_identity(self.store)
            with closing(sqlite3.connect(self.store / "session.db")) as db, db:
                db.execute("DROP VIEW whatsmeow_device" if "VIEW" in definition else "DROP TABLE whatsmeow_device")

    def test_unsafe_store_and_sidecars_rejected_without_chmod(self):
        self.store.chmod(0o755)
        with self.assertRaises(cli.SafeError):
            cli.verify_store(self.store)
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o755)
        self.store.chmod(0o700)
        (self.store / "session.db-wal").symlink_to(self.store / "account.json")
        with self.assertRaises(cli.SafeError):
            cli.verify_store(self.store)

    def test_foreign_store_never_migrated(self):
        (self.store / "account.json").write_text('{}')
        with self.assertRaises(cli.SafeError):
            cli.verify_store(self.store)

    def test_account_uri_query_or_fragment_rejected(self):
        for name in ("state?mode=ro", "state#fragment"):
            with self.assertRaises(cli.SafeError):
                cli.initialize_store(str(self.root / name))

    def test_self_send_requires_explicit_opt_in(self):
        self.linked()
        self_jid = IDENTITY.split(":")[0] + "@s.whatsapp.net"
        with mock.patch.object(cli, "run_bounded") as run:
            with self.assertRaises(cli.SafeError) as error:
                cli.send_text(self.store, self_jid, SECRET)
            self.assertEqual(error.exception.code, "self_send_blocked")
            run.assert_not_called()

    def test_message_only_in_stdin_and_strict_acceptance(self):
        self.linked()
        accepted = {"status": "accepted", "message_id": "ABC123", "delivery_confirmed": False}
        with mock.patch.object(cli, "run_bounded", return_value=(0, json.dumps(accepted).encode())) as run:
            self.assertEqual(cli.send_text(self.store, JID, SECRET)["message_id"], "ABC123")
        argv, timeout, request = run.call_args.args
        self.assertEqual(argv[0], sys.executable)
        self.assertEqual(argv[1:3], ["-I", "-B"])
        self.assertNotIn(SECRET, str(argv))
        self.assertNotIn(JID, str(argv))
        self.assertEqual(json.loads(request)["text"], SECRET)
        self.assertEqual(json.loads(request)["jid"], JID)
        self.assertEqual(timeout, cli.SEND_TIMEOUT)

    def test_bad_acceptance_unknown_no_retry(self):
        self.linked()
        for data in (b'raw ' + SECRET.encode(), b'{}', b'{"status":"accepted","message_id":"ABC","delivery_confirmed":true}'):
            with mock.patch.object(cli, "run_bounded", return_value=(0, data)) as run:
                with self.assertRaises(cli.SafeError) as error:
                    cli.send_text(self.store, JID, SECRET)
                self.assertEqual(error.exception.code, "send_unknown")
                self.assertNotIn(SECRET, str(error.exception))
                run.assert_called_once()

    def test_pair_requires_tty_and_never_adds_account(self):
        with mock.patch.object(cli.os, "open", side_effect=OSError), mock.patch.object(cli, "run_bounded") as run:
            with self.assertRaises(cli.SafeError) as error:
                cli.backend_operation("pair", self.store)
            self.assertEqual(error.exception.code, "terminal_required")
            run.assert_not_called()
        self.linked()
        with self.assertRaises(cli.SafeError) as error:
            cli.backend_operation("pair", self.store)
        self.assertEqual(error.exception.code, "already_linked")

    def test_pair_forwards_only_verified_tty_and_closes_descriptor(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        with mock.patch.object(cli.os, "open", return_value=write_fd), mock.patch.object(cli.os, "isatty", return_value=True), mock.patch.object(cli.os, "fstat", return_value=types.SimpleNamespace(st_mode=stat.S_IFCHR)), mock.patch.object(cli, "run_bounded", return_value=(0, b'{"status":"linked"}')) as run:
            self.assertEqual(cli.backend_operation("pair", self.store), {"status": "linked"})
        self.assertEqual(run.call_args.kwargs["pass_fds"], (write_fd,))
        self.assertEqual(json.loads(run.call_args.args[2])["tty_fd"], write_fd)
        with self.assertRaises(OSError):
            os.fstat(write_fd)

    def test_pair_terminal_closed_on_launch_failure(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        with mock.patch.object(cli.os, "open", return_value=write_fd), mock.patch.object(cli.os, "isatty", return_value=True), mock.patch.object(cli.os, "fstat", return_value=types.SimpleNamespace(st_mode=stat.S_IFCHR)), mock.patch.object(cli, "run_bounded", side_effect=cli.SafeError("send_not_started", "No worker started")):
            with self.assertRaises(cli.SafeError):
                cli.backend_operation("pair", self.store)
        with self.assertRaises(OSError):
            os.fstat(write_fd)

    def test_worker_rejects_stdio_pipe_or_send_as_pairing_terminal(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, read_fd)
        self.addCleanup(os.close, write_fd)
        for request in ({"command": "pair", "tty_fd": 1}, {"command": "pair", "tty_fd": True}, {"command": "pair", "tty_fd": write_fd}, {"command": "send", "tty_fd": write_fd}):
            with self.assertRaises(ValueError):
                backend.verified_pair_terminal(request)

    def test_detached_worker_can_render_only_to_inherited_terminal(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        worker_path = str(Path(backend.__file__).absolute())
        source = """import importlib.util,json,os,sys,types
request=json.loads(sys.stdin.buffer.read())
spec=importlib.util.spec_from_file_location('synthetic_worker',request['worker'])
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
# Only synthetic text enters this PTY; no protocol library or account is loaded.
class FakeQR:
 def terminal(self,out,compact):
  out.write('SYNTHETIC_QR_MARKER');out.flush()
sys.modules['segno']=types.SimpleNamespace(make_qr=lambda data: FakeQR())
worker.render_pair_qr(request,b'synthetic-not-a-credential')
try:
 fd=os.open('/dev/tty',os.O_WRONLY);os.close(fd);detached=False
except OSError:
 detached=True
print(json.dumps({'tty_valid':os.isatty(request['tty_fd']),'detached':detached}))
"""
        request = {"command": "pair", "tty_fd": slave, "worker": worker_path}
        code, raw = cli.run_bounded([sys.executable, "-I", "-B", "-c", source], 5, json.dumps(request).encode(), pass_fds=(slave,))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(raw), {"tty_valid": True, "detached": True})
        self.assertNotIn(b"SYNTHETIC_QR_MARKER", raw)
        os.set_blocking(master, False)
        self.assertEqual(os.read(master, 4096), b"SYNTHETIC_QR_MARKER")

    def test_all_dry_runs_do_not_inspect_or_launch(self):
        with mock.patch.object(cli, "checked_path", side_effect=AssertionError), mock.patch.object(cli, "run_bounded", side_effect=AssertionError):
            for command in ("init", "status", "pair", "sync"):
                args = [command, "--store", "/fictional/store"]
                if command == "sync":
                    args += ["--chat", JID]
                self.assertEqual(cli.main(args, stdout=io.StringIO(), stderr=io.StringIO()), 0)

    def test_sync_scope_limits(self):
        for more in (["--seconds", "0"], ["--seconds", "121"], ["--limit", "201"]):
            with mock.patch.object(cli, "run_bounded") as run:
                self.assertEqual(cli.main(["sync", "--store", str(self.store), "--chat", JID] + more, stdout=io.StringIO(), stderr=io.StringIO()), 1)
                run.assert_not_called()

    def test_scoped_index_bounded_dedup_revocation(self):
        index = backend.TextIndex(self.store, JID, 2)
        now = int(time.time())
        index.record("A", now, False, "one")
        index.record("A", now, False, "changed")
        index.record("B", now, True, "two")
        index.record("C", now, False, "over limit")
        index.record("OLD", now - 32 * 86400, False, "expired")
        self.assertEqual(index.stored, 2)
        index.revoke("A")
        index.revoke("UNSEEN")
        later = backend.TextIndex(self.store, JID, 10)
        later.record("UNSEEN", now, False, "must not resurrect")
        with closing(sqlite3.connect(index.path)) as db:
            rows = db.execute("SELECT msg_id,text,revoked FROM messages ORDER BY msg_id").fetchall()
        self.assertEqual(rows, [("A", None, 1), ("B", "two", 0), ("UNSEEN", None, 1)])

    def test_native_missing_or_changed_fails_before_import(self):
        library = self.root / "neonize-darwin-arm64.dylib"
        library.write_bytes(b"wrong library")
        distribution = mock.Mock()
        distribution.locate_file.return_value = self.root
        with mock.patch.object(backend.platform, "system", return_value="Darwin"), mock.patch.object(backend.platform, "machine", return_value="arm64"), mock.patch.object(backend.importlib.metadata, "version", side_effect=lambda name: backend.PINNED_PACKAGES[name]), mock.patch.object(backend.importlib.metadata, "distribution", return_value=distribution):
            with self.assertRaises(RuntimeError):
                backend.load_protocol(cli)
        self.assertNotIn("neonize", sys.modules)

    def test_dependency_drift_fails_closed(self):
        with mock.patch.object(backend.importlib.metadata, "version", return_value="unexpected"):
            with self.assertRaises(RuntimeError):
                backend.load_protocol(cli)

    def test_runtime_download_always_disabled(self):
        with self.assertRaises(RuntimeError):
            backend.disabled(SECRET)

    def test_worker_redacts_invalid_request_and_native_errors(self):
        code, raw = cli.run_bounded([sys.executable, "-I", "-B", str(Path(backend.__file__).absolute())], 5, json.dumps({"command": SECRET}).encode())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(raw), {"error": "backend_unavailable"})
        self.assertNotIn(SECRET.encode(), raw)

    def test_worker_rejects_concurrent_store_before_protocol(self):
        import fcntl
        lock_path = self.store / ".cli.lock"
        with open(lock_path, "w") as lock:
            lock_path.chmod(0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            request = {"command": "sync", "store": str(self.store), "identity": None, "seconds": 30, "limit": 200}
            code, raw = cli.run_bounded([sys.executable, "-I", "-B", str(Path(backend.__file__).absolute())], 5, json.dumps(request).encode())
        self.assertEqual(json.loads(raw), {"error": "backend_busy"})

    def test_stdin_large_payload_does_not_deadlock_or_enter_argv(self):
        request = ("👋" * 10000).encode()
        code, raw = cli.run_bounded([sys.executable, "-I", "-c", "import sys; print(len(sys.stdin.buffer.read()))"], 5, request)
        self.assertEqual(code, 0)
        self.assertEqual(int(raw), len(request))


class FakeMessage:
    def __init__(self, text=None, **fields):
        if text is not None:
            fields["conversation"] = text
        self.fields = fields
        self.__dict__.update(fields)

    def ListFields(self):
        return [(types.SimpleNamespace(name=name), value) for name, value in self.fields.items()]

    def HasField(self, name):
        return name in self.fields


class PayloadTests(unittest.TestCase):
    def test_plain_literal_only(self):
        self.assertEqual(backend.plain_text(FakeMessage("literal @12025550123 https://example.com")), "literal @12025550123 https://example.com")
        for field in ("ephemeralMessage", "viewOnceMessage", "imageMessage", "protocolMessage"):
            self.assertIsNone(backend.plain_text(FakeMessage(**{field: object()})))
        self.assertIsNone(backend.plain_text(FakeMessage("body", messageContextInfo=object())))

    def test_expiring_extended_text_not_persisted(self):
        context = types.SimpleNamespace(expiration=60, ephemeralSettingTimestamp=0)
        message = FakeMessage(extendedTextMessage=types.SimpleNamespace(text="private", contextInfo=context))
        self.assertIsNone(backend.plain_text(message))

class FakeEvents:
    def __init__(self):
        self.handlers = {}
        self.qr_handler = None

    def __call__(self, name):
        return lambda handler: self.handlers.__setitem__(name, handler)

    def qr(self, handler):
        self.qr_handler = handler


class LifecycleTests(unittest.TestCase):
    setUp = DirectTests.setUp
    linked = DirectTests.linked
    def protocol(self, *, qr=False, wrong_identity=False, send_error=False):
        client = types.SimpleNamespace(event=FakeEvents())
        stop = threading.Event()
        sent = []
        client.stop = stop.set
        def connect():
            if qr:
                client.event.qr_handler(client, b"synthetic-only-not-a-credential")
            client.event.handlers["connected"](client, None)
            stop.wait(3)
        client.connect = connect
        client.get_me = lambda: types.SimpleNamespace(JID=types.SimpleNamespace(User="12025550125" if wrong_identity else "12025550124", Device=7, Server="s.whatsapp.net"))
        def send(to, message, **kwargs):
            sent.append((to, message, kwargs))
            if send_error:
                raise RuntimeError(SECRET)
            return types.SimpleNamespace(ID="ABC123")
        client.send_message = send
        protocol = types.SimpleNamespace(NewClient=mock.Mock(return_value=client), JID=lambda **kw: types.SimpleNamespace(**kw), Message=lambda **kw: FakeMessage(**kw), ConnectedEv="connected", LoggedOutEv="logout", HistorySyncEv="history", MessageEv="message")
        return protocol, sent

    def request(self, **changes):
        values = {"command": "send", "store": str(self.store), "identity": IDENTITY, "jid": JID, "text": "literal @12025550123 https://example.com", "seconds": 1, "limit": 200, "allow_self": False}
        values.update(changes)
        return values

    def test_selected_session_and_literal_protobuf_send(self):
        self.linked()
        protocol, sent = self.protocol()
        response = backend.operate(self.request(), cli, protocol)
        self.assertEqual(response["status"], "accepted")
        self.assertFalse(response["delivery_confirmed"])
        self.assertEqual(len(sent), 1)
        to, message, flags = sent[0]
        self.assertEqual(to.User + "@" + to.Server, JID)
        self.assertEqual(message.conversation, self.request()["text"])
        self.assertEqual(flags, {"link_preview": False})
        self.assertEqual(protocol.NewClient.call_args.kwargs["jid"].Device, 7)

    def test_account_change_blocks_send(self):
        self.linked()
        protocol, sent = self.protocol(wrong_identity=True)
        self.assertEqual(backend.operate(self.request(), cli, protocol), {"error": "pair_required"})
        self.assertEqual(sent, [])

    def test_unexpected_qr_blocks_send_without_exposing_code(self):
        self.linked()
        protocol, sent = self.protocol(qr=True)
        self.assertEqual(backend.operate(self.request(), cli, protocol), {"error": "pair_required"})
        self.assertEqual(sent, [])

    def test_native_send_error_never_retries(self):
        self.linked()
        protocol, sent = self.protocol(send_error=True)
        with self.assertRaises(RuntimeError):
            backend.operate(self.request(), cli, protocol)
        self.assertEqual(len(sent), 1)

    def test_native_self_guard_cannot_be_bypassed(self):
        self.linked()
        protocol, sent = self.protocol()
        own = IDENTITY.split(":")[0] + "@s.whatsapp.net"
        self.assertEqual(backend.operate(self.request(jid=own), cli, protocol), {"error": "pair_required"})
        self.assertEqual(sent, [])

    def test_identity_changed_before_connect_does_not_open_client(self):
        self.linked()
        protocol, sent = self.protocol()
        self.assertEqual(backend.operate(self.request(identity="12025550125:7@s.whatsapp.net"), cli, protocol), {"error": "pair_required"})
        protocol.NewClient.assert_not_called()



if __name__ == "__main__":
    unittest.main()
