"""Real controlling-PTY regressions; synthetic QR content only, no account client."""
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import signal
import sys
import time
import unittest

import whatsapp_cli as cli


def terminal_probe(real_segno=False):
    read_fd, write_fd = os.pipe()
    pid, master = pty.fork()
    if pid == 0:
        os.close(read_fd)
        result = {}
        try:
            import termios
            attrs = termios.tcgetattr(0)
            attrs[3] |= termios.TOSTOP
            termios.tcsetattr(0, termios.TCSANOW, attrs)
            before = termios.tcgetattr(0)
            legacy_fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
            tty_fd = cli.open_pair_terminal()
            source = """import importlib.util,json,os,signal,sys,types
from pathlib import Path
request=json.loads(sys.stdin.buffer.read())
spec=importlib.util.spec_from_file_location('reviewed_worker',request['worker'])
worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
signal.signal(signal.SIGTTOU,signal.SIG_DFL)
signal.pthread_sigmask(signal.SIG_UNBLOCK,{signal.SIGTTOU})
result={'isatty':os.isatty(request['tty_fd'])}
try:
 os.write(request['legacy_fd'],b'BASELINE_WRITE');result['baseline_errno']=None
except OSError as error:
 result['baseline_errno']=error.errno
before=signal.pthread_sigmask(signal.SIG_BLOCK,set())
if not request['real_segno']:
 class FakeQR:
  def terminal(self,out,compact):
   result['blocked_during_write']=signal.SIGTTOU in signal.pthread_sigmask(signal.SIG_BLOCK,set())
   out.write('SYNTHETIC_QR_BLOCK \\u2588\\n');out.flush()
 sys.modules['segno']=types.SimpleNamespace(make_qr=lambda data:FakeQR())
try:
 worker.render_pair_qr(request,b'2@synthetic-token-for-regression-only-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx');result['rendered']=True
except Exception as error:
 result['rendered']=False;result['renderer_error']=type(error).__name__;result['renderer_errno']=getattr(error,'errno',None)
result['mask_restored']=signal.pthread_sigmask(signal.SIG_BLOCK,set())==before
result['signal_handler_preserved']=signal.getsignal(signal.SIGTTOU)==signal.SIG_DFL
print(json.dumps(result))
"""
            request = {"command": "pair", "tty_fd": tty_fd,
                       "worker": str(Path(__file__).with_name("whatsapp_backend.py")),
                       "real_segno": real_segno, "legacy_fd": legacy_fd}
            try:
                code, raw = cli.run_bounded([sys.executable, "-I", "-B", "-c", source], 5,
                                            json.dumps(request).encode(), pass_fds=(tty_fd, legacy_fd))
            finally:
                os.close(tty_fd)
                os.close(legacy_fd)
            result = json.loads(raw)
            result["code"] = code
            result["terminal_flags_preserved"] = termios.tcgetattr(0) == before
        except Exception as error:
            result = {"probe_error": type(error).__name__}
        os.write(write_fd, json.dumps(result).encode())
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    metadata, tty_bytes = bytearray(), 0
    deadline = time.monotonic() + 8
    completed = False
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([read_fd, master], [], [], 0.05)
            for descriptor in readable:
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError:
                    continue
                if descriptor == master:
                    tty_bytes += len(chunk)  # Discard synthetic QR bytes, never print them.
                else:
                    metadata.extend(chunk)
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                completed = True
                break
        if not completed:
            raise TimeoutError("Terminal regression did not complete")
        result = json.loads(metadata)
        result["terminal_bytes"] = tty_bytes
        return result
    finally:
        if not completed:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(read_fd)
        os.close(master)


class TerminalJobControlTests(unittest.TestCase):
    def verify_result(self, result):
        if sys.platform == "darwin":
            self.assertEqual(result["baseline_errno"], 5)  # Alias writes fail in a detached session.
        else:
            self.assertIn(result["baseline_errno"], (None, 5))
        self.assertTrue(result["isatty"])
        self.assertTrue(result["rendered"])
        self.assertTrue(result["mask_restored"])
        self.assertTrue(result["signal_handler_preserved"])
        self.assertTrue(result["terminal_flags_preserved"])
        self.assertEqual(result["code"], 0)
        self.assertGreater(result["terminal_bytes"], 0)

    def test_concrete_terminal_works_where_session_alias_fails(self):
        result = terminal_probe()
        self.verify_result(result)
        self.assertFalse(result["blocked_during_write"])

    @unittest.skipUnless(importlib.util.find_spec("segno"), "Installed-wheel renderer check")
    def test_real_qr_renderer_on_controlling_terminal_with_tostop(self):
        self.verify_result(terminal_probe(real_segno=True))


if __name__ == "__main__":
    unittest.main()
