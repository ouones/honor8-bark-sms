"""Run on Linux/WSL: python3 -m unittest discover -s tests -v.

Requires bash, curl, jq and openssl on the test host.
Only external commands and the HTTP service are replaced; the daemon is real.
"""

import contextlib
from datetime import datetime
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
from zoneinfo import ZoneInfo


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
        self.request_times = []
        self.observed_cursors = []
        self.reply = (200, b'{"code":200,"message":"success"}')
        owner = self

        class Receiver(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                payload = self.rfile.read(int(self.headers["Content-Length"]))
                owner.observed_cursors.append(owner.cursor() if owner.cursor_file.is_file() else None)
                owner.requests.append((self.path, self.headers, payload))
                owner.request_times.append(time.monotonic())
                status, body = owner.reply(len(owner.requests)) if callable(owner.reply) else owner.reply
                try:
                    if status is None:
                        self.close_connection = True
                        return
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
        self.fixture = self.module / "synthetic-provider.txt"
        self.siminfo_fixture = self.module / "synthetic-siminfo.txt"
        self.content_calls = self.module / "content-calls.txt"
        self.siminfo_fixture.write_text("Row: 0 _id=1, sim_id=0\n", encoding="utf-8")
        self.content_calls.write_text("", encoding="utf-8")
        self.write_command("content", '''#!/usr/bin/python3
import os
from pathlib import Path
import json
import sqlite3
import sys

args = sys.argv[1:]
with open(os.environ["CONTENT_CALLS"], "a", encoding="utf-8") as stream:
    stream.write(" ".join(args) + "\\n")
if args[args.index("--uri") + 1] == "content://telephony/siminfo":
    print(Path(os.environ["SIMINFO_FIXTURE"]).read_text(encoding="utf-8"), end="")
    sys.exit(0)
database = sqlite3.connect(":memory:")
database.execute("CREATE TABLE sms (_id INTEGER, date INTEGER, sub_id INTEGER, address TEXT, body TEXT, type INTEGER)")
database.executemany("INSERT INTO sms VALUES (?, ?, ?, ?, ?, 1)",
                     json.loads(Path(os.environ["FIXTURE"]).read_text(encoding="utf-8")))
projection = args[args.index("--projection") + 1].replace(":", ",")
where = args[args.index("--where") + 1] if "--where" in args else "1"
sort = args[args.index("--sort") + 1] if "--sort" in args else "date DESC"
rows = database.execute(f"SELECT {projection} FROM sms WHERE {where} ORDER BY {sort}")
names = [column[0] for column in rows.description]
found = False
for index, row in enumerate(rows):
    found = True
    print(f"Row: {index} " + ", ".join(f"{name}={value}" for name, value in zip(names, row)))
if not found:
    print("No result found.")
''')
        self.write_command("sleep", '''#!/bin/sh
printf 'poll\\n' >> "$POLLS"
exec /bin/sleep "$@"
''')
        self.write_sms_records([(1, 1, 1, "synthetic-sender", "history")])
        self.polls = self.module / "polls"

    def write_sms_records(self, records):
        pending = self.fixture.with_suffix(".new")
        pending.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        pending.replace(self.fixture)

    def write_command(self, name, source):
        path = self.commands / name
        path.write_text(source, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def cursor(self):
        return self.cursor_file.read_text(encoding="utf-8").strip()

    @property
    def cursor_file(self):
        return self.module / "state/sms.cursor"

    def startup_config(self, extra=""):
        with (self.module / "core/config.conf").open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f'\nBARK_API_URL="{self.url}"\nBARK_DEVICE_KEY="{KEY}"\n'
                         f'TMP_DIR="{self.tmp}"\nINTERVAL=1\n{extra}\n')

    def startup_env(self):
        return dict(os.environ, PATH=f"{self.commands}:{os.environ['PATH']}",
                    FIXTURE=str(self.fixture),
                    SIMINFO_FIXTURE=str(self.siminfo_fixture), CONTENT_CALLS=str(self.content_calls),
                    POLLS=str(self.polls), TZ="Asia/Shanghai")

    def test_invalid_startup_never_initializes_progress(self):
        original = (self.module / "core/config.conf").read_text(encoding="utf-8")
        for extra in ('BARK_DEVICE_KEY=""', 'INTERVAL=0', 'INTERVAL=-1',
                      'INTERVAL=abc', 'INTERVAL=1.5', 'MAX_LOG_SIZE_KB=bad',
                      'ENABLE_SMS=maybe', 'PUSH_CHANNEL=unknown', 'OS_TYPE=""',
                      'BARK_API_URL=""', 'BARK_API_URL="https://bad host/push"',
                      'BARK_API_URL="https://:443/push"', 'BARK_API_URL="https:////push"'):
            with self.subTest(extra=extra):
                (self.module / "core/config.conf").write_text(original, encoding="utf-8")
                self.startup_config(extra)
                process = subprocess.Popen(["busybox", "ash", str(self.module / "core/sms_daemon.sh")],
                                           env=self.startup_env(), start_new_session=True,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                try:
                    output, error = process.communicate(timeout=2)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn("错误", (output + error).decode("utf-8"))
                    self.assertFalse(self.cursor_file.exists())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.communicate()

    def test_missing_bark_dependencies_fail_before_progress(self):
        self.startup_config()
        for name in ("curl", "jq", "cacert.pem"):
            with self.subTest(name=name):
                path = self.module / "bin" / name
                hidden = path.with_suffix(".hidden")
                path.rename(hidden)
                try:
                    result = subprocess.run(["busybox", "ash", str(self.module / "core/sms_daemon.sh")],
                                            env=self.startup_env(), capture_output=True, timeout=2)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("错误", (result.stdout + result.stderr).decode("utf-8"))
                    self.assertFalse(self.cursor_file.exists())
                finally:
                    hidden.rename(path)

    def test_configured_log_rotation_and_single_instance(self):
        self.startup_config('MAX_LOG_SIZE_KB=1')
        log = self.tmp / "sms_forward_run.log"
        log.write_text("x" * 1100, encoding="utf-8")
        command = ["busybox", "ash", str(self.module / "core/sms_daemon.sh")]
        first = subprocess.Popen(command, env=self.startup_env(), start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(self.cursor_file.exists)
            self.assertEqual(self.cursor(), "1 1")
            self.assertTrue(log.with_suffix(".log.bak").exists())
            second = subprocess.run(command, env=self.startup_env(), capture_output=True, timeout=2)
            self.assertNotEqual(second.returncode, 0)
            self.assertIn("运行", second.stderr.decode("utf-8"))
            self.assertIsNone(first.poll())
        finally:
            os.killpg(first.pid, signal.SIGTERM)
            first.wait(timeout=3)
        # Kernel lock is released after termination; no stale PID cleanup required.
        restarted = subprocess.Popen(command, env=self.startup_env(), start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1.2)
            self.assertIsNone(restarted.poll())
        finally:
            os.killpg(restarted.pid, signal.SIGTERM)
            restarted.wait(timeout=3)

    def test_bark_install_preserves_honor8_settings_without_keys(self):
        self.startup_config('BARK_API_URL="https://example.invalid/push"')
        # Map only the absolute external BusyBox executable on the host.
        (self.module / "customize.sh").write_text(
            (ROOT / "customize.sh").read_text(encoding="utf-8").replace(
                "/data/adb/magisk/busybox", shutil.which("busybox")), encoding="utf-8")
        self.write_command("getevent", '#!/bin/sh\necho unexpected-key-read >&2\nexit 1\n')
        harness = '''ui_print() { printf '%s\\n' "$*"; }
abort() { ui_print "$*"; exit 1; }
set_perm_recursive() { :; }
set_perm() { :; }
. "$MODPATH/customize.sh"
. "$MODPATH/core/config.conf"
printf 'settings:%s:%s:%s:%s:%s\\n' "$OS_TYPE" "$ENABLE_SMS" "$ENABLE_MISSED_CALL" "$ENABLE_INCOMING_CALL" "$ENABLE_OUTGOING_CALL"
'''
        result = subprocess.run(["busybox", "ash", "-c", harness],
                                env=dict(self.startup_env(), MODPATH=str(self.module)),
                                capture_output=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b"settings:EMUI:true:false:false:false", result.stdout)
        self.assertNotIn(b"unexpected-key-read", result.stderr)
        valid = (self.module / "core/config.conf").read_text(encoding="utf-8")
        for extra in ('BARK_DEVICE_KEY=""', 'BARK_API_URL=""', 'INTERVAL=0',
                      'BARK_API_URL="https://bad host/push"',
                      'BARK_API_URL="https://:443/push"', 'BARK_API_URL="https:////push"'):
            with self.subTest(extra=extra):
                (self.module / "core/config.conf").write_text(valid + "\n" + extra + "\n", encoding="utf-8")
                result = subprocess.run(["busybox", "ash", "-c", harness],
                                        env=dict(self.startup_env(), MODPATH=str(self.module)),
                                        capture_output=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("错误", result.stdout.decode("utf-8"))
                self.assertFalse(self.cursor_file.exists())
        (self.module / "core/config.conf").write_text(valid, encoding="utf-8")
        (self.module / "bin/curl").unlink()
        result = subprocess.run(["busybox", "ash", "-c", harness],
                                env=dict(self.startup_env(), MODPATH=str(self.module)),
                                capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("依赖错误", result.stdout.decode("utf-8"))

    def test_missing_provider_tool_fails_before_progress(self):
        self.startup_config()
        (self.commands / "content").unlink()
        result = subprocess.run(["busybox", "ash", str(self.module / "core/sms_daemon.sh")],
                                env=self.startup_env(), capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"content", result.stderr)
        self.assertFalse(self.cursor_file.exists())

    def test_service_requires_busybox(self):
        script = self.module / "service.sh"
        script.write_text((ROOT / "service.sh").read_text(encoding="utf-8").replace(
            "/data/adb/magisk/busybox", str(self.module / "missing-busybox")), encoding="utf-8")
        result = subprocess.run(["bash", str(script)], capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"BusyBox", result.stderr)
        self.assertFalse(self.cursor_file.exists())

    def test_service_boots_single_daemon_with_configured_log(self):
        self.startup_config('MAX_LOG_SIZE_KB=1')
        # Ubuntu's trimmed BusyBox omits nohup; map that external applet only.
        self.write_command("magisk-busybox", '''#!/bin/sh
if [ "$1" = nohup ]; then shift; exec /usr/bin/nohup "$@"; fi
exec /usr/bin/busybox "$@"
''')
        script = self.module / "service.sh"
        script.write_text((ROOT / "service.sh").read_text(encoding="utf-8").replace(
            "/data/adb/magisk/busybox", str(self.commands / "magisk-busybox")), encoding="utf-8")
        self.write_command("getprop", '#!/bin/sh\nprintf "1\\n"\n')
        self.write_command("sleep", '#!/bin/sh\nexit 0\n')
        (self.tmp / "sms_forward_run.log").write_text("x" * 1100, encoding="utf-8")
        groups = []
        try:
            for _ in range(2):
                process = subprocess.Popen(["bash", str(script)], env=self.startup_env(),
                                           start_new_session=True, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL)
                groups.append(process.pid)
                self.assertEqual(process.wait(timeout=2), 0)
                wait_for(self.cursor_file.exists)
            self.assertEqual(self.cursor(), "1 1")
            self.assertTrue((self.tmp / "sms_forward_run.log.bak").exists())
            self.write_sms_records([(2, 2, 1, "synthetic", BODY)])
            wait_for(lambda: self.cursor() == "2 2")
            time.sleep(1.2)
            self.assertEqual(len(self.requests), 1)
            self.assertFalse(list(self.tmp.glob("last_call*")))
        finally:
            for group in groups:
                try:
                    os.killpg(group, signal.SIGTERM)
                except ProcessLookupError:
                    pass

    @contextlib.contextmanager
    def running_daemon(self):
        process = subprocess.Popen(["bash", str(self.module / "core/sms_daemon.sh")],
                                   env=self.startup_env(), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            yield process
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)

    def test_initial_boundary_skips_history_and_ignores_legacy_state(self):
        self.startup_config()
        (self.tmp / "last_sms_date.txt").write_text("999999\n", encoding="utf-8")
        self.write_sms_records([
            (9, 100, 1, "synthetic", "history nine"),
            (99, 999, 2, "synthetic", "other subscription"),
            (10, 100, 1, "synthetic", "history ten"),
            (50, 99, 1, "synthetic", "older"),
        ])
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.assertEqual(self.cursor_file.read_bytes(), b"100 10\n")
            self.assertEqual(self.requests, [])
            self.assertEqual(self.cursor_file.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.cursor_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.cursor_file.stat().st_uid, os.getuid())

    def test_empty_initial_boundary_then_new_sms(self):
        self.startup_config()
        self.write_sms_records([])
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.assertEqual(self.cursor(), "0 0")
            self.assertEqual(self.requests, [])
            self.write_sms_records([(2, 100, 1, "synthetic", BODY)])
            wait_for(lambda: self.cursor() == "100 2")
            self.assertEqual(len(self.requests), 1)

    def test_initial_query_errors_do_not_create_a_boundary(self):
        self.startup_config()
        provider = (self.commands / "content").read_text(encoding="utf-8")
        for output, code in (("Error while accessing provider", 0), ("", 0),
                             ("No result found.\nError while accessing provider", 0),
                             ("No result found.", 1)):
            with self.subTest(output=output, code=code):
                self.polls.unlink(missing_ok=True)
                self.cursor_file.unlink(missing_ok=True)
                self.write_command("content", f'''#!/bin/sh
case "$*" in
  *siminfo*) cat "$SIMINFO_FIXTURE" ;;
  *) printf '%s\\n' '{output}'; exit {code} ;;
esac
''')
                with self.running_daemon():
                    wait_for(self.polls.exists)
                    self.assertFalse(self.cursor_file.exists())
                    self.assertEqual(self.requests, [])
        self.write_command("content", provider)
        self.polls.unlink(missing_ok=True)
        self.write_sms_records([(5, 500, 1, "synthetic", "history after recovery")])
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.assertEqual(self.cursor(), "500 5")
            self.assertEqual(self.requests, [])

    def test_equal_timestamps_and_61_records_cross_the_batch_boundary(self):
        self.startup_config()
        with self.running_daemon():
            wait_for(self.polls.exists)
            # Reverse source order so the Provider's sort is significant.
            self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                    for i in range(62, 1, -1)])
            wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= 2)
            self.assertEqual(len(self.requests), 50)
            self.assertEqual(self.cursor(), "100 51")
            wait_for(lambda: self.cursor() == "100 62")
            self.assertEqual(len(self.requests), 61)
            self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                              for request in self.requests],
                             [f"message {i}" for i in range(2, 63)])
            self.assertEqual(self.observed_cursors,
                             ["1 1"] + [f"100 {i}" for i in range(2, 62)])
            calls = self.content_calls.read_text(encoding="utf-8")
            self.assertIn("date ASC, _id ASC LIMIT 50", calls)
            self.assert_no_payload_files_or_sensitive_logs()

    def test_failed_record_stops_batch_and_restart_resumes_saved_cursor(self):
        self.startup_config()
        self.reply = lambda count: (200, b'{"code":200}') if count == 1 else (503, b'{}')
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                    for i in (2, 3, 4)])
            wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= 2)
            self.assertEqual(self.cursor(), "100 2")
            self.assertEqual(len(self.requests), 2)
        self.reply = (200, b'{"code":200}')
        with self.running_daemon():
            wait_for(lambda: self.cursor() == "100 4")
            self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                              for request in self.requests],
                             ["message 2", "message 3", "message 3", "message 4"])
            self.assert_no_payload_files_or_sensitive_logs()

    def test_invalid_cursor_stops_without_resetting_it(self):
        self.startup_config()
        self.cursor_file.parent.mkdir()
        for value in ("", "1", "-1 2\n", "1 x\n", "1 2 3\n", "1 2\n3 4\n", "1 2\n\n"):
            with self.subTest(value=value):
                self.cursor_file.write_text(value, encoding="utf-8")
                result = subprocess.run(["busybox", "ash", str(self.module / "core/sms_daemon.sh")],
                                        env=self.startup_env(), capture_output=True, timeout=2)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("进度", result.stderr.decode("utf-8"))
                self.assertEqual(self.cursor_file.read_text(encoding="utf-8"), value)
                self.assertEqual(self.requests, [])

    def test_decode_failure_stops_before_later_records(self):
        self.startup_config()
        provider = (self.commands / "content").read_text(encoding="utf-8")
        # An odd-length hex value is a malformed external Provider record.
        self.write_command("content", provider.replace(
            'for index, row in enumerate(rows):',
            'for index, row in enumerate(rows):\n'
            '    if row[0] == 3 and "body_hex" in names:\n'
            '        row = (*row[:-1], "A")'))
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                    for i in (2, 3, 4)])
            wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= 2)
            self.assertEqual(self.cursor(), "100 2")
            self.assertEqual(len(self.requests), 1)
            self.write_command("content", provider)
            wait_for(lambda: self.cursor() == "100 4")
            self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                              for request in self.requests],
                             ["message 2", "message 3", "message 4"])
            self.assert_no_payload_files_or_sensitive_logs()

    def test_commit_failure_stops_before_later_records(self):
        self.startup_config()
        pending = self.module / "state/sms.cursor.tmp"

        def block_commit(count):
            if count == 2:
                pending.mkdir(exist_ok=True)
            return 200, b'{"code":200}'

        self.reply = block_commit
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                    for i in (2, 3, 4)])
            wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= 2)
            self.assertEqual(self.cursor(), "100 2")
            self.assertEqual(len(self.requests), 2)
            self.assertIn("进度提交失败", (self.tmp / "sms_forward_run.log").read_text(encoding="utf-8"))
            pending.rmdir()
            self.reply = (200, b'{"code":200}')
            wait_for(lambda: self.cursor() == "100 4")
            self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                              for request in self.requests],
                             ["message 2", "message 3", "message 3", "message 4"])
            self.assert_no_payload_files_or_sensitive_logs()

    def test_directory_at_cursor_destination_is_a_commit_failure(self):
        self.startup_config()
        saved = self.module / "saved.cursor"

        def replace_destination(count):
            if count == 2:
                self.cursor_file.rename(saved)
                self.cursor_file.mkdir()
            return 200, b'{"code":200}'

        self.reply = replace_destination
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                    for i in (2, 3, 4)])
            wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= 2)
            self.assertEqual(len(self.requests), 2)
            self.assertEqual(saved.read_bytes(), b"100 2\n")
            self.assertEqual(list(self.cursor_file.iterdir()), [])
            self.assertIn("进度提交失败", (self.tmp / "sms_forward_run.log").read_text(encoding="utf-8"))
        self.cursor_file.rmdir()
        saved.replace(self.cursor_file)
        self.reply = (200, b'{"code":200}')
        with self.running_daemon():
            wait_for(lambda: self.cursor() == "100 4")
            self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                              for request in self.requests],
                             ["message 2", "message 3", "message 3", "message 4"])
            self.assert_no_payload_files_or_sensitive_logs()

    def test_siminfo_error_after_a_row_does_not_initialize_or_advance(self):
        self.startup_config()
        self.siminfo_fixture.write_text(
            "Row: 0 _id=1, sim_id=0\nError while accessing provider\n", encoding="utf-8")
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.assertFalse(self.cursor_file.exists())
            self.siminfo_fixture.write_text("Row: 0 _id=1, sim_id=0\n", encoding="utf-8")
            wait_for(lambda: self.cursor_file.exists() and self.cursor() == "1 1")
            wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= 2)
            poll_count = self.polls.read_text(encoding="utf-8").count("poll")
            self.siminfo_fixture.write_text(
                "Row: 0 _id=1, sim_id=0\nError while accessing provider\n", encoding="utf-8")
            self.write_sms_records([(2, 100, 1, "synthetic", BODY)])
            wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") > poll_count)
            self.assertEqual(self.cursor(), "1 1")
            self.assertEqual(self.requests, [])
            self.siminfo_fixture.write_text("Row: 0 _id=1, sim_id=0\n", encoding="utf-8")
            wait_for(lambda: self.cursor() == "100 2")
            self.assertEqual(len(self.requests), 1)

    def test_provider_poll_errors_preserve_cursor_and_recover(self):
        self.startup_config()
        provider = (self.commands / "content").read_text(encoding="utf-8")
        cases = [("Error while accessing provider", 0, ""),
                 ("No result found.", 1, ""),
                 ("No result found.", 0, "Error while accessing provider"),
                 ("Row: 0 invalid", 0, "")]
        with self.running_daemon():
            wait_for(self.polls.exists)
            for message_id, (output, code, error) in enumerate(cases, start=2):
                with self.subTest(output=output, code=code, error=error):
                    previous = self.cursor()
                    poll_count = self.polls.read_text(encoding="utf-8").count("poll")
                    self.write_command("content", f'''#!/bin/sh
case "$*" in
  *siminfo*) cat "$SIMINFO_FIXTURE" ;;
  *) printf '%s\\n' '{output}'; printf '%s' '{error}' >&2; exit {code} ;;
esac
''')
                    self.write_sms_records([(message_id, 100, 1, "synthetic", BODY)])
                    wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") > poll_count)
                    self.assertEqual(self.cursor(), previous)
                    self.assertEqual(len(self.requests), message_id - 2)
                    self.write_command("content", provider)
                    wait_for(lambda: self.cursor() == f"100 {message_id}")
                    wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= poll_count + 2)
            self.assertEqual(len(self.requests), len(cases))
            self.assert_no_payload_files_or_sensitive_logs()

    def test_deleted_failed_sms_is_not_replayed_from_the_previous_batch(self):
        self.startup_config()
        self.reply = lambda count: (200, b'{"code":200}') if count == 1 else (503, b'{}')
        with self.running_daemon():
            wait_for(self.polls.exists)
            self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                    for i in (2, 3, 4)])
            wait_for(lambda: self.polls.read_text(encoding="utf-8").count("poll") >= 2)
            self.assertEqual(self.cursor(), "100 2")
            self.assertEqual(len(self.requests), 2)
            # Remove the failed source and change the next source to prove a reread.
            self.write_sms_records([(4, 100, 1, "synthetic", "updated message 4")])
            self.reply = (200, b'{"code":200}')
            wait_for(lambda: self.cursor() == "100 4")
            self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                              for request in self.requests],
                             ["message 2", "message 3", "updated message 4"])
            self.assertGreaterEqual(self.request_times[2] - self.request_times[1], 0.9)
            self.assert_no_payload_files_or_sensitive_logs()

    def test_kill_after_success_before_replace_replays_uncommitted_sms(self):
        self.startup_config()
        reached = self.module / "replace-reached"
        block = self.module / "block-replace"
        self.write_command("mv", '''#!/bin/sh
for destination do :; done
module=${destination%/state/sms.cursor}
if [ "$module" != "$destination" ] && [ -f "$module/block-replace" ]; then
    touch "$module/replace-reached"
    while true; do /bin/sleep 0.1; done
fi
exec /bin/mv "$@"
''')
        with self.running_daemon() as process:
            wait_for(self.polls.exists)
            block.touch()
            self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                    for i in (2, 3)])
            wait_for(reached.exists)
            self.assertEqual(self.cursor_file.read_bytes(), b"1 1\n")
            self.assertEqual((self.module / "state/sms.cursor.tmp").read_bytes(), b"100 2\n")
            self.assertEqual(len(self.requests), 1)
            self.assertIn("Bark 服务确认成功", (self.tmp / "sms_forward_run.log").read_text(encoding="utf-8"))
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
        block.unlink()
        with self.running_daemon():
            wait_for(lambda: self.cursor() == "100 3")
            self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                              for request in self.requests],
                             ["message 2", "message 2", "message 3"])
            self.assert_no_payload_files_or_sensitive_logs()

    @contextlib.contextmanager
    def daemon(self, subscription_id=1, siminfo=None, initial_new_sms=True):
        if siminfo is None:
            siminfo = f"Row: 0 _id={subscription_id}, sim_id=0\n"
        self.siminfo_fixture.write_text(siminfo, encoding="utf-8")
        self.write_sms_records([(1, 1, subscription_id, "synthetic-sender", "history")])
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
        env = self.startup_env()
        env.update(TMP_DIR=str(self.tmp), CURL_HOME=str(self.module))
        process = subprocess.Popen(["bash", str(self.module / "core/sms_daemon.sh")],
                                   env=env, start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(self.polls.exists)
            self.assertEqual(self.cursor(), "1 1")
            self.assertEqual(self.requests, [])
            if initial_new_sms:
                self.write_sms_records([(2, 2, subscription_id, "synthetic-sender", BODY)])
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
                         {"sms_forward_run.log"})
        self.assertEqual({path.name for path in self.cursor_file.parent.iterdir()}, {"sms.cursor"})

    def test_plain_sms_is_one_utf8_json_post(self):
        with self.daemon():
            self.wait_for_attempt()
            self.assertEqual(self.cursor(), "2 2")
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

    def test_verification_code_is_one_bark_notification_with_copy(self):
        body = "您的验证码是 123456"
        with self.daemon():
            self.write_sms_records([(2, 2, 1, "service", body)])
            wait_for(lambda: self.cursor() == "2 2")
            time.sleep(0.1)
            self.assertEqual(len(self.requests), 1)
            data = json.loads(self.requests[0][2].decode("utf-8"))
            self.assertEqual(data["copy"], "123456")
            self.assertIn(body, data["body"])

    def test_sim1_sms_is_complete_and_uses_the_mapped_subscription(self):
        subscription_id = 42
        sms_date = 1700000000123
        address = "短信服务 Row: 2"
        body = '验证码为 812345\nRow: 0, date=123, address=fake\r\t引号"反斜杠\\🙂'
        siminfo = "Row: 0 _id=1, sim_id=1\nRow: 1 _id=42, sim_id=0\n"
        with self.daemon(subscription_id=subscription_id, siminfo=siminfo):
            self.write_sms_records([
                (3, sms_date + 1, 1, "other-sender", "other subscription"),
                (2, sms_date, subscription_id, address, body),
                (1, 1, subscription_id, "synthetic-sender", "history"),
            ])
            wait_for(lambda: self.cursor() == f"{sms_date} 2")
            wait_for(lambda: len(self.requests) == 1)
            data = json.loads(self.requests[0][2].decode("utf-8"))
            received_at = datetime.fromtimestamp(
                sms_date / 1000, ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
            received_at += f".{sms_date % 1000:03d}"
            self.assertEqual(data["title"], "【短信】 812345")
            self.assertEqual(data["copy"], "812345")
            self.assertIn(address, data["body"])
            self.assertIn(received_at, data["body"])
            self.assertIn(body, data["body"])
            self.assertNotIn("other-sender", data["body"])
            calls = self.content_calls.read_text(encoding="utf-8")
            self.assertIn("content://telephony/siminfo", calls)
            self.assertIn("--where sub_id=42", calls)

    def test_missing_sim1_mapping_does_not_initialize_progress(self):
        self.startup_config()
        self.siminfo_fixture.write_text("Row: 0 _id=1, sim_id=1\n", encoding="utf-8")
        self.write_sms_records([(2, 2, 1, "synthetic-sender", BODY)])
        process = subprocess.Popen(["bash", str(self.module / "core/sms_daemon.sh")],
                                   env=self.startup_env(), start_new_session=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(lambda: "content://telephony/siminfo" in
                     self.content_calls.read_text(encoding="utf-8"))
            time.sleep(1.2)
            cursor_file = self.cursor_file
            self.assertFalse(cursor_file.exists())
            self.assertEqual(self.requests, [])
            self.assertNotIn("--projection date", self.content_calls.read_text(encoding="utf-8"))
            cursor_file.parent.mkdir(exist_ok=True)
            cursor_file.write_text("1 1\n", encoding="utf-8")
            time.sleep(1.2)
            self.assertEqual(self.cursor(), "1 1")
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=3)

    def test_supported_code_patterns_have_fixed_priority_and_keep_unmatched_sms(self):
        cases = [
            ("验证码为 1234", "1234"),
            ("Your code is 654321", "654321"),
            ("验证码 12345678", "12345678"),
            ("验证码 123", None),
            ("验证码 123456789", None),
            ("订单号 123456", None),
            ("code 1111 验证码 2222", "1111"),
        ]
        with self.daemon(initial_new_sms=False):
            for sms_date, (body, code) in enumerate(cases, start=3):
                self.write_sms_records([
                    (sms_date, sms_date, 1, "synthetic-sender", body),
                    (1, 1, 1, "synthetic-sender", "history"),
                ])
                wait_for(lambda: self.cursor() == f"{sms_date} {sms_date}")
                wait_for(lambda: len(self.requests) == sms_date - 2)
                data = json.loads(self.requests[-1][2].decode("utf-8"))
                if code is None:
                    self.assertEqual(data["title"], "【短信】")
                    self.assertNotIn("copy", data)
                else:
                    self.assertEqual(data["title"], f"【短信】 {code}")
                    self.assertEqual(data["copy"], code)
                self.assertIn(body, data["body"])

    def test_failure_response_does_not_advance_caller(self):
        cases = [
            (None, b''),
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
                self.reply = lambda count, failure=(status, body): (
                    (200, b'{"code":200}') if count == 1 else failure)
                # Each process gets a fresh initial boundary.
                self.polls.unlink(missing_ok=True)
                self.cursor_file.unlink(missing_ok=True)
                self.requests.clear()
                self.request_times.clear()
                with self.daemon(initial_new_sms=False):
                    self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                            for i in (2, 3, 4)])
                    self.wait_for_attempt()
                    self.assertEqual(self.cursor(), "100 2")
                    self.assertEqual(len(self.requests), 2)
                    self.reply = (200, b'{"code":200}')
                    wait_for(lambda: self.cursor() == "100 4")
                    self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                                      for request in self.requests],
                                     ["message 2", "message 3", "message 3", "message 4"])
                    self.assertGreaterEqual(self.request_times[2] - self.request_times[1], 0.9)
                    self.assert_no_payload_files_or_sensitive_logs()

    def test_client_exit_error_rejects_even_success_looking_output(self):
        client = self.module / "bin/curl"
        client.unlink()
        client.write_text('#!/bin/sh\nprintf \'{"code":200}\\n200\'\nexit 7\n', encoding="utf-8")
        client.chmod(0o755)
        with self.daemon():
            self.wait_for_attempt()
            self.assertEqual(self.cursor(), "1 1")
            self.assert_no_payload_files_or_sensitive_logs()

    def test_request_timeout_is_bounded_without_internal_retry(self):
        def timeout_second(count):
            if count == 2:
                time.sleep(7)
            return 200, b'{"code":200}'

        self.reply = timeout_second
        with self.daemon(initial_new_sms=False):
            started = time.monotonic()
            self.write_sms_records([(i, 100, 1, "synthetic", f"message {i}")
                                    for i in (2, 3, 4)])
            self.wait_for_attempt()
            failed_round_finished = time.monotonic()
            self.assertGreater(time.monotonic() - started, 4.5)
            self.assertLess(time.monotonic() - started, 7)
            self.assertEqual(self.cursor(), "100 2")
            self.assertEqual(len(self.requests), 2)
            self.reply = (200, b'{"code":200}')
            wait_for(lambda: self.cursor() == "100 4")
            self.assertEqual([json.loads(request[2])["body"].splitlines()[-1]
                              for request in self.requests],
                             ["message 2", "message 3", "message 3", "message 4"])
            self.assertGreaterEqual(self.request_times[2] - failed_round_finished, 0.9)
            self.assert_no_payload_files_or_sensitive_logs()

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
            self.assertEqual(self.cursor(), "1 1")
            self.assertEqual(self.requests, [])

    def test_remote_http_and_loopback_userinfo_are_rejected(self):
        for url in ("http://example.com/push", "http://localhost:80@example.com/push"):
            with self.subTest(url=url):
                self.url = url
                self.polls.unlink(missing_ok=True)
                self.cursor_file.unlink(missing_ok=True)
                self.startup_config()
                result = subprocess.run(["busybox", "ash", str(self.module / "core/sms_daemon.sh")],
                                        env=self.startup_env(), capture_output=True, timeout=2)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.cursor_file.exists())
                self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
