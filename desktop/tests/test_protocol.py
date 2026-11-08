"""The contract: the samples in PROTOCOL.md, the test values in vectors.json, the framing and the size limits."""
import hashlib
import json
import os
import re
import types
import unittest

import helpers
from helpers import T0, FakeSpeech, FakeToggl, HomeCase, connect, drain, make_backend

import lockcode
from link import framing
from speech import adpcm


def samples():
    """{section: [message, ...]} from PROTOCOL.md's json blocks. The state sample spans several lines."""
    with open(os.path.join(helpers.PROTOCOL, "PROTOCOL.md")) as f:
        text = f.read()
    out = {"desktop": [], "board": []}
    for part, section in zip(text.split("## From the board"), ("desktop", "board")):
        for block in re.findall(r"```json\n(.*?)```", part, re.S):
            try:
                out[section].append(json.loads(block))
            except ValueError:
                out[section] += [json.loads(line) for line in block.splitlines() if line.strip()]
    return out


class Samples(HomeCase):
    def test_every_sample_is_json_and_all_kinds_are_there(self):
        s = samples()
        desktop = {m["t"] for m in s["desktop"]}
        board = {m["t"] for m in s["board"]}
        self.assertEqual(desktop, {"hello", "state", "agents", "ack", "entry_status", "projects", "tasks", "fetch_failed",
                                   "sync_result", "transcript", "lock_code", "clock"})
        self.assertEqual(board, {"board", "busy", "resync", "tap", "toggl", "fetch", "sync", "voice_start", "audio", "voice_end",
                                 "voice_cancel", "clock"})
        self.assertEqual({m["op"] for m in s["board"] if m["t"] == "toggl"}, {"start", "update", "shift", "stop"})

    def test_the_uuids_and_version_match(self):
        with open(os.path.join(helpers.PROTOCOL, "PROTOCOL.md")) as f:
            text = f.read()
        for value in (framing.SERVICE_UUID, framing.RX_UUID, framing.TX_UUID):
            self.assertIn(f"`{value}`", text)
        self.assertIn(f"# Bento protocol (version {framing.VERSION})", text)
        # A board's Bluetooth name is its own, such as "Bento Knob": the app finds it by its service, not its name.
        self.assertIn('named for its model', text)

    def test_the_samples_board_has_a_valid_uid(self):
        board = next(m for m in samples()["board"] if m["t"] == "board")
        self.assertEqual(framing.parse_uid(board["uid"]), board["uid"])

    def test_what_a_valid_uid_is(self):
        self.assertEqual(framing.parse_uid("9c41f07a"), "9c41f07a")
        self.assertEqual(framing.parse_uid("9C41F07A"), "9c41f07a")  # one board, however it's written
        for bad in ("", "9c41f07", "9c41f07a0", "zzzzzzzz", "9c41f07\n", "٩c41f07a", 12345678, None, True, ["9c41f07a"]):
            self.assertIsNone(framing.parse_uid(bad), bad)
        self.assertIsNone(framing.parse_uid(framing.UNKNOWN_BOARD))  # the unknown board can't be taken for a real one

    def test_the_app_takes_every_message_from_the_board(self):
        toggl, speech = FakeToggl(), FakeSpeech()
        self.addCleanup(toggl.close)
        self.addCleanup(speech.close)
        backend = make_backend(toggl, speech)
        link = connect(backend)
        from link.base import Link
        real = Link({}, backend)  # its handle() is what parses a line, for the board on the fake link
        real.backend = types.SimpleNamespace(on_message=lambda m, _link: backend.on_message(m, link))
        for msg in samples()["board"]:
            real.handle(framing.encode(msg)[:-1])
        drain(backend)
        self.assertEqual(sorted(m["id"] for m in link.of_kind("ack")), [17, 18, 19, 20, 21])
        self.assertEqual(link.of_kind("sync_result")[0]["req"], 7)
        self.assertEqual(real.busy, "another Mac is chosen")
        self.assertEqual(link.of_kind("clock")[0]["for"], 17)  # the board's clock, answered

    def test_what_the_app_sends_has_the_samples_shape(self):
        """Every field in a sample from the desktop app is in what the app really sends."""
        toggl, speech = FakeToggl(), FakeSpeech()
        self.addCleanup(toggl.close)
        self.addCleanup(speech.close)
        toggl.projects = [{"id": 123, "name": "Deep work", "color": "#4FB3F6"}]
        toggl.entries[1] = {"id": 1, "description": "Write the Q4 plan", "start": helpers.iso(T0), "project_id": 123,
                            "project": {"name": "Deep work", "color": "#4FB3F6"}, "task_id": 0}
        self.clock = helpers.Clock()
        backend = make_backend(toggl, speech, clock=self.clock)
        link = connect(backend)
        backend.runs.run_jobs()
        sample = {}
        for m in samples()["desktop"]:
            sample.setdefault(m["t"], []).append(m)

        self.assertEqual(backend.hello()["v"], sample["hello"][0]["v"])
        self.assertEqual(set(backend.hello()), set(sample["hello"][0]))
        state = backend.state()
        self.assertEqual(set(state), set(sample["state"][0]))
        whole, changes = sample["agents"]
        backend.zoo.on_hook({"event": "SubagentStart", "session_id": "s", "project": "api-server", "agent_id": "g", "agent_type": "Explore",
                             "rex_session": "session:1", "rex_block": "block:1"})
        backend.zoo.on_hook({"event": "Notification", "session_id": "s", "needs": True, "notice": "Look here"})
        backend.where_of = lambda a: "Rex"
        self.clock.advance(5)
        connect(backend, link)  # the `board` answer starts the whole list
        page = backend.agents_due()
        self.assertEqual(set(page), set(whole))
        self.assertEqual(set(page["rows"][0]), set(whole["rows"][0]))
        self.assertEqual(set(page["rows"][0]["subs"][0]), set(whole["rows"][0]["subs"][0]))
        backend.zoo.on_hook({"event": "SessionEnd", "session_id": "s"})
        backend.zoo.on_hook({"event": "PreToolUse", "session_id": "t", "project": "api-server", "activity": "Reading a.c"})
        self.clock.advance(5)
        gone = backend.agents_due()
        self.clock.advance(5)
        row = backend.agents_due()
        self.assertEqual(set(gone), set(changes))
        self.assertEqual(set(row), {"t", "rows"})
        self.assertLessEqual(set(changes["rows"][0]), set(row["rows"][0]) | {"age"})
        self.assertLessEqual(set(row["rows"][0]), set(whole["rows"][0]))

        for kind in ("projects", "tasks"):
            sent = link.of_kind(kind)[0]
            self.assertEqual(set(sent), set(sample[kind][0]))
            self.assertEqual(set(sent["rows"][0]), set(sample[kind][0]["rows"][0]))
        self.assertEqual(link.of_kind("tasks")[0]["rows"][0]["project"], sample["tasks"][0]["rows"][0]["project"])

        backend.on_message({"t": "toggl", "id": 1, "op": "start", "rid": 812, "at": T0, "desc": "x", "task_id": 0, "project": 0})
        self.assertEqual(set(link.of_kind("ack")[0]), set(sample["ack"][0]))
        toggl.fail_next(400)
        toggl.stop_elsewhere()
        drain(backend)
        self.assertEqual(link.of_kind("entry_status")[0], {"t": "entry_status", "rid": 812, "status": "rejected"})
        self.assertEqual(set(link.of_kind("entry_status")[0]), set(sample["entry_status"][0]))

        toggl.foreign_timer(project_id=123)
        backend.on_message({"t": "sync", "req": 7, "rid": 0})
        backend.runs.run_jobs()
        adopted = next(m for m in sample["sync_result"] if m["result"] == "adopted")
        self.assertEqual(set(link.of_kind("sync_result")[0]), set(adopted))

        backend.on_message({"t": "fetch", "req": 6, "what": "nonsense"})
        backend.runs.run_jobs()
        self.assertEqual(set(link.of_kind("fetch_failed")[0]), set(sample["fetch_failed"][0]))

        board_clock = next(m for m in samples()["board"] if m["t"] == "clock")
        backend.on_message(board_clock)
        answer = link.of_kind("clock")[-1]
        self.assertEqual(set(answer), set(sample["clock"][0]))
        for part in ("task", "timer", "pomo"):
            self.assertLessEqual(set(answer[part]), set(sample["clock"][0][part]) | {"start"}, part)
            self.assertLessEqual(set(sample["clock"][0][part]) - {"start"}, set(answer[part]), part)

    def test_a_board_sample_with_no_real_audio_gets_failed(self):
        backend = make_backend()
        link = connect(backend)
        for msg in samples()["board"]:  # "b64": "…" isn't base64
            if msg["t"] in ("voice_start", "audio"):
                backend.on_message(msg)
        backend.on_message({"t": "voice_end", "vid": 3, "packets": 1})
        self.assertEqual(link.of_kind("transcript"), [{"t": "transcript", "vid": 3, "error": "failed"}])


