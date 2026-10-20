"""Explicit device smoke check: python tests/check_device.py.

Requires one authorized rooted Honor8 on ADB. Uses only a disposable directory,
synthetic Provider output and a loopback HTTP server; never queries real SMS.
"""

import http.server
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
ADB = shutil.which("adb")


def adb(*arguments, check=True):
    return subprocess.run([ADB, *arguments], check=check, capture_output=True,
                          encoding="utf-8", errors="strict", timeout=20)


def root(command, check=True):
    return adb("shell", "su -c " + shlex.quote(command), check=check)


def main():
    if not ADB:
        raise RuntimeError("adb is required")
    remote = "/data/local/tmp/honor8-bark-t01-" + uuid.uuid4().hex
    received = []

    class Receiver(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"code":200,"message":"success"}')

        def log_message(self, *_):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    pid = None
    try:
        model = adb("shell", "getprop ro.product.model").stdout.strip()
        android = adb("shell", "getprop ro.build.version.release").stdout.strip()
        assert model == "FRD-AL00" and android == "8.0.0", (model, android)
        adb("reverse", f"tcp:{port}", f"tcp:{port}")
        with tempfile.TemporaryDirectory(prefix="bark-device-") as directory:
            stage = Path(directory)
            shutil.copytree(ROOT / "core", stage / "core")
            (stage / "bin").mkdir()
            (stage / "commands").mkdir()
            for name in ("curl", "jq", "cacert.pem"):
                shutil.copyfile(ROOT / "bin" / name, stage / "bin" / name)
            config = stage / "core/config.conf"
            with config.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(f'''\nPUSH_CHANNEL="Bark"
BARK_API_URL="http://127.0.0.1:{port}/custom/push"
BARK_DEVICE_KEY="synthetic-device-key"
TMP_DIR="{remote}/runtime"
ENABLE_MISSED_CALL="false"
ENABLE_INCOMING_CALL="false"
ENABLE_OUTGOING_CALL="false"
OS_TYPE="EMUI"
INTERVAL=1
''')
            (stage / "commands/content").write_text(f'''#!/system/bin/sh
case "$*" in
  *"--projection date "*) printf 'Row: 0 date=1\\n' ;;
  *) cat '{remote}/fixture' ;;
esac
''', encoding="utf-8", newline="\n")
            (stage / "fixture").write_text("Row: 0 address=synthetic, body=history, date=1, sub_id=1\n", encoding="utf-8")
            (stage / "run.sh").write_text(f'''#!/system/bin/sh
export ASH_STANDALONE=1
export TMP_DIR='{remote}/runtime'
export PATH='{remote}/commands':$PATH
nohup /data/adb/magisk/busybox ash '{remote}/core/sms_daemon.sh' >/dev/null 2>&1 &
echo $!
''', encoding="utf-8", newline="\n")
            adb("shell", "mkdir", remote)
            adb("push", str(stage) + os.sep + ".", remote + "/")
            root(f"chmod 700 {remote}; mkdir {remote}/runtime; chmod 755 {remote}/bin/curl {remote}/bin/jq {remote}/commands/content")
            print(root(f"{remote}/bin/curl --version").stdout.splitlines()[0])
            print(root(f"{remote}/bin/jq --version").stdout.strip())
            client = f"{remote}/bin/curl -q -s --connect-timeout 3 --max-time 5 --cacert {remote}/bin/cacert.pem -o /dev/null -w '%{{http_code}}'"
            valid = root(client + " https://example.com/", check=False)
            assert valid.returncode == 0 and valid.stdout.strip() == "200", valid
            invalid = root(client + " https://self-signed.badssl.com/", check=False)
            assert invalid.returncode == 60, invalid
            print("HTTPS: trusted endpoint HTTP 200; untrusted certificate curl exit 60")
            root(f"/data/adb/magisk/busybox ash -n {remote}/core/sms_daemon.sh")
            pid = root(f"/data/adb/magisk/busybox ash {remote}/run.sh").stdout.strip()
            assert pid.isdecimal(), pid
            deadline = time.monotonic() + 8
            while root(f"cat {remote}/runtime/last_sms_date.txt", check=False).stdout.strip() != "1":
                assert time.monotonic() < deadline, "initial boundary missing"
                time.sleep(0.1)
            assert not received
            body = "普通合成短信：荣耀8 UTF-8 验证🙂"
            (stage / "fixture").write_text(f"Row: 0 address=synthetic, body={body}, date=2, sub_id=1\n", encoding="utf-8")
            adb("push", str(stage / "fixture"), "/data/local/tmp/" + Path(remote).name + ".fixture")
            root(f"mv {remote}.fixture {remote}/fixture")
            deadline = time.monotonic() + 8
            while root(f"cat {remote}/runtime/last_sms_date.txt").stdout.strip() != "2":
                assert time.monotonic() < deadline, "send did not advance boundary"
                time.sleep(0.1)
            time.sleep(1.2)
            assert len(received) == 1, len(received)
            path, data = received[0]
            assert path == "/custom/push"
            assert data["device_key"] == "synthetic-device-key"
            assert body in data["body"]
            log = root(f"cat {remote}/runtime/sms_forward_run.log").stdout
            assert body not in log and data["device_key"] not in log
            files = root(f"ls {remote}/runtime").stdout.split()
            assert sorted(files) == ["last_sms_date.txt", "sms_forward_run.log"], files
            print("FRD-AL00 / Android 8 / Magisk ash: one synthetic UTF-8 JSON POST, caller advanced 1 -> 2; no payload file")
    finally:
        if pid and pid.isdecimal():
            root(f"kill {pid}", check=False)
        # Only the exact UUID-named disposable directory created above is removed.
        root(f"rm -rf {remote}; rm -f {remote}.fixture", check=False)
        adb("reverse", "--remove", f"tcp:{port}", check=False)
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    main()
