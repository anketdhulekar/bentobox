"""What arrives from outside can't crash the app or grow it without end: hook events over local HTTP, several boards'
voice clips at once, a saved file of the wrong shape, and odd numbers from Toggl. Each case here reproduces a problem
a review found."""
import http.client
import json
import os
import threading
import unittest

import helpers
from helpers import Clock, HomeCase

import config
import fields
from agents import zoo as zoo_mod
from agents.rex import Rex, applescript_string
from agents.sources import pid_alive
from agents.zoo import MAX_AGENTS, MAX_STORED, Zoo
from clock_sync import ClockHub
from link.tcp import TcpLink, host_allowed
from localhttp import make_http
from speech.clips import MAX_CLIP_S, VoiceDesk
from util import rotate_log


def ev(event, sid="s1", **kw):
    return {"event": event, "session_id": sid, **kw}


class HookEvents(unittest.TestCase):
    """Events that crashed the zoo or grew it before the events were checked."""

    def setUp(self):
        self.zoo = Zoo(Clock())

    def everything_still_works(self):
        self.zoo.public()
        self.zoo.rows()
        self.zoo.tick()
        self.zoo.count()

    def test_a_numeric_sub_agent_id(self):
        self.zoo.on_hook(ev("PreToolUse", agent_id=5, activity="Reading"))
        self.everything_still_works()

    def test_sub_agent_ids_of_two_types(self):
        self.zoo.on_hook(ev("SubagentStart", agent_id=5, agent_type="Plan"))
        self.zoo.on_hook(ev("SubagentStart", agent_id="a", agent_type="Explore"))
        self.everything_still_works()
        self.assertEqual(list(self.zoo.agents["s1"].subs), ["a"])  # only the one that can be an id

    def test_a_dict_or_a_list_for_an_id(self):
        self.zoo.on_hook(ev("PreToolUse", agent_id={"x": 1}))
        self.zoo.on_hook({"event": "PreToolUse", "session_id": ["s"]})
        self.zoo.on_hook({"event": "PreToolUse", "session_id": {"s": 1}})
        self.zoo.on_hook("not even a dict")
        self.everything_still_works()

    def test_a_pid_too_big_for_the_system(self):
        self.zoo.on_hook(ev("SessionStart", sid="cx", agent="codex", project="shop", pid=10 ** 20, source="startup"))
        self.everything_still_works()
        self.assertEqual(self.zoo.agents["cx"].pid, 0)

    def test_pid_alive_never_raises(self):
        for pid in (10 ** 20, -1, 0, "123", None, True, 2 ** 31, 3.5):
            self.assertFalse(pid_alive(pid), pid)
        self.assertTrue(pid_alive(os.getpid()))

    def test_a_flood_of_made_up_sessions_is_capped(self):
        for i in range(5000):
            self.zoo.on_hook(ev("UserPromptSubmit", sid=f"flood-{i}"))
        self.assertLessEqual(len(self.zoo.agents), MAX_STORED)
        self.assertEqual(self.zoo.count(), MAX_AGENTS)
        self.assertIn("flood-4999", self.zoo.agents)  # the newest stay
        self.everything_still_works()

    def test_finished_agents_make_room_before_busy_ones(self):
        clock = Clock()
        zoo = Zoo(clock)
        zoo.on_hook(ev("UserPromptSubmit", sid="busy"))  # the oldest, but working
        clock.advance(1)
        zoo.on_hook(ev("Stop", sid="busy-done"))
        zoo.on_hook(ev("UserPromptSubmit", sid="busy-done"))
        zoo.on_hook(ev("Stop", sid="busy-done"))
        clock.advance(1)
        for i in range(MAX_STORED - 2):  # full
            zoo.on_hook(ev("UserPromptSubmit", sid=f"n{i}"))
        zoo.on_hook(ev("UserPromptSubmit", sid="one more"))
        self.assertEqual(len(zoo.agents), MAX_STORED)
        self.assertIn("busy", zoo.agents)  # the oldest, but busy: it stays
        self.assertNotIn("busy-done", zoo.agents)

    def test_text_is_cut_and_ids_that_arent_rex_ids_are_dropped(self):
        self.zoo.on_hook(ev("PreToolUse", activity="x" * 10000, project="p" * 500, rex_block="-C", rex_session="session:1"))
        a = self.zoo.agents["s1"]
        self.assertLessEqual(len(a.activity), zoo_mod.TEXT_MAX["activity"])
        self.assertLessEqual(len(a.project), zoo_mod.TEXT_MAX["project"])
        self.assertEqual(a.rex_block, "")
        self.zoo.on_hook(ev("PreToolUse", rex_block="block:7; rm -rf", rex_session="session:1"))
        self.assertEqual(self.zoo.agents["s1"].rex_block, "")
        self.zoo.on_hook(ev("PreToolUse", rex_block="block:7", rex_session="--json"))
        self.assertEqual((self.zoo.agents["s1"].rex_block, self.zoo.agents["s1"].rex_session), ("block:7", ""))

    def test_a_session_id_too_long_or_empty_is_ignored(self):
        self.zoo.on_hook(ev("UserPromptSubmit", sid="x" * 65))
        self.zoo.on_hook(ev("UserPromptSubmit", sid=""))
        self.assertEqual(self.zoo.agents, {})