class Vectors(unittest.TestCase):
    def test_adpcm_decodes_the_reference_values(self):
        import base64
        v = helpers.load_vectors()["adpcm"]
        self.assertEqual(adpcm.decode(base64.b64decode(v["b64"]), len(v["decoded"]), v["predictor"], v["index"]), v["decoded"])

    def test_adpcm_agrees_with_the_reference_on_other_data(self):
        import random
        gen = helpers.reference_adpcm()
        rng = random.Random(7)
        pcm = [rng.randint(-32768, 32767) for _ in range(501)]
        data = gen.encode(pcm, 100, 20)
        self.assertEqual(adpcm.decode(data, len(pcm), 100, 20), gen.decode(data, len(pcm), 100, 20))

    def test_the_lock_hash(self):
        v = helpers.load_vectors()["lock"]
        self.assertEqual(lockcode.hash_code(v["code"], bytes.fromhex(v["salt"]), v["iter"]), v["hash"])
        self.assertEqual(lockcode.ITERATIONS, v["iter"])
        self.assertEqual(hashlib.pbkdf2_hmac("sha256", v["code"].encode(), bytes.fromhex(v["salt"]), v["iter"], 32).hex(), v["hash"])


class Framing(unittest.TestCase):
    def test_chunks_join_back(self):
        data = framing.encode({"t": "state", "agents": [{"name": "x" * 500}]})
        parts = framing.chunks(data, 182)
        self.assertTrue(all(len(p) <= 182 for p in parts))
        self.assertEqual(b"".join(parts), data)

    def test_line_buffer_splits_and_drops_long_lines(self):
        lb = framing.LineBuffer(limit=10)
        self.assertEqual(lb.feed(b'{"a":'), [])
        self.assertEqual(lb.feed(b'1}\n{"b":2}\n'), [b'{"a":1}', b'{"b":2}'])
        self.assertEqual(lb.feed(b"x" * 20), [])
        self.assertEqual(lb.feed(b"yy\nok\n"), [b"ok"])  # the long line is dropped up to its newline

    def test_a_long_line_arriving_whole_is_dropped_too(self):
        lb = framing.LineBuffer(limit=10)
        self.assertEqual(lb.feed(b"x" * 30 + b"\nok\n"), [b"ok"])

    def test_a_line_exactly_at_the_limit_passes(self):
        lb = framing.LineBuffer(limit=10)
        self.assertEqual(lb.feed(b"123456789\n"), [b"123456789"])
        self.assertEqual(lb.feed(b"1234567890\nok\n"), [b"ok"])

    def test_a_broken_string_can_still_be_encoded(self):
        self.assertTrue(framing.encode({"desc": "bad \ud800 text"}).endswith(b"\n"))


class Paging(unittest.TestCase):
    def rows(self, n=100, length=250):
        return [{"id": i + 1, "name": f"{i:03d} " + "é€" * (length // 2), "color": "#112233"} for i in range(n)]

    def test_each_page_fits_the_line_limit(self):
        rows = self.rows()
        pages = framing.paginate("projects", 5, rows)
        self.assertGreater(len(pages), 1)
        for i, page in enumerate(pages):
            self.assertLess(len(framing.encode(page)), framing.LINE_MAX)
            self.assertEqual((page["t"], page["req"], page["page"], page["pages"]), ("projects", 5, i, len(pages)))
        self.assertEqual([r for p in pages for r in p["rows"]], rows)

    def test_a_short_list_is_one_page_and_an_empty_one_still_arrives(self):
        self.assertEqual(framing.paginate("tasks", 6, [])[0], {"t": "tasks", "req": 6, "page": 0, "pages": 1, "rows": []})
        self.assertEqual(len(framing.paginate("tasks", 6, self.rows(3, 10))), 1)


if __name__ == "__main__":
    unittest.main()
