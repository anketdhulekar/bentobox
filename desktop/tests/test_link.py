"""The links: the whole app over TCP against the fake board, and the Bluetooth link with a fake bleak. Nothing here scans
or touches a real radio."""
import asyncio
import importlib.util
import json
import sys
import threading
import time
import types
import unittest
from unittest import mock

import helpers
from helpers import KEY, SPEECH_KEY, T0, FakeSpeech, FakeToggl, HomeCase, audio_messages, make_backend, speech_pcm

from agents.zoo import short_id
from fake_board import FakeBoard, summary
from link.base import Link
from link import framing
from link.ble import BleLink, BoardLink, is_board
from link.framing import LINE_MAX, RX_UUID, SERVICE_UUID, TX_UUID
from link.tcp import TcpLink


class Tcp(HomeCase):
    """The real TcpLink, Backend threads and fake servers, with a real clock."""

    def setUp(self):
        super().setUp()
        self.toggl, self.speech = FakeToggl(), FakeSpeech()
        self.addCleanup(self.toggl.close)
        self.addCleanup(self.speech.close)
        self.toggl.projects = [{"id": 123, "name": "Acme", "color": "#4FB3F6"}]
        self.board = FakeBoard()
        self.addCleanup(self.board.stop)

    def run_app(self, **kw):
        backend = make_backend(self.toggl, self.speech, clock=time.time, link="tcp", display_port=self.board.port, **kw)
        backend.voice.spawn = lambda fn: threading.Thread(target=fn, daemon=True).start()
        link = TcpLink(backend.cfg, backend)
        link.reconnect_min_s = 0.05
        link.reconnect_max_s = 0.1
        link.busy_wait_s = 0.2
        backend.start()
        stop = threading.Event()
        thread = threading.Thread(target=link.run, args=(stop,), daemon=True)
        thread.start()

        def shut():
            stop.set()
            thread.join(timeout=3)
            backend.stop()
        self.addCleanup(shut)
        return backend, link, shut

    def test_hello_state_board_and_the_first_lists(self):
        self.run_app()
        hello = self.board.wait_for("hello")
        self.assertEqual((hello["v"], bool(hello["host"])), (framing.VERSION, True))
        self.assertEqual(self.board.messages[0]["t"], "hello")
        state = self.board.wait_for("state")
        self.assertEqual((state["toggl"], state["voice"], state["n"]), ("ok", True, 0))
        self.assertLess(abs(state["now"] - time.time()), 5)
        self.assertEqual(state["utc"], time.localtime().tm_gmtoff)
        self.assertEqual(self.board.wait_for("projects")["rows"][0]["name"], "Acme")
        self.board.wait_for("tasks")

    def test_a_run_from_start_to_stop(self):
        backend, _, _ = self.run_app()
        self.board.wait_for("tasks")
        now = int(time.time())
        self.board.send({"t": "toggl", "id": 17, "op": "start", "rid": 812, "at": now - 100, "desc": "Write", "task_id": 0, "project": 123})
        self.board.wait_for("ack", lambda m: m["id"] == 17)
        deadline = time.monotonic() + 5
        while not self.toggl.running and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.toggl.running)
        self.board.send({"t": "toggl", "id": 18, "op": "update", "rid": 812, "desc": "Write more", "task_id": 0, "project": 123})
        self.board.send({"t": "toggl", "id": 19, "op": "shift", "rid": 812, "start": now - 160})
        self.board.wait_for("ack", lambda m: m["id"] == 19)
        time.sleep(0.5)
        self.assertEqual(self.toggl.reqs("PATCH"), [])  # settling
        deadline = time.monotonic() + 5
        while not self.toggl.reqs("PATCH") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.toggl.reqs("PATCH")[0].body, {"description": "Write more", "start": helpers.iso(now - 160)})
        self.board.send({"t": "toggl", "id": 20, "op": "stop", "rid": 812, "at": now})
        deadline = time.monotonic() + 5
        while self.toggl.running and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.toggl.running, 0)
        self.assertEqual(len(self.toggl.reqs("PATCH")), 1)
        self.assertEqual(backend.runs.queued(), 0)

    def test_sync_fetch_and_voice_answers_reach_the_board(self):
        self.run_app()
        self.board.wait_for("tasks")
        self.board.send({"t": "sync", "req": 7, "rid": 0})
        self.assertEqual(self.board.wait_for("sync_result")["result"], "in_sync")
        self.board.send({"t": "fetch", "req": 5, "what": "projects"})
        self.assertEqual(self.board.wait_for("projects", lambda m: m["req"] == 5)["pages"], 1)
        pcm = speech_pcm(1.0)
        packets = audio_messages(pcm)
        self.board.send({"t": "voice_start", "vid": 3, "rate": 16000})
        for p in packets:
            self.board.send(p)
        self.board.send({"t": "voice_end", "vid": 3, "packets": len(packets)})
        answer = self.board.wait_for("transcript", timeout=10)
        self.assertEqual((answer["text"], answer["project"]), ("Acme design review", 123))

    def test_state_follows_a_new_agent_at_once_and_every_10_s_at_least(self):
        backend, link, _ = self.run_app()
        self.board.wait_for("state")
        before = self.board.of_kind("state")[-1]["seq"]
        backend.zoo.on_hook({"event": "PreToolUse", "session_id": "s", "project": "p", "activity": "Reading a.c"})
        state = self.board.wait_for("state", lambda m: m["n"] == 1)
        self.assertGreater(state["seq"], before)
        self.board.wait_until(lambda: [a.get("act") for a in self.board.agents.values()] == ["Reading a.c"])
        self.board.clear()
        link._last_state -= 11  # as if 11 s had passed with no change
        self.assertEqual(self.board.wait_for("state")["seq"], state["seq"])

    def test_the_board_ends_up_with_the_macs_list_through_changes_a_resync_and_a_reconnect(self):
        backend, link, _ = self.run_app()
        zoo = backend.zoo
        for i in range(30):
            zoo.on_hook({"event": "PreToolUse", "session_id": f"s{i}", "project": "プ" * 40, "activity": f"Step {i}"})
        def matches():
            return [(r["id"], r.get("act")) for r in self.board.agents.values()] == \
                   [(a["id"], a["act"]) for a in zoo.public()["agents"]]
        self.board.wait_until(matches)
        zoo.on_hook({"event": "PreToolUse", "session_id": "s3", "activity": "Changed"})
        zoo.on_hook({"event": "SessionEnd", "session_id": "s4"})
        zoo.on_hook({"event": "PermissionRequest", "session_id": "s5", "activity": "Needs permission: Bash"})
        self.board.wait_until(matches)
        self.assertEqual(len(self.board.agents), 29)
        self.board.clear()
        self.board.agents.clear()  # a board that lost track
        self.board.send({"t": "resync"})
        self.assertEqual(self.board.wait_for("agents")["page"], 0)
        self.board.wait_until(matches)
        self.board.clear()
        self.board.drop()
        self.board.wait_for("hello")
        self.assertEqual(self.board.wait_for("agents")["page"], 0)
        self.board.wait_until(matches)
        self.assertEqual(self.board.of_kind("state")[-1]["n"], 29)

    def test_a_long_list_keeps_its_pace_and_holds_nothing_else_up(self):
        backend, _, _ = self.run_app()
        self.board.wait_for("tasks")
        for i in range(60):
            backend.zoo.on_hook({"event": "PreToolUse", "session_id": f"s{i}", "project": "プ" * 40, "activity": "編" * 48})
        self.board.clear()
        started = time.monotonic()
        self.board.send({"t": "resync"})
        pages = self.board.wait_for("agents", lambda m: m.get("page") == 0)["pages"]
        self.assertGreater(pages, 1)
        self.board.send({"t": "toggl", "id": 40, "op": "start", "rid": 1, "at": int(time.time()), "desc": "x", "task_id": 0, "project": 0})
        self.board.wait_for("ack", lambda m: m["id"] == 40)
        self.assertLess(len([m for m in self.board.of_kind("agents") if "page" in m]), pages)  # the ack didn't wait for the list
        backend.zoo.on_hook({"event": "PermissionRequest", "session_id": "s59", "activity": "Needs permission: Bash"})
        self.board.wait_for("agents", lambda m: "page" not in m and [r for r in m["rows"] if r["st"] == "needs"])
        self.assertLess(len([m for m in self.board.of_kind("agents") if "page" in m]), pages)  # nor did the agent that needs you
        self.board.wait_for("agents", lambda m: m.get("page") == pages - 1, timeout=30)
        self.assertEqual(self.board.agents[short_id("s59")]["st"], "needs")
        lines = [m for m in self.board.of_kind("agents") if m.get("pages") == pages]
        self.assertEqual([m["page"] for m in lines], list(range(pages)))
        self.assertLessEqual(max(m["_len"] for m in lines), framing.AGENTS_LINE_MAX)
        # The first line goes at once, and every other one waits for those before it.
        self.assertGreaterEqual(time.monotonic() - started, sum(m["_len"] for m in lines[:-1]) / framing.AGENTS_BYTES_PER_S * 0.9)

    def test_reconnecting_after_a_drop_keeps_what_was_owed(self):
        backend, link, _ = self.run_app()
        self.board.wait_for("tasks")
        first = link.session
        self.board.clear()
        self.board.drop()
        self.board.wait_for("hello", timeout=5)
        self.board.wait_for("state")
        self.assertGreater(link.session, first)
        self.assertEqual(self.board.of_kind("projects"), [])  # no second list load
        self.board.send({"t": "toggl", "id": 30, "op": "start", "rid": 1, "at": T0, "desc": "x", "task_id": 0, "project": 0})
        self.board.wait_for("ack", lambda m: m["id"] == 30)

    def test_the_fake_board_answers_as_the_protocol_says(self):
        mark = helpers.LOGS.tell()
        backend, _, _ = self.run_app()
        self.board.wait_for("tasks")
        helpers.LOGS.seek(mark)
        self.assertNotIn("it speaks protocol", helpers.LOGS.read())
        helpers.LOGS.seek(0, 2)
        self.assertEqual(framing.parse_uid(self.board.uid), self.board.uid)
        self.assertEqual(backend.board_uid, self.board.uid)
        other = FakeBoard()
        self.addCleanup(other.stop)
        self.assertNotEqual(other.uid, self.board.uid)  # each makes up its own

    def wait_running(self, desc):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            entry = self.toggl.entries.get(self.toggl.running)
            if entry and entry["description"] == desc:
                return entry
            time.sleep(0.01)
        self.fail(f"no timer named {desc}")

    def test_a_reflashed_board_over_the_real_link(self):
        """The same board, on the same port, with a new uid and its numbers starting again."""
        self.run_app()
        self.board.wait_for("tasks")
        self.board.send({"t": "toggl", "id": 1, "op": "start", "rid": 1, "at": int(time.time()) - 500, "desc": "Before", "task_id": 0, "project": 0})
        self.board.wait_for("ack", lambda m: m["id"] == 1)
        self.wait_running("Before")
        self.board.clear()
        self.board.uid = "abcdef01"
        self.board.drop()
        self.board.wait_for("hello")
        self.assertEqual(self.board.wait_for("projects")["req"], 0)  # a new uid gets the first list load too
        self.board.send({"t": "toggl", "id": 1, "op": "start", "rid": 1, "at": int(time.time()) - 100, "desc": "After", "task_id": 0, "project": 0})
        self.board.wait_for("ack", lambda m: m["id"] == 1)
        self.wait_running("After")

    def test_two_boards_of_the_same_model_over_the_real_link(self):
        """Both are "knob-lcd-1.8", on the same port one after the other. Both use the same numbers."""
        self.run_app()
        self.board.wait_for("tasks")
        first_uid = self.board.uid
        self.board.send({"t": "toggl", "id": 7, "op": "start", "rid": 3, "at": int(time.time()) - 500, "desc": "One", "task_id": 0, "project": 0})
        self.board.wait_for("ack", lambda m: m["id"] == 7)
        self.wait_running("One")
        self.board.clear()
        self.board.uid = "abcdef01"
        self.board.drop()
        self.board.wait_for("tasks")
        self.board.send({"t": "toggl", "id": 7, "op": "start", "rid": 3, "at": int(time.time()) - 100, "desc": "Two", "task_id": 0, "project": 0})
        self.board.wait_for("ack", lambda m: m["id"] == 7)
        self.wait_running("Two")
        self.board.clear()
        self.board.uid = first_uid
        self.board.drop()
        self.board.wait_for("state")
        self.board.send({"t": "toggl", "id": 8, "op": "stop", "rid": 3, "at": int(time.time())})  # the first board's stop
        self.board.wait_for("ack", lambda m: m["id"] == 8)
        time.sleep(0.2)
        self.assertEqual(self.toggl.entries[self.toggl.running]["description"], "Two")
        self.assertEqual(self.board.of_kind("projects"), [])  # the first board already had its lists

    def test_an_old_build_with_no_uid_over_the_real_link(self):
        self.board.old_build = True
        backend, link, _ = self.run_app()
        self.board.wait_for("tasks")
        self.assertEqual(backend.board_uid, "unknown")
        self.assertTrue(link.connected)
        self.board.send({"t": "toggl", "id": 1, "op": "start", "rid": 1, "at": int(time.time()), "desc": "Old", "task_id": 0, "project": 0})
        self.board.wait_for("ack", lambda m: m["id"] == 1)

    def test_busy_means_wait_and_try_again(self):
        self.board.busy = "another Mac is chosen"
        _, link, _ = self.run_app()
        self.board.wait_for("hello")
        started = time.monotonic()
        deadline = started + 5
        while self.board.hellos < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreaterEqual(self.board.hellos, 2)
        self.assertGreaterEqual(time.monotonic() - started, link.busy_wait_s * 0.8)
        self.assertEqual(self.board.of_kind("ack"), [])

    def test_no_board_yet_is_fine(self):
        port = self.board.port
        self.board.stop()
        self.board = None
        self.addCleanup(lambda: None)
        backend = make_backend(self.toggl, self.speech, link="tcp", display_port=port)
        link = TcpLink(backend.cfg, backend)
        link.reconnect_min_s = 0.01
        stop = threading.Event()
        thread = threading.Thread(target=link.run, args=(stop,), daemon=True)
        thread.start()
        time.sleep(0.2)
        self.assertFalse(link.connected)
        self.assertFalse(link.send({"t": "ack", "id": 1}))
        stop.set()
        thread.join(timeout=3)
        self.board = FakeBoard()  # for the cleanup

    def test_a_lock_code_set_while_running_arrives(self):
        backend, _, _ = self.run_app()
        self.board.wait_for("tasks")
        backend.lock_store.set_code(helpers.LOCK_CODE)
        backend.reload()
        msg = self.board.wait_for("lock_code")
        self.assertEqual(msg["iter"], 2000)
        deadline = time.monotonic() + 3
        while backend.lock_store.is_pending() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(backend.lock_store.is_pending())

    def test_a_toggl_key_set_while_running_shows_in_state(self):
        store = helpers.secrets_store.MemoryStore()
        backend, _, _ = self.run_app(store=store)
        self.assertEqual(self.board.wait_for("state")["toggl"], "no_key")
        store.set("toggl", KEY)
        store.set("speech", SPEECH_KEY)
        backend.reload()
        state = self.board.wait_for("state", lambda m: m["toggl"] == "ok")
        self.assertTrue(state["voice"])

    def test_bad_lines_from_the_board_do_no_harm(self):
        backend, link, _ = self.run_app()
        self.board.wait_for("hello")
        self.board.wait_for("state")
        self.board.send_raw(b"not json\n[1,2]\n\xff\xfe\n" + b"[" * 15000 + b"\n" + b"x" * (LINE_MAX + 100) + b"\n")
        self.board.send({"t": "fetch", "req": 77, "what": "cats"})
        self.assertEqual(self.board.wait_for("fetch_failed")["req"], 77)
        self.assertTrue(link.connected)