class HookFloodOverHttp(unittest.TestCase):
    def test_many_posts_over_local_http(self):
        zoo = Zoo()
        server = make_http(zoo, 0, lambda i, s: True, lambda a: "")
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            for i in range(300):
                c.request("POST", "/hook", body=json.dumps(ev("UserPromptSubmit", sid=f"h{i}", agent_id=i)),
                          headers={"Content-Type": "application/json"})
                self.assertEqual(c.getresponse().read(), b"")
            c.request("GET", "/state")
            r = c.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(len(json.loads(r.read())["agents"]), 300)
        finally:
            server.shutdown()
            server.server_close()


class TwoBoardsTalking(unittest.TestCase):
    """Each board numbers its own clips: the same vid from two boards is two clips, each answered to its own board."""

    class Board:
        def __init__(self, name):
            self.name = name

    def setUp(self):
        self.sent = []
        self.desk = VoiceDesk(lambda msg, session: self.sent.append((session.name, msg)),
                              lambda wav, keywords: "Acme design review", lambda: True, lambda: [],
                              spawn=lambda fn: fn())
        self.knob, self.square = self.Board("knob"), self.Board("square")

    def clip(self, board, vid=3, end=True):
        pcm = helpers.speech_pcm(1.0)
        self.desk.handle({"t": "voice_start", "vid": vid, "rate": 16000}, board)
        packets = helpers.audio_messages(pcm, vid=vid)
        return packets

    def test_colliding_vids_from_two_boards(self):
        knob_packets = self.clip(self.knob)
        square_packets = self.clip(self.square)
        for a, b in zip(knob_packets, square_packets):  # interleaved, as two links deliver them
            self.desk.handle(a, self.knob)
            self.desk.handle(b, self.square)
        self.desk.handle({"t": "voice_end", "vid": 3, "packets": len(square_packets)}, self.square)
        self.desk.handle({"t": "voice_end", "vid": 3, "packets": len(knob_packets)}, self.knob)
        answers = {(name, msg.get("text") is not None) for name, msg in self.sent}
        self.assertEqual(answers, {("knob", True), ("square", True)})

    def test_a_cancel_on_one_board_leaves_the_others_answer(self):
        square_packets = self.clip(self.square)
        self.clip(self.knob)
        self.desk.handle({"t": "voice_cancel", "vid": 3}, self.knob)
        for p in square_packets:
            self.desk.handle(p, self.square)
        self.desk.handle({"t": "voice_end", "vid": 3, "packets": len(square_packets)}, self.square)
        self.assertEqual([(n, "text" in m) for n, m in self.sent], [("square", True)])

    def test_one_board_leaving_drops_only_its_clip(self):
        self.clip(self.knob)
        self.clip(self.square)
        self.desk.disconnected(self.knob)
        self.assertEqual(set(self.desk.clips), {self.square})

    def test_a_clip_past_two_minutes_fails(self):
        self.desk.handle({"t": "voice_start", "vid": 9, "rate": 8000}, self.knob)
        n = 8000 * 10
        data = "A" * (n // 2 * 4 // 3)
        seq = 0
        while seq * n <= 8000 * MAX_CLIP_S:
            self.desk.handle({"t": "audio", "vid": 9, "seq": seq, "p": 0, "i": 0, "n": n, "b64": data}, self.knob)
            seq += 1
        self.desk.handle({"t": "voice_end", "vid": 9, "packets": seq}, self.knob)
        self.assertEqual(self.sent, [("knob", {"t": "transcript", "vid": 9, "error": "failed"})])

    def test_cancelled_forgets_its_oldest_first(self):
        for vid in range(40):
            self.desk.handle({"t": "voice_cancel", "vid": vid}, self.knob)
        self.assertEqual(self.desk.cancelled[-1], (self.knob, 39))
        self.assertNotIn((self.knob, 0), self.desk.cancelled)


class SavedClocks(HomeCase):
    def write(self, data):
        path = config.state_path("clock.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_a_file_of_the_wrong_shape_is_no_latest(self):
        for latest in ({}, {"from": "9c41f07a"}, {"from": "nope", "at": 1}, [], "x"):
            self.write({"version": 1, "rev": 3, "latest": latest})
            hub = ClockHub(config.state_path("clock.json"), clock=Clock())
            if latest == {"from": "9c41f07a"}:  # a usable id and nothing else: clocks at rest
                self.assertEqual(hub.message()["timer"]["st"], "idle")
            else:
                self.assertIsNone(hub.message(), latest)

    def test_saved_fields_cant_replace_the_messages_own(self):
        self.write({"version": 1, "rev": 3, "latest": {"from": "9c41f07a", "t": "lock_code", "rev": 99, "now": 1, "clear": True}})
        msg = ClockHub(config.state_path("clock.json"), clock=Clock()).message()
        self.assertEqual((msg["t"], msg["rev"]), ("clock", 3))
        self.assertNotIn("clear", msg)
        self.assertNotEqual(msg["now"], 1)


class SmallerPieces(HomeCase):
    def test_numbers_from_outside(self):
        self.assertEqual(fields.number(float("nan")), 0)
        self.assertEqual(fields.number(float("inf")), 0)
        self.assertEqual(fields.number(True), 0)
        self.assertEqual(fields.number(12.7), 12)
        self.assertEqual(fields.clamped(10 ** 20, 0, 5), 0)
        self.assertEqual(fields.clamped(9, 0, 5), 5)
        self.assertEqual(fields.color("4fb3f6"), "#4FB3F6")
        self.assertEqual(fields.color("red"), fields.DEFAULT_COLOR)

    def test_applescript_strings_are_escaped(self):
        self.assertEqual(applescript_string('Re"x\\'), 'Re\\"x\\\\')

    def test_rex_never_gets_an_option_for_an_id(self):
        calls = []
        rex = Rex("/bin/false")
        rex._run = lambda *args, **kw: calls.append(args) or "{}"
        with self.assertRaises(RuntimeError):
            rex.focus("-C", "block:1")
        with self.assertRaises(RuntimeError):
            rex.focus("session:1", "--json")
        self.assertEqual(calls, [])
        rex.pane_names({"-s", "session:2"})
        self.assertEqual([a[3] for a in calls if a[:2] == ("block", "ls")], ["session:2"])  # "-s" never listed

    def test_tcp_stays_on_this_mac(self):
        self.assertTrue(host_allowed({"display_host": "127.0.0.1"}))
        self.assertTrue(host_allowed({"display_host": "localhost"}))
        self.assertFalse(host_allowed({"display_host": "192.168.1.20"}))
        self.assertTrue(host_allowed({"display_host": "192.168.1.20", "allow_remote_display": True}))
        link = TcpLink({"display_host": "10.0.0.5", "display_port": 7777}, None)
        stop = threading.Event()
        link.run(stop)  # returns at once, connecting to nothing
        self.assertFalse(link.connected)

    def test_the_log_rotates_only_when_its_stdout_and_too_big(self):
        path = os.path.join(self.home, "bento.log")
        with open(path, "w") as f:
            f.write("x" * 100)
        with open(path, "a") as stream:
            self.assertFalse(rotate_log(path, limit=1000, stream=stream))
            self.assertTrue(rotate_log(path, limit=50, stream=stream))
            stream.write("new\n")
        with open(path) as f:
            self.assertEqual(f.read(), "new\n")
        with open(path + ".1") as f:
            self.assertEqual(len(f.read()), 100)
        other = os.path.join(self.home, "other.log")
        with open(other, "w") as stream:
            self.assertFalse(rotate_log(path, limit=1, stream=stream))  # not the file stdout writes to


if __name__ == "__main__":
    unittest.main()
