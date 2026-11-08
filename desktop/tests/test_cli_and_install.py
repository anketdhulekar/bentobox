"""bentobox.py and install.py as real processes, each with a temp HOME and fake binaries. Nothing here changes the real
hooks, launchd or Keychain: launchd and the installer's real writes are only ever run with --dry-run."""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import helpers
from helpers import HomeCase

APP_SCRIPT = os.path.join(helpers.DESKTOP, "bentobox.py")
INSTALL = os.path.join(helpers.DESKTOP, "install.py")
HOOK = os.path.join(helpers.DESKTOP, "agents", "hook.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def snapshot(folder):
    """Every file under a folder, with its content."""
    out = {}
    for root, _, files in os.walk(folder):
        for name in files:
            path = os.path.join(root, name)
            with open(path, "rb") as f:
                out[path] = f.read()
    return out


class Process(unittest.TestCase):
    """A temp HOME, with a config that has fake tools, so that nothing real is called."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="bentobox-test-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.port = free_port()
        self.tool_log = os.path.join(self.home, "tool.log")
        self.tool = os.path.join(self.home, "fake-tool")
        with open(self.tool, "w") as f:
            f.write(f'#!/bin/sh\necho "$0 $*" >> {self.tool_log}\nexit 1\n')
        os.chmod(self.tool, 0o755)
        self.write_config()
        self.env = dict(os.environ, HOME=self.home, PYTHONDONTWRITEBYTECODE="1")

    def write_config(self, **extra):
        folder = os.path.join(self.home, ".config", "bentobox")
        os.makedirs(folder, exist_ok=True)
        cfg = {"http_port": self.port, "claude_bin": self.tool, "rex_bin": self.tool, "security_bin": self.tool,
               "poll_seconds": 3600, **extra}
        with open(os.path.join(folder, "config.json"), "w") as f:
            json.dump(cfg, f)

    def cli(self, *argv, stdin=None):
        return subprocess.run([sys.executable, APP_SCRIPT, *argv], input=stdin, capture_output=True, text=True, env=self.env,
                              timeout=30, stdin=None if stdin is not None else subprocess.DEVNULL)

    def start(self, *argv):
        proc = subprocess.Popen([sys.executable, APP_SCRIPT, "run", *argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=self.env, text=True)

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()
        self.addCleanup(stop)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(f"it stopped: {proc.stdout.read()}")
            if self.cli("status").returncode == 0:
                return proc
            time.sleep(0.1)
        self.fail("no answer from status")

    def request(self, method, path, headers=None, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, body=body, headers=headers or {})
            r = c.getresponse()
            return r.status, r.read()
        finally:
            c.close()


class Run(Process):
    def test_status_with_nothing_running_says_so(self):
        res = self.cli("status")
        self.assertEqual(res.returncode, 1)
        self.assertIn("isn't running", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    def test_something_else_on_the_port_isnt_bento(self):
        import http.server
        import threading

        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass
        server = http.server.HTTPServer(("127.0.0.1", self.port), Other)
        threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        res = self.cli("status")
        self.assertEqual(res.returncode, 1)
        self.assertNotIn("Traceback", res.stderr)

    def test_a_bad_port_setting_never_fails_the_hook(self):
        env = dict(self.env, BENTOBOX_PORT="nonsense")
        res = subprocess.run([sys.executable, HOOK], input="{}", text=True, env=env, timeout=10)
        self.assertEqual(res.returncode, 0)

    def test_no_display_starts_answers_status_and_stops_cleanly(self):
        proc = self.start("--no-display")
        res = self.cli("status")
        self.assertEqual(res.returncode, 0)
        self.assertIn("Board: off (--no-display)", res.stdout)
        self.assertIn("No agents.", res.stdout)
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_no_display_never_asks_the_keychain_or_toggl(self):
        self.start("--no-display")
        self.cli("status")
        if os.path.exists(self.tool_log):
            with open(self.tool_log) as f:
                calls = f.read()
            self.assertNotIn("password", calls)

    def test_only_this_mac_can_reach_it_and_hooks_work(self):
        self.start("--no-display")
        self.assertEqual(self.request("GET", "/state")[0], 200)
        self.assertEqual(self.request("GET", "/state", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/state", {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/reload", {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("POST", "/reload")[0], 204)
        event = {"hook_event_name": "PreToolUse", "session_id": "e2e", "cwd": "/a/b/demo", "tool_name": "Read",
                 "tool_input": {"file_path": "/a/b/demo/notes.txt"}}
        env = dict(self.env, BENTOBOX_PORT=str(self.port))
        res = subprocess.run([sys.executable, HOOK], input=json.dumps(event), text=True, env=env, timeout=10)
        self.assertEqual(res.returncode, 0)
        res = self.cli("status")
        self.assertIn("Reading notes.txt", res.stdout)
        state = json.loads(self.request("GET", "/state")[1])
        self.assertEqual(state["agents"][0]["act"], "Reading notes.txt")

    def test_a_second_app_says_so_instead_of_a_traceback(self):
        self.start("--no-display")
        res = subprocess.run([sys.executable, APP_SCRIPT, "run", "--no-display"], capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(res.returncode, 1)
        self.assertIn("in use", res.stderr)
        self.assertNotIn("Traceback", res.stderr)

    def test_a_normal_run_over_tcp_reaches_a_board_and_its_keys_stay_private(self):
        """The whole app, as launchd would run it, against the fake board."""
        from fake_board import FakeBoard
        board = FakeBoard()
        self.addCleanup(board.stop)
        self.write_config(link="tcp", display_port=board.port)
        proc = self.start()
        board.wait_for("hello", timeout=10)
        state = board.wait_for("state")
        self.assertEqual((state["toggl"], state["voice"]), ("no_key", False))  # the fake tool says there are no keys
        res = self.cli("status")
        self.assertIn("Board: connected", res.stdout)
        self.assertIn("Toggl: no_key", res.stdout)
        # A lock code set from the CLI, while it runs, reaches the board.
        res = self.cli("lock-code", "set", stdin=helpers.LOCK_CODE + "\n")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("The boards connected now have it", res.stdout)
        msg = board.wait_for("lock_code")
        self.assertEqual(set(msg) - {"_len"}, {"t", "salt", "iter", "hash"})
        with open(self.tool_log) as f:
            for call in f:
                self.assertNotIn("add-generic-password", call)
                self.assertNotIn(" -w", call)  # with no key present, no key is read
        proc.send_signal(signal.SIGTERM)
        output = proc.stdout.read()
        self.assertEqual(proc.wait(timeout=20), 0)
        for secret in (helpers.LOCK_CODE, msg["hash"], msg["salt"]):
            self.assertNotIn(secret, output)

    def test_a_config_that_would_send_keys_in_the_clear_is_refused(self):
        self.write_config(toggl_api="http://focus.toggl.com/api")
        res = subprocess.run([sys.executable, APP_SCRIPT, "run", "--no-display"], capture_output=True, text=True, env=self.env, timeout=20)
        self.assertEqual(res.returncode, 1)
        self.assertIn("isn't HTTPS", res.stderr)

    def test_init(self):
        os.remove(os.path.join(self.home, ".config", "bentobox", "config.json"))
        res = self.cli("init")
        self.assertEqual(res.returncode, 0, res.stderr)
        path = os.path.join(self.home, ".config", "bentobox", "config.json")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        with open(path) as f:
            cfg = json.load(f)
        self.assertEqual((cfg["http_port"], cfg["link"], cfg["display_port"]), (47824, "ble", 7777))
        self.assertNotIn("toggl_api", cfg)  # the addresses stay defaults unless someone changes them

    def test_help_and_unknown_commands(self):
        self.assertEqual(self.cli("--help").returncode, 0)
        self.assertEqual(self.cli("nonsense").returncode, 2)


class Installer(HomeCase):
    def setUp(self):
        super().setUp()
        self.env = dict(os.environ, HOME=self.home, PYTHONDONTWRITEBYTECODE="1")

    def run_install(self, *argv):
        return subprocess.run([sys.executable, INSTALL, *argv], capture_output=True, text=True, env=self.env, timeout=30)

    def make_existing_settings(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as f:
            json.dump({"env": {"TOKEN": "PRIVATE-VALUE"}, "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}, f)

    def test_hooks_dry_run_changes_nothing_and_shows_only_hooks(self):
        self.make_existing_settings()
        os.makedirs(os.path.join(self.home, ".codex"))
        os.makedirs(os.path.join(self.home, ".grok"))
        before = snapshot(self.home)
        res = self.run_install("hooks", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertNotIn("PRIVATE-VALUE", res.stdout)
        self.assertIn(HOOK, res.stdout)
        self.assertIn(f"{HOOK} --agent codex", res.stdout)
        self.assertIn('"mine"', res.stdout)  # your own hooks stay
        self.assertIn(os.path.join(self.home, ".grok", "hooks", "bentobox.json"), res.stdout)
        self.assertNotIn("zoo_hook", res.stdout)

    def test_hooks_dry_run_with_no_agents_installed(self):
        res = self.run_install("hooks", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(os.path.join(self.home, ".claude", "settings.json"), res.stdout)
        self.assertNotIn(".codex", res.stdout)
        self.assertNotIn(".grok", res.stdout)
        self.assertEqual(snapshot(self.home), {})

    def test_launchd_dry_run(self):
        res = self.run_install("launchd", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("local.bentobox", res.stdout)
        self.assertIn("bentobox-launcher", res.stdout)
        self.assertIn(os.path.join(helpers.DESKTOP, "bentobox.py"), res.stdout)
        self.assertIn(os.path.join(self.home, "Library", "Logs", "bentobox.log"), res.stdout)
        self.assertEqual(snapshot(self.home), {})

    def test_setup_and_app_dry_run(self):
        desktop_before = sorted(os.listdir(helpers.DESKTOP))
        for what in ("setup", "app"):
            res = self.run_install(what, "--dry-run")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("Bentobox.app", res.stdout)
        self.assertEqual(snapshot(self.home), {})
        self.assertEqual(sorted(os.listdir(helpers.DESKTOP)), desktop_before)

    def test_uninstall_dry_run_changes_nothing(self):
        self.make_existing_settings()
        before = snapshot(self.home)
        res = self.run_install("uninstall", "--dry-run")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(snapshot(self.home), before)
        self.assertIn("Would stop local.bentobox", res.stdout)

    def test_the_macos_files_name_bento(self):
        with open(os.path.join(helpers.DESKTOP, "macos", "Info.plist")) as f:
            plist = f.read()
        self.assertIn("<string>local.bentobox</string>", plist)
        self.assertIn("<string>bentobox-launcher</string>", plist)
        self.assertIn("NSBluetoothAlwaysUsageDescription", plist)

    def test_the_hook_runs_on_the_oldest_python_it_can_meet(self):
        old = "/usr/bin/python3"
        if not os.path.exists(old):
            self.skipTest("no system Python on this Mac")
        res = subprocess.run([old, "-c", f"import ast,sys; ast.parse(open({HOOK!r}).read(), feature_version=(3, 9))"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
