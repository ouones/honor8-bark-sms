"""Run on Linux/WSL: python3 -m unittest discover -s tests -v.

Requires bash, curl, jq and openssl on the test host.
Only external commands and the HTTP service are replaced; the daemon is real.
"""

import contextlib
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import tempfile
import threading
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BODY = '普通合成短信：测试 "引号"、反斜杠\\与表情🙂'
KEY = "synthetic-device-key"


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("daemon did not reach the expected observable state")


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bark-t01-")
        self.addCleanup(self.temp.cleanup)
        self.module = Path(self.temp.name)
        shutil.copytree(ROOT / "core", self.module / "core")
        (self.module / "bin").mkdir()
        for name in ("curl", "jq"):
            executable = shutil.which(name)
            self.assertIsNotNone(executable, f"install {name} on the test host")
            (self.module / "bin" / name).symlink_to(executable)
        shutil.copyfile(ROOT / "bin/cacert.pem", self.module / "bin/cacert.pem")
        self.tmp = self.module / "runtime"
        self.tmp.mkdir()
        self.commands = self.module / "commands"
        self.commands.mkdir()
        self.requests = []
        self.reply = (200, b'{"code":200,"message":"success"}')
        self.delay = 0
        owner = self

        class Receiver(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                owner.requests.append((self.path, self.headers, payload))
                time.sleep(owner.delay)
                status, body = owner.reply
                try:
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        self.server.daemon_threads = True
        self.addCleanup(self.server.server_close)
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.start()
        self.addCleanup(self.server_thread.join)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/custom/push"
        self.write_command("content", '''#!/bin/sh
case "$*" in
  *"--projection date "*) printf 'Row: 0 date=1\\n' ;;
  *) cat "$FIXTURE" ;;
esac
''')
        self.write_command("sleep", '''#!/bin/sh
printf 'poll\\n' >> "$POLLS"
exec /bin/sleep "$@"
''')
        self.fixture = self.module / "synthetic-provider.txt"
        self.fixture.write_text("Row: 0 address=synthetic-sender, body=history, date=1, sub_id=1\n", encoding="utf-8")
        self.polls = self.module / "polls"

    def write_command(self, name, source):
        path = self.commands / name
        path.write_text(source, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def cursor(self):
        return (self.tmp / "last_sms_date.txt").read_text(encoding="utf-8").strip()

    @contextlib.contextmanager
    def daemon(self):
        self.fixture.write_text("Row: 0 address=synthetic-sender, body=history, date=1, sub_id=1\n", encoding="utf-8")
        config = self.module / "core/config.conf"
        with config.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f'''\nPUSH_CHANNEL="Bark"
BARK_API_URL="{self.url}"
BARK_DEVICE_KEY="{KEY}"
TMP_DIR="{self.tmp}"
ENABLE_SMS="true"
ENABLE_MISSED_CALL="false"
ENABLE_INCOMING_CALL="false"
ENABLE_OUTGOING_CALL="false"
OS_TYPE="EMUI"
INTERVAL=1
''')
        env = dict(os.environ, PATH=f"{self.commands}:{os.environ['PATH']}",
                   TMP_DIR=str(self.tmp), FIXTURE=str(self.fixture), POLLS=str(self.polls),
                   CURL_HOME=str(self.module))
        process = subprocess.Popen(["bash", str(self.module / "core/sms_daemon.sh")],
                                   env=env, start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(self.polls.exists)
            self.assertEqual(self.cursor(), "1")
            self.assertEqual(self.requests, [])
            self.fixture.write_text(
                f"Row: 0 address=synthetic-sender, body={BODY}, date=2, sub_id=1\n",
                encoding="utf-8")
            yield
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)

    def wait_for_attempt(self):
        wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= 2)

    def assert_no_payload_files_or_sensitive_logs(self):
        log = (self.tmp / "sms_forward_run.log").read_text(encoding="utf-8")
        for secret in (KEY, BODY, "synthetic-sender"):
            self.assertNotIn(secret, log)
        self.assertEqual({path.name for path in self.tmp.iterdir()},
                         {"sms_forward_run.log", "last_sms_date.txt"})

    def test_plain_sms_is_one_utf8_json_post(self):
        with self.daemon():
            self.wait_for_attempt()
            self.assertEqual(self.cursor(), "2")
            time.sleep(1.2)
            self.assertEqual(len(self.requests), 1)
            path, headers, payload = self.requests[0]
            self.assertEqual(path, "/custom/push")
            self.assertIn("application/json", headers["Content-Type"])
            data = json.loads(payload.decode("utf-8"))
            self.assertEqual(data["device_key"], KEY)
            self.assertEqual(data["title"], "【短信】")
            self.assertIn(BODY, data["body"])
            self.assert_no_payload_files_or_sensitive_logs()

    def test_failure_response_does_not_advance_caller(self):
        cases = [
            (503, b'{"code":200}'),
            (200, b'{"code":400,"message":"200"}'),
            (200, b''),
            (200, b'<html>200</html>'),
            (200, b'{"nested":{"code":200}}'),
            (200, b'{"code":"200"}'),
            (200, b'{"code":200} trailing'),
            (200, b'{"code":200}\n{"code":500}'),
            (200, b'[ {"code":200} ]'),
        ]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                self.reply = (status, body)
                # Each process gets a fresh initial boundary.
                self.polls.unlink(missing_ok=True)
                (self.tmp / "last_sms_date.txt").unlink(missing_ok=True)
                self.requests.clear()
                with self.daemon():
                    self.wait_for_attempt()
                    self.assertEqual(self.cursor(), "1")
                    self.assertEqual(len(self.requests), 1)
                    self.assert_no_payload_files_or_sensitive_logs()

    def test_client_exit_error_rejects_even_success_looking_output(self):
        client = self.module / "bin/curl"
        client.unlink()
        client.write_text('#!/bin/sh\nprintf \'{"code":200}\\n200\'\nexit 7\n', encoding="utf-8")
        client.chmod(0o755)
        with self.daemon():
            self.wait_for_attempt()
            self.assertEqual(self.cursor(), "1")
            self.assert_no_payload_files_or_sensitive_logs()

    def test_request_timeout_is_bounded_without_internal_retry(self):
        self.delay = 7
        with self.daemon():
            started = time.monotonic()
            self.wait_for_attempt()
            self.assertGreater(time.monotonic() - started, 4.5)
            self.assertLess(time.monotonic() - started, 6.5)
            self.assertEqual(self.cursor(), "1")
            self.assertEqual(len(self.requests), 1)

    def test_untrusted_certificate_is_rejected(self):
        (self.module / ".curlrc").write_text("insecure\nretry = 3\n", encoding="utf-8")
        cert, key = self.module / "cert.pem", self.module / "key.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "1",
                        "-subj", "/CN=localhost"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.url = f"https://localhost:{self.server.server_port}/push"
        with self.daemon():
            self.wait_for_attempt()
            self.assertEqual(self.cursor(), "1")
            self.assertEqual(self.requests, [])

    def test_remote_http_and_loopback_userinfo_are_rejected(self):
        for url in ("http://example.com/push", "http://localhost:80@example.com/push"):
            with self.subTest(url=url):
                self.url = url
                self.polls.unlink(missing_ok=True)
                (self.tmp / "last_sms_date.txt").unlink(missing_ok=True)
                with self.daemon():
                    self.wait_for_attempt()
                    self.assertEqual(self.cursor(), "1")
                    self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
