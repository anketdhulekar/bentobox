"""Tests for tools/bentobox. They never touch launchd, the Keychain or a running Bentobox: launchctl, the app and its port
are replaced, and every path points into a temporary folder."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.dont_write_bytecode = True


def load_tool():
    loader = importlib.machinery.SourceFileLoader("bentobox_tool", os.path.join(TOOLS, "bentobox"))
    spec = importlib.util.spec_from_loader("bentobox_tool", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


RUNNING = """gui/501/local.bentobox = {
\tactive count = 1
\tpath = /Users/me/Library/LaunchAgents/local.bentobox.plist
\tstate = running
\tprogram = /x/bentobox-launcher
\tpid = 4242
\tendpoints = {
\t\t"com.apple.x" = {
\t\t\tstate = active
\t\t}
\t}
}
"""
STOPPED = RUNNING.replace("\tstate = running", "\tstate = not running").replace("\tpid = 4242\n", "")


class Launchctl:
    """Stands in for subprocess.run: answers `launchctl print` with `printed` (None: not loaded), records the rest."""

    def __init__(self, printed=None, app_out=""):
        self.printed, self.app_out, self.calls = printed, app_out, []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        if argv[:2] == ["launchctl", "print"]:
            return mock.Mock(returncode=0 if self.printed else 113, stdout=self.printed or "")
        return mock.Mock(returncode=0, stdout=self.app_out)

    def changes(self):
        return [c for c in self.calls if c[0] == "launchctl" and c[1] != "print"]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.t = load_tool()
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        here = os.path.join(self.d.name, "repo", "desktop")
        os.makedirs(os.path.join(here, ".venv", "bin"))
        self.paths = {
            "VENV_PYTHON": os.path.join(here, ".venv", "bin", "python"),
            "LAUNCHER": os.path.join(here, "build", "Bentobox.app", "Contents", "MacOS", "bentobox-launcher"),
            "APP_SCRIPT": os.path.join(here, "bentobox.py"),
            "PLIST": os.path.join(self.d.name, "LaunchAgents", "local.bentobox.plist"),
            "LOG": os.path.join(self.d.name, "bentobox.log"),
        }
        for name, path in self.paths.items():
            self.enterContext(mock.patch.object(self.t.install, name, path))
        self.cfg = os.path.join(self.d.name, "config.json")
        self.enterContext(mock.patch.object(self.t.config, "config_path", lambda: self.cfg))
        self.enterContext(mock.patch.object(self.t.config, "load_config", lambda: {"http_port": 47999}))
        self.enterContext(mock.patch.object(self.t, "START_WAIT_S", 0))
        self.launchd_installs, self.answers_once_started = [], True
        self.enterContext(mock.patch.object(self.t.install, "install_launchd", self.install_launchd))
        self.up = False
        self.enterContext(mock.patch.object(self.t, "answering", lambda p: self.up))

    def install_launchd(self, dry_run):
        self.launchd_installs.append(dry_run)
        if not dry_run:
            self.up = self.answers_once_started

    def enterContext(self, cm):  # unittest's own is 3.11+; this runs on 3.9 too
        value = cm.__enter__()
        self.addCleanup(cm.__exit__, None, None, None)
        return value

    def set_up(self, env=True, app=True, cfg=True):
        for path, wanted in ((self.paths["VENV_PYTHON"], env), (self.paths["LAUNCHER"], app), (self.cfg, cfg)):
            if wanted:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w").close()

    def plist(self, script):
        os.makedirs(os.path.dirname(self.paths["PLIST"]), exist_ok=True)
        with open(self.paths["PLIST"], "wb") as f:
            plistlib.dump({"Label": "local.bentobox", "ProgramArguments": ["/x/launcher", "/x/python", script, "run"]}, f)

    def run_tool(self, *argv, launchctl=None):
        launchctl = launchctl or Launchctl()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.t.subprocess, "run", launchctl), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            try:
                code = self.t.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue() + err.getvalue(), launchctl

    # -- what launchd says
    def test_launchds_own_state_and_pid(self):
        with mock.patch.object(self.t.subprocess, "run", Launchctl(RUNNING)):
            self.assertEqual(self.t.service(), {"state": "running", "pid": 4242})
        with mock.patch.object(self.t.subprocess, "run", Launchctl(STOPPED)):
            self.assertEqual(self.t.service(), {"state": "not running", "pid": 0})
        with mock.patch.object(self.t.subprocess, "run", Launchctl(None)):
            self.assertIsNone(self.t.service())

    def test_the_copy_the_agent_runs(self):
        self.assertEqual(self.t.another_copy(), "")  # none installed
        self.plist(self.paths["APP_SCRIPT"])
        self.assertEqual(self.t.another_copy(), "")
        self.plist("/Users/me/old-clone/desktop/bentobox.py")
        self.assertEqual(self.t.another_copy(), "/Users/me/old-clone/desktop/bentobox.py")

    # -- start
    def test_start_says_which_step_comes_first(self):
        code, out, launchctl = self.run_tool("start")
        self.assertIn("install.py setup", str(code))
        self.set_up(app=False)
        code, out, launchctl = self.run_tool("start")
        self.assertIn("install.py app", str(code))
        self.set_up(cfg=False)
        os.remove(self.cfg)
        code, out, launchctl = self.run_tool("start")
        self.assertIn("bentobox.py init", str(code))
        self.assertEqual((self.launchd_installs, launchctl.changes()), ([], []))

    def test_start_installs_and_waits_for_an_answer(self):
        self.set_up()
        code, out, _ = self.run_tool("start")
        self.assertEqual((code, self.launchd_installs), (0, [False]))
        self.assertIn("answers on 127.0.0.1:47999", out)

    def test_start_that_never_answers_shows_the_log(self):
        self.set_up()
        self.answers_once_started = False
        with open(self.paths["LOG"], "w") as f:
            f.write("board: something went wrong\n")
        code, out, _ = self.run_tool("start")
        self.assertEqual(code, 1)
        self.assertIn("something went wrong", out)

    def test_start_dry_run_changes_nothing(self):
        self.set_up()
        code, out, launchctl = self.run_tool("start", "--dry-run")
        self.assertEqual((code, self.launchd_installs, launchctl.changes()), (0, [True], []))

    def test_start_when_already_running_does_nothing(self):
        self.set_up()
        self.plist(self.paths["APP_SCRIPT"])
        self.up = True
        code, out, launchctl = self.run_tool("start", launchctl=Launchctl(RUNNING))
        self.assertEqual((code, self.launchd_installs, launchctl.changes()), (0, [], []))
        self.assertIn("already running", out)

    def test_start_leaves_another_copy_alone_unless_asked(self):
        self.set_up()
        self.plist("/Users/me/old-clone/desktop/bentobox.py")
        code, out, _ = self.run_tool("start", launchctl=Launchctl(RUNNING))
        self.assertIn("old-clone", str(code))
        self.assertEqual(self.launchd_installs, [])
        code, out, _ = self.run_tool("start", "--replace", launchctl=Launchctl(RUNNING))
        self.assertEqual((code, self.launchd_installs), (0, [False]))

    def test_start_wont_fight_a_bentobox_in_a_terminal(self):
        self.set_up()
        self.up = True  # something answers, and launchd runs nothing
        code, out, _ = self.run_tool("start")
        self.assertIn("already answers", str(code))
        self.assertEqual(self.launchd_installs, [])

    # -- stop, restart, status
    def test_stop(self):
        code, out, launchctl = self.run_tool("stop")
        self.assertEqual((code, launchctl.changes()), (0, []))
        code, out, launchctl = self.run_tool("stop", "--dry-run", launchctl=Launchctl(RUNNING))
        self.assertEqual(launchctl.changes(), [])
        code, out, launchctl = self.run_tool("stop", launchctl=Launchctl(RUNNING))
        self.assertEqual(launchctl.changes(), [["launchctl", "bootout", f"gui/{os.getuid()}/local.bentobox"]])
        self.assertIn("starts again when you log in", out)

    def test_restart(self):
        self.set_up()
        self.plist(self.paths["APP_SCRIPT"])
        self.up = True
        code, out, launchctl = self.run_tool("restart", launchctl=Launchctl(RUNNING))
        self.assertEqual(launchctl.changes(), [["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/local.bentobox"]])
        code, out, launchctl = self.run_tool("restart", "--dry-run", launchctl=Launchctl(RUNNING))
        self.assertEqual(launchctl.changes(), [])
        self.up = False
        code, out, launchctl = self.run_tool("restart")  # not loaded: it starts
        self.assertEqual((code, self.launchd_installs), (0, [False]))

    def test_status_exit_codes(self):
        code, out, _ = self.run_tool("status")
        self.assertEqual(code, self.t.NOT_RUNNING)
        self.assertIn("Service: not running", out)
        self.up = True
        code, out, _ = self.run_tool("status")  # `tools/bentobox run`, in a terminal
        self.assertEqual(code, 0)
        self.assertIn("Service: running in a terminal", out)
        self.up = False
        self.set_up()
        self.up = True
        code, out, launchctl = self.run_tool("status", launchctl=Launchctl(RUNNING))
        self.assertEqual(code, 0)
        self.assertIn("Service: running (local.bentobox, pid 4242)", out)
        self.assertIn([self.paths["VENV_PYTHON"], self.paths["APP_SCRIPT"], "status"], launchctl.calls)

    # -- check
    def test_check_on_a_new_mac_starts_with_setup(self):
        code, out, launchctl = self.run_tool("check")
        self.assertEqual(code, 1)
        self.assertIn("Next, from the repo root: python3 desktop/install.py setup", out)
        self.assertNotIn("key-status", str(launchctl.calls))  # no app to ask yet

    def test_check_says_keys_are_set_and_never_more(self):
        self.set_up()
        self.plist(self.paths["APP_SCRIPT"])
        self.up = True
        said = ("Toggl key: set\nspeech-to-text key: not set\nToggl organization: 12345\nLock code: not set\n"
                "Trusted: 9c41f07a  Bento Knob\n")
        code, out, _ = self.run_tool("check", launchctl=Launchctl(RUNNING, app_out=said))
        self.assertEqual(code, 0)  # only optional steps left
        self.assertRegex(out, r"ok +Toggl key +set")
        self.assertRegex(out, r"maybe +Speech-to-text key +not set \(optional\)")
        self.assertIn("in their own Terminal", out)
        self.assertRegex(out, r"ok +Background service +running")
        self.assertRegex(out, r"ok +Boards +1 trusted")

    def test_check_points_at_another_copy(self):
        self.set_up()
        self.plist("/Users/me/old-clone/desktop/bentobox.py")
        code, out, _ = self.run_tool("check", launchctl=Launchctl(RUNNING))
        self.assertEqual(code, 1)
        self.assertIn("tools/bentobox start --replace", out)


if __name__ == "__main__":
    unittest.main()
