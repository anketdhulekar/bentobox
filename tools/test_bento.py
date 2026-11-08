"""Tests for tools/bento. They never touch a board: esptool is replaced, and the commands that write files run on a copy."""
import contextlib
import importlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(TOOLS, "bento")
REPO = os.path.dirname(TOOLS)
sys.dont_write_bytecode = True


def load_tool():
    loader = importlib.machinery.SourceFileLoader("bento_tool", TOOL)
    spec = importlib.util.spec_from_loader("bento_tool", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


BOARDS_FILE = """# Boards
| Board | MAC |
|---|---|
| knob-lcd-1.85 | 11:11:11:11:11:11 |
| knob-lcd-1.8 | AA:BB:CC:DD:EE:0F |
amoled-2.16 aa-bb-cc-dd-ee-10
"""

READ_MAC_OUTPUT = """esptool.py v4.12.0
Connecting...
Chip is ESP32-S3 (QFN56) (revision v0.2)
MAC: %s
Uploading stub...
MAC: %s
Hard resetting via RTS pin...
"""


class BoardsFile(unittest.TestCase):
    def setUp(self):
        self.bento = load_tool()
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = os.path.join(self.dir, "boards.local.md")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(BOARDS_FILE)
        self.bento.BOARDS_LOCAL = self.path
        self.bento.expected_mac.__defaults__ = (self.path,)

    def test_finds_a_board_by_its_whole_name(self):
        self.assertEqual(self.bento.expected_mac("knob-lcd-1.8"), "aa:bb:cc:dd:ee:0f")

    def test_accepts_dashes_in_a_mac(self):
        self.assertEqual(self.bento.expected_mac("amoled-2.16"), "aa:bb:cc:dd:ee:10")

    def test_no_entry(self):
        self.assertIsNone(self.bento.expected_mac("nope"))

    def test_no_file(self):
        self.assertIsNone(self.bento.expected_mac("knob-lcd-1.8", self.path + ".missing"))

    def fake_esptool(self, mac):
        self.calls = []

        def in_idf(command, cwd=None, capture=False):
            self.calls.append(command)
            return (0, READ_MAC_OUTPUT % (mac, mac)) if capture else 0

        self.bento.in_idf = in_idf

    def test_refuses_a_different_board(self):
        self.fake_esptool("11:22:33:44:55:66")
        with self.assertRaisesRegex(self.bento.Refusal, "different board"):
            self.bento.check_connected_board("knob-lcd-1.8", "/dev/none")
        self.assertEqual(self.calls, [["esptool.py", "--port", "/dev/none", "read_mac"]])

    def test_accepts_the_right_board(self):
        self.fake_esptool("aa:bb:cc:dd:ee:0f")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.bento.check_connected_board("knob-lcd-1.8", "/dev/none")
        self.assertIn("has the MAC", out.getvalue())

    def test_no_entry_refuses_before_esptool_runs(self):
        self.fake_esptool("aa:bb:cc:dd:ee:0f")
        with self.assertRaisesRegex(self.bento.Refusal, "boards.local.md"):
            self.bento.check_connected_board("nope", "/dev/none")
        self.assertEqual(self.calls, [])

    def test_unreadable_output_refuses(self):
        self.assertIsNone(self.bento.parse_read_mac("no such thing"))

    def test_everyday_and_debug_builds_never_share_a_folder(self):
        """Without --debug the debug interface is switched off explicitly, so a folder's cached ON can't carry over."""
        everyday = self.bento.idf_py("knob-lcd-1.8", "build")
        debug = self.bento.idf_py("knob-lcd-1.8", "build", debug=True)
        self.assertIn("-DBENTO_FORCE_DEBUG=OFF", everyday)
        self.assertIn("-DBENTO_FORCE_DEBUG=ON", debug)
        folder = lambda command: command[command.index("-B") + 1]
        self.assertTrue(folder(everyday).endswith("build-knob-lcd-1.8"))
        self.assertTrue(folder(debug).endswith("build-knob-lcd-1.8-debug"))


class Commands(unittest.TestCase):
    """The real commands, on a copy of the board folders with no boards.local.md, so nothing reaches a port."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        os.makedirs(os.path.join(self.dir, "tools"))
        shutil.copy(TOOL, os.path.join(self.dir, "tools", "bento"))
        shutil.copytree(os.path.join(REPO, "firmware", "boards"), os.path.join(self.dir, "firmware", "boards"))

    def run_tool(self, *args):
        tool = os.path.join(self.dir, "tools", "bento")
        return subprocess.run([sys.executable, "-I", tool] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

    def test_new_board_fills_in_its_name(self):
        self.assertEqual(self.run_tool("new-board", "my-board-1.0").returncode, 0)
        with open(os.path.join(self.dir, "firmware", "boards", "my-board-1.0", "board_info.c"), encoding="utf-8") as f:
            info = f.read()
        self.assertIn('.name = "my-board-1.0"', info)
        self.assertNotIn("BOARD_NAME", info)

    def test_new_board_never_overwrites(self):
        self.run_tool("new-board", "my-board")
        self.assertEqual(self.run_tool("new-board", "my-board").returncode, 1)

    def test_new_board_checks_the_name(self):
        self.assertEqual(self.run_tool("new-board", "Bad Name").returncode, 1)

    def test_flash_and_backup_refuse_with_no_entry(self):
        for command in ("flash", "backup"):
            done = self.run_tool(command, "knob-lcd-1.8")
            self.assertEqual(done.returncode, 1)
            self.assertIn("boards.local.md", done.stdout)
            self.assertIn("read_mac", done.stdout)

    def test_unknown_board(self):
        done = self.run_tool("flash", "not-a-board")
        self.assertEqual(done.returncode, 1)
        self.assertIn("No board", done.stdout)

    def test_sim_passes_its_options_on(self):
        # The simulator's own options, such as --demo, must reach it: make would take them as its own.
        runner = os.path.join(self.dir, "firmware", "sim", "run.sh")
        os.makedirs(os.path.dirname(runner))
        with open(runner, "w", encoding="utf-8") as f:
            f.write('echo "sim: $*"\n')
        done = self.run_tool("sim", "knob-lcd-1.8", "--demo", "--agents", "3")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("sim: knob-lcd-1.8 --demo --agents 3", done.stdout)


class PerfReport(unittest.TestCase):
    """tools/perf_test.py's arithmetic: what it calls a median, slower, faster and over budget."""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, TOOLS)
        try:
            cls.pt = importlib.import_module("perf_test")
        finally:
            sys.path.remove(TOOLS)

    def sample(self, **kw):
        s = {"ok": True, "first_ms": 10.0, "settle_ms": 200.0, "frame_ms_max": 12.0, "work_ms": 40.0, "bands": 30, "painted": 8,
             "gap_ms_max": 22.0}
        s.update(kw)
        return s

    def test_medians_count_only_the_runs_that_did_what_they_should(self):
        row = self.pt.summarize({"a": [self.sample(first_ms=8), self.sample(first_ms=10), self.sample(first_ms=90),
                                       self.sample(ok=False, first_ms=500)]})["a"]
        self.assertEqual(row["first_ms"], 10)
        self.assertEqual((row["n"], row["failed"]), (3, 1))
        self.assertEqual(row["worst_of_runs"], 12.0)

    def test_nothing_repainted_has_no_response(self):
        row = self.pt.summarize({"a": [self.sample(first_ms=None, settle_ms=None)]})["a"]
        self.assertIsNone(row["first_ms"])
        self.assertEqual(self.pt.compare({"a": row}, {"a": {"first_ms": 5.0}}, "board"), {})

    def test_slower_needs_both_the_share_and_the_size(self):
        base = {"a": {"first_ms": 40.0, "bands": 20}}
        cmp = self.pt.compare
        self.assertEqual(cmp({"a": {"first_ms": 52.0, "bands": 20}}, base, "board")[("a", "first_ms")][2], "slower")  # +30 %, +12 ms
        self.assertEqual(cmp({"a": {"first_ms": 48.0, "bands": 20}}, base, "board"), {})  # +20 %: within the tolerance
        small = {"a": {"first_ms": 2.0, "bands": 20}}
        self.assertEqual(cmp({"a": {"first_ms": 4.0, "bands": 20}}, small, "board"), {})  # +100 %, but 2 ms is noise
        self.assertEqual(cmp({"a": {"first_ms": 28.0, "bands": 20}}, base, "board")[("a", "first_ms")][2], "faster")
        self.assertEqual(cmp({"a": {"first_ms": 40.0, "bands": 25}}, base, "board")[("a", "bands")][2], "slower")
        self.assertEqual(cmp({"a": {"first_ms": 40.0, "bands": 21}}, base, "board"), {})  # one band more is noise

    def test_an_action_the_baseline_lacks_is_not_compared(self):
        self.assertEqual(self.pt.compare({"new": {"first_ms": 99.0}}, {"old": {"first_ms": 1.0}}, "sim"), {})
        self.assertEqual(self.pt.compare({"a": {"first_ms": 99.0}}, None, "sim"), {})

    def test_budgets_apply_to_boards_only(self):
        rows = {"a": {"first_ms": 150.0, "frame_ms_max": 20.0}, "b": {"first_ms": 50.0, "frame_ms_max": 60.0}}
        self.assertEqual(set(self.pt.over_budget(rows, "board")), {("a", "first_ms"), ("b", "frame_ms_max")})
        self.assertEqual(self.pt.over_budget(rows, "sim"), {})

    def test_every_action_has_a_unique_name(self):
        keys = [a.key for a in self.pt.ACTIONS]
        self.assertEqual(len(keys), len(set(keys)))


class GeneratedAssets(unittest.TestCase):
    """The committed fonts.c and icons.c are made by the generators from their sources: each says which sources, and
    they must be the sources as they are now."""

    def committed_hash(self, name):
        import re
        with open(os.path.join(REPO, "firmware", "ux", "assets", name), encoding="utf-8") as f:
            head = f.read(600)
        found = re.search(r"^// source-hash: ([0-9a-f]+)$", head, re.MULTILINE)
        self.assertIsNotNone(found, f"{name} has no source-hash line: regenerate it")
        return found.group(1)

    def generator(self, name):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("needs Pillow: run with tools/.venv/bin/python")
        sys.path.insert(0, TOOLS)
        try:
            return importlib.import_module(name)
        finally:
            sys.path.remove(TOOLS)

    def test_the_menu_icons_match_the_preview(self):
        self.assertEqual(self.committed_hash("icons.c"), self.generator("gen_icons").source_hash(),
                         "preview/bento.html's icons changed: tools/.venv/bin/python tools/gen_icons.py")

    def test_the_fonts_match_their_table(self):
        self.assertEqual(self.committed_hash("fonts.c"), self.generator("gen_fonts").source_hash(),
                         "the font table or the font changed: tools/.venv/bin/python tools/gen_fonts.py")


if __name__ == "__main__":
    unittest.main()