class Summary(unittest.TestCase):
    def test_the_fake_board_never_shows_a_lock_hash(self):
        self.assertNotIn("abcdef", summary({"t": "lock_code", "salt": "00", "iter": 2000, "hash": "abcdef"}))

    def test_it_says_what_state_and_agents_carry(self):
        self.assertIn("240 agents", summary({"t": "state", "toggl": "ok", "voice": True, "n": 240}))
        self.assertIn("page 2 of 12, 1 rows", summary({"t": "agents", "page": 1, "pages": 12, "rows": [{"id": "a1b2c3d4"}]}))
        changes = summary({"t": "agents", "rows": [{"id": "a1b2c3d4", "name": "lynx"}], "gone": ["77aa01ff"]})
        self.assertIn("lynx", changes)
        self.assertIn("77aa01ff", changes)


class FakeBoardsList(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard()
        self.addCleanup(self.board.stop)

    def take(self, **msg):
        self.board._take_agents(dict(msg, t="agents"))
        return list(self.board.agents)

    def test_it_does_with_agents_what_a_board_does(self):
        self.assertEqual(self.take(page=0, pages=2, rows=[{"id": "a"}, {"id": "b"}]), ["a", "b"])
        self.assertEqual(self.take(page=1, pages=2, rows=[{"id": "c"}]), ["a", "b", "c"])
        self.assertEqual(self.take(rows=[{"id": "b", "st": "needs"}, {"id": "d"}]), ["a", "b", "c", "d"])  # in place, and at the end
        self.assertEqual(self.board.agents["b"], {"id": "b", "st": "needs"})
        self.assertEqual(self.take(rows=[], gone=["a"]), ["b", "c", "d"])
        self.assertEqual(self.take(page=0, pages=1, rows=[{"id": "d"}, {"id": "b"}]), ["b", "d"])  # only its agents, where they were

    def test_a_row_with_no_page_during_a_list_counts_as_named_by_it(self):
        self.take(page=0, pages=1, rows=[{"id": "old"}])
        self.take(page=0, pages=2, rows=[{"id": "a"}])
        self.take(rows=[{"id": "n", "st": "needs"}])
        self.assertEqual(self.take(page=1, pages=2, rows=[{"id": "b"}]), ["a", "n", "b"])  # `old` is gone, and `n` isn't

    def test_after_a_list_a_row_is_only_a_row(self):
        self.take(page=0, pages=1, rows=[{"id": "a"}])
        self.take(rows=[{"id": "b"}])
        self.assertEqual(self.take(page=0, pages=1, rows=[{"id": "a"}]), ["a"])

    def test_a_new_row_on_a_full_board_pushes_out_those_a_list_hasnt_named(self):
        ids = [f"{i:08x}" for i in range(512)]
        self.take(page=0, pages=1, rows=[{"id": i} for i in ids])
        self.take(page=0, pages=2, rows=[{"id": ids[0]}])
        self.assertEqual(self.take(rows=[{"id": "n"}]), [ids[0], "n"])

    def test_it_keeps_the_first_512(self):
        ids = [f"{i:08x}" for i in range(600)]
        self.assertEqual(self.take(page=0, pages=1, rows=[{"id": i} for i in ids]), ids[:512])


class Handle(unittest.TestCase):
    def test_an_exception_while_handling_is_logged_without_the_message(self):
        class Bad:
            def on_message(self, msg, link=None):
                raise RuntimeError(f"boom {msg}")
        link = Link({}, Bad())
        mark = helpers.LOGS.tell()
        link.handle(b'{"t":"toggl","secretfield":"PRIVATE-TEXT"}')
        helpers.LOGS.seek(mark)
        text = helpers.LOGS.read()
        helpers.LOGS.seek(0, 2)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("PRIVATE-TEXT", text)

    def test_nothing_is_queued_with_no_board(self):
        link = Link({}, None)
        self.assertFalse(link.send({"t": "ack", "id": 1}))

    def test_an_over_long_message_isnt_written(self):
        self.assertIsNone(Link({}, None)._line({"t": "x", "pad": "y" * LINE_MAX}))

    def test_a_clock_counts_from_when_its_written(self):
        class Backend:
            @staticmethod
            def now_ms():
                return 1793826000999
        line = Link({}, Backend())._line({"t": "clock", "rev": 1, "now": 5, "at": 4})
        self.assertEqual(json.loads(line)["now"], 1793826000999)
        self.assertEqual(json.loads(Link({}, Backend())._line({"t": "ack", "id": 1, "now": 5}))["now"], 5)  # only a clock


class FakeChar:
    max_write_without_response_size = 100


class FakeClient:
    """Enough of bleak's client for the link's session."""

    def __init__(self, fail_first=0):
        self.writes = []
        self.is_connected = True
        self.notify = None
        self.fail_first = fail_first
        self.services = types.SimpleNamespace(get_characteristic=lambda uuid: FakeChar() if uuid == RX_UUID else None)

    async def start_notify(self, uuid, callback):
        self.assert_tx = uuid
        self.notify = callback

    async def write_gatt_char(self, char, data, response=None):
        if self.fail_first:
            self.fail_first -= 1
            raise OSError("not paired yet")
        self.writes.append((bytes(data), response))

    def lines(self):
        """(message, whether each of its chunks had a response)."""
        out, buf, flags = [], b"", []
        for data, response in self.writes:
            buf += data
            flags.append(response)
            if buf.endswith(b"\n"):
                out.append((json.loads(buf), set(flags)))
                buf, flags = b"", []
        return out


class Ble(HomeCase):
    def setUp(self):
        super().setUp()
        self.backend = make_backend(clock=time.time)
        self.backend.trust.trust(helpers.UID)  # paired, and on this Mac's list
        self.backend.trust.trust("9c41f07a")
        self.link = BoardLink(self.backend.cfg, self.backend, "the-address")
        self.stop = threading.Event()

    def run_session(self, client, script):
        async def scenario():
            task = asyncio.create_task(self.link._session(client, self.stop))
            await script()
            self.stop.set()
            await asyncio.wait_for(task, 3)
        asyncio.run(asyncio.wait_for(scenario(), 10))

    def test_the_filter_is_the_service_uuid(self):
        self.assertTrue(is_board([SERVICE_UUID]))
        self.assertTrue(is_board([SERVICE_UUID.upper()]))
        self.assertFalse(is_board(["7a6f6f6b-6565-7065-7200-000000000000"]))  # the old Agent Zoo display
        self.assertFalse(is_board([]))
        self.assertFalse(is_board(None))

    def test_a_session(self):
        client = FakeClient()

        async def script():
            while not self.link.connected:
                await asyncio.sleep(0.01)
            line = json.dumps({"t": "board", "v": 4, "uid": helpers.UID, "name": "knob", "fw": "1"}) + "\n"
            for part in (line[:10], line[10:]):  # a notification can hold part of a line
                client.notify(None, bytearray(part.encode()))
            client.notify(None, bytearray(b'{"t":"toggl","id":17,"op":"start","rid":1,"at":1793826000,"desc":"x","task_id":0,"project":0}\n'))
            self.assertTrue(self.link.send({"t": "lock_code", "clear": True}, reliable=True))
            await asyncio.sleep(0.2)

        self.run_session(client, script)
        self.assertEqual(client.assert_tx, TX_UUID)
        lines = client.lines()
        hello, response = lines[0]
        self.assertEqual((hello["t"], hello["v"], response), ("hello", framing.VERSION, {True}))  # with response: it triggers pairing
        self.assertEqual(lines[1][0]["t"], "state")
        kinds = [m["t"] for m, _ in lines]
        self.assertIn("ack", kinds)
        self.assertIsNone(self.backend.board_uid)  # the session ended
        for msg, flags in lines:
            if msg["t"] == "lock_code":
                self.assertEqual(flags, {True})
            if msg["t"] in ("ack", "state"):
                self.assertEqual(flags, {False})
        self.assertTrue(all(len(d) <= FakeChar.max_write_without_response_size for d, _ in client.writes))

    def test_an_answer_during_the_hello_write_isnt_lost(self):
        """The board's answer can arrive before the write that carried the hello has returned."""
        client = FakeClient()
        self.backend.lock_store.set_code(helpers.LOCK_CODE)
        real_write = client.write_gatt_char

        async def write(char, data, response=None):
            await real_write(char, data, response)
            if b'"hello"' in bytes(data) and not client.answered:
                client.answered = True
                client.notify(None, bytearray(b'{"t":"board","v":4,"uid":"9c41f07a","name":"knob","fw":"1"}\n'))
                client.notify(None, bytearray(b'{"t":"toggl","id":17,"op":"start","rid":1,"at":1793826000,"desc":"x","task_id":0,"project":0}\n'))
        client.answered = False
        client.write_gatt_char = write

        async def script():
            while not self.link.connected:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.2)
        self.run_session(client, script)
        kinds = [m["t"] for m, _ in client.lines()]
        self.assertIn("lock_code", kinds)
        self.assertIn("ack", kinds)
        self.assertFalse(self.backend.lock_store.is_pending())

    def test_a_long_message_is_split_and_comes_back_whole(self):
        client = FakeClient()
        rows = [{"id": i, "name": "プ" * 100, "color": "#112233"} for i in range(20)]

        async def script():
            while not self.link.connected:
                await asyncio.sleep(0.01)
            self.link.send({"t": "projects", "req": 5, "page": 0, "pages": 1, "rows": rows})
            await asyncio.sleep(0.2)
        self.run_session(client, script)
        got = next(m for m, _ in client.lines() if m["t"] == "projects")
        self.assertEqual(got["rows"], rows)

    def test_agents_lines_go_split_and_without_a_response(self):
        client = FakeClient()
        for i in range(60):
            self.backend.zoo.on_hook({"event": "PreToolUse", "session_id": f"s{i}", "project": "プ" * 40, "activity": "編" * 48})

        async def script():
            while not self.link.connected:
                await asyncio.sleep(0.01)
            client.notify(None, bytearray(b'{"t":"board","v":4,"uid":"9c41f07a","name":"knob","fw":"1"}\n'))
            await asyncio.sleep(0.2)
        self.run_session(client, script)
        pages = [(m, flags) for m, flags in client.lines() if m["t"] == "agents"]
        self.assertEqual(pages[0][0]["page"], 0)
        self.assertEqual(pages[0][1], {False})
        self.assertTrue(pages[0][0]["rows"])
        self.assertTrue(all(len(d) <= FakeChar.max_write_without_response_size for d, _ in client.writes))

    def test_a_message_goes_after_pairing_retries(self):
        client = FakeClient(fail_first=1)
        with mock.patch("link.ble.RECONNECT_MIN_S", 0.01):
            async def script():
                while not self.link.connected:
                    await asyncio.sleep(0.01)
            self.run_session(client, script)
        self.assertEqual(client.lines()[0][0]["t"], "hello")

    def test_a_link_that_drops_ends_the_session(self):
        client = FakeClient()

        async def scenario():
            task = asyncio.create_task(self.link._session(client, self.stop))
            while not self.link.connected:
                await asyncio.sleep(0.01)
            self.backend.on_message({"t": "board", "v": 4, "uid": helpers.UID, "name": "knob"})
            self.assertEqual(self.backend.board_uid, helpers.UID)
            client.is_connected = False
            await asyncio.wait_for(task, 3)
        asyncio.run(scenario())
        self.assertFalse(self.link.connected)
        self.assertIsNone(self.backend.board_uid)
        self.assertFalse(self.link.send({"t": "ack", "id": 1}))

    def test_busy_ends_the_session(self):
        client = FakeClient()

        async def scenario():
            task = asyncio.create_task(self.link._session(client, self.stop))
            while not self.link.connected:
                await asyncio.sleep(0.01)
            client.notify(None, bytearray(b'{"t":"busy","reason":"pairing"}\n'))
            await asyncio.wait_for(task, 3)
        asyncio.run(scenario())
        self.assertEqual(self.link.busy, "pairing")

    def test_the_main_loop_with_a_fake_bleak(self):
        """No radio: the scanner and client are fakes. It connects to every board it found, each on its own link, and to
        nothing that isn't a board."""
        seen = {"devices": [], "scans": []}
        adv = lambda uuids: types.SimpleNamespace(service_uuids=uuids)  # noqa: E731
        stop = self.stop

        class Scanner:
            @staticmethod
            async def discover(timeout=5.0, return_adv=False):
                seen["scans"].append((timeout, return_adv))
                await asyncio.sleep(0.01)
                return {"A": ("dev-a", adv([SERVICE_UUID])), "B": ("dev-b", adv([SERVICE_UUID.upper()])),
                        "C": ("dev-c", adv(["7a6f6f6b-6565-7065-7200-000000000000"]))}

        class Client:
            def __init__(self, dev, timeout=None):
                seen["devices"].append(dev)
                self.client = FakeClient()
                self.client.is_connected = False  # ends the session straight after the hello

            async def __aenter__(self):
                return self.client

            async def __aexit__(self, *args):
                if len(seen["devices"]) >= 2:
                    stop.set()

        fake = types.ModuleType("bleak")
        fake.BleakClient, fake.BleakScanner = Client, Scanner
        ble = BleLink(self.backend.cfg, self.backend)
        with mock.patch.dict(sys.modules, {"bleak": fake}):
            ble.run(self.stop)
        self.assertEqual(sorted(seen["devices"]), ["dev-a", "dev-b"])  # each board once: the next try waits its backoff
        self.assertEqual(seen["scans"][0], (10, True))
        self.assertEqual(sorted(ble.links), ["A", "B"])
        self.assertIsNot(ble.links["A"], ble.links["B"])

    def test_without_bleak_it_says_so_and_stops(self):
        mark = helpers.LOGS.tell()
        with mock.patch.dict(sys.modules, {"bleak": None}):
            BleLink(self.backend.cfg, self.backend).run(self.stop)
        helpers.LOGS.seek(mark)
        self.assertIn("needs bleak", helpers.LOGS.read())
        helpers.LOGS.seek(0, 2)

    @unittest.skipUnless(importlib.util.find_spec("bleak"), "bleak isn't installed for this Python")
    def test_the_real_bleak_has_what_the_link_uses(self):
        """Only inspects the classes: nothing here scans or connects."""
        import inspect
        import bleak
        from bleak.backends.characteristic import BleakGATTCharacteristic
        self.assertIn("timeout", inspect.signature(bleak.BleakScanner.discover).parameters)
        self.assertIn("return_adv", inspect.signature(bleak.BleakScanner.discover).parameters)
        self.assertIn("timeout", inspect.signature(bleak.BleakClient.__init__).parameters)
        self.assertIn("response", inspect.signature(bleak.BleakClient.write_gatt_char).parameters)
        self.assertTrue(hasattr(bleak.BleakClient, "start_notify"))
        self.assertTrue(hasattr(bleak.BleakClient, "services"))
        self.assertTrue(hasattr(bleak.BleakClient, "is_connected"))
        self.assertTrue(hasattr(BleakGATTCharacteristic, "max_write_without_response_size"))


if __name__ == "__main__":
    unittest.main()
