"""Several boards, one set of clocks: the clock hub's rules, two boards on one backend, Toggl runs named with `of`, and
two fake boards over the real TCP links."""
import json
import os
import threading
import time

import helpers
from helpers import T0, Clock, FakeLink, FakeToggl, HomeCase, connect, disconnect, drain, make_backend

import config
from clock_sync import ClockHub
from fake_board import FakeBoard
from link.tcp import TcpLink, ports

KNOB, SQUARE = "331d7d73", "64056f77"
MS = 1000


def board_clock(seq=1, changed=True, now=50_000, at=49_000, st="running", start=10_000, rid=5, of=None, desc="Write it",
                project=123, pomo_st="idle"):
    """A board's `clock`, with times by its own clock (milliseconds since it started, say)."""
    timer = {"st": st, "acc": 0, "rid": rid}
    if st == "running":
        timer["start"] = start
    if of:
        timer["of"] = of
    return {"t": "clock", "seq": seq, "changed": changed, "now": now, "at": at,
            "task": {"desc": desc, "task_id": 0, "project": {"id": project, "name": "Deep work", "color": "#4fb3f6"}},
            "timer": timer, "pomo": {"st": pomo_st, "set": 25, "total": 25 * 60 * MS, "left": 25 * 60 * MS, "rid": 0}}


class Hub(HomeCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.path = config.state_path("clock.json")
        self.hub = ClockHub(self.path, clock=self.clock)

    def test_nothing_to_say_before_any_board_spoke(self):
        self.assertIsNone(self.hub.message())

    def test_a_change_is_taken_answered_and_passed_on(self):
        answer, news = self.hub.take(KNOB, board_clock(seq=7))
        self.assertEqual((answer["for"], answer["from"], answer["seq"], answer["rev"]), (7, KNOB, 7, 1))
        self.assertNotIn("for", news)
        self.assertEqual(news["timer"]["of"], KNOB)  # a run with no `of` is the sender's own
        self.assertEqual(news["task"]["project"]["color"], "#4FB3F6")

    def test_times_are_read_as_that_long_before_the_message_arrived(self):
        """The board's clock is its own: only the distances from its `now` count."""
        now_ms = int(self.clock() * 1000)
        _, news = self.hub.take(KNOB, board_clock(now=50_000, at=49_000, start=10_000))
        self.assertEqual(news["at"], now_ms - 1_000)
        self.assertEqual(news["timer"]["start"], now_ms - 40_000)
        self.assertEqual(news["now"], now_ms)

    def test_a_time_from_the_future_is_now(self):
        now_ms = int(self.clock() * 1000)
        _, news = self.hub.take(KNOB, board_clock(now=50_000, at=90_000, start=80_000))
        self.assertEqual((news["at"], news["timer"]["start"]), (now_ms, now_ms))

    def test_the_newest_change_wins(self):
        self.hub.take(KNOB, board_clock(seq=1, now=50_000, at=49_000))  # a second ago
        answer, news = self.hub.take(SQUARE, board_clock(seq=4, now=9_000, at=4_000, st="paused"))  # five seconds ago
        self.assertIsNone(news)  # older: nobody else hears of it
        self.assertEqual((answer["for"], answer["from"], answer["timer"]["st"]), (4, KNOB, "running"))  # it learns what won
        answer, news = self.hub.take(SQUARE, board_clock(seq=5, now=9_000, at=9_000, st="paused"))
        self.assertEqual((news["from"], news["timer"]["st"], news["rev"]), (SQUARE, "paused", 2))
        self.assertEqual(answer["for"], 5)

    def test_a_boards_later_change_wins_over_its_own_earlier_one(self):
        """A quick turn of the knob: changes 40 ms apart, delivered by Bluetooth in one burst. Read from when each
        arrived, the later one can look a little older. The board's own order decides: its seq."""
        self.hub.take(KNOB, board_clock(seq=10, now=50_000, at=49_990, desc="25 min"))  # 10 ms before it was sent
        answer, news = self.hub.take(KNOB, board_clock(seq=11, now=50_040, at=50_020, desc="26 min"))  # 20 ms before
        self.assertIsNotNone(news, "the later change, though it reads 10 ms older")
        self.assertEqual((answer["for"], answer["seq"], answer["task"]["desc"]), (11, 11, "26 min"))
        # The order wraps with the board's 30-bit count, and a different board still goes by time.
        self.hub.take(KNOB, board_clock(seq=2 ** 30 - 1, now=60_000, at=60_000))
        self.assertIsNotNone(self.hub.take(KNOB, board_clock(seq=0, now=60_040, at=59_900))[1], "after the wrap")
        self.assertIsNone(self.hub.take(SQUARE, board_clock(seq=12, now=60_040, at=59_000))[1], "another board's older one")

    def test_an_unchanged_board_learns_the_latest_and_never_replaces_it(self):
        self.hub.take(KNOB, board_clock(seq=1, at=10_000))
        answer, news = self.hub.take(SQUARE, board_clock(seq=1, changed=False, at=50_000, st="idle"))
        self.assertIsNone(news)
        self.assertEqual((answer["from"], answer["timer"]["st"], answer["for"]), (KNOB, "running", 1))

    def test_with_none_yet_an_unchanged_board_sets_the_latest(self):
        answer, news = self.hub.take(SQUARE, board_clock(seq=1, changed=False, st="paused"))
        self.assertEqual((news["from"], news["timer"]["st"]), (SQUARE, "paused"))
        self.assertEqual(answer["for"], 1)

    def test_it_survives_a_restart(self):
        self.hub.take(KNOB, board_clock(seq=3))
        again = ClockHub(self.path, clock=self.clock)
        self.assertEqual((again.message()["from"], again.message()["rev"]), (KNOB, 1))
        _, news = again.take(SQUARE, board_clock(seq=1, now=60_000, at=60_000))
        self.assertEqual(news["rev"], 2)

    def test_a_broken_file_is_no_latest(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write("{nope")
        self.assertIsNone(ClockHub(self.path, clock=self.clock).message())

    def test_an_old_build_or_a_broken_message_changes_nothing(self):
        self.assertEqual(self.hub.take("unknown", board_clock()), (None, None))
        self.assertEqual(self.hub.take(None, board_clock()), (None, None))
        self.assertEqual(self.hub.take(KNOB, dict(board_clock(), now="soon")), (None, None))
        self.assertIsNone(self.hub.message())

    def test_whatever_arrives_comes_out_in_bounds(self):
        msg = board_clock(desc="プ" * 200)
        msg["timer"].update(st="dancing", acc=-5, rid=2 ** 40, of="not-a-uid")
        msg["pomo"].update(st="done", set=900, total=10 ** 12, left=-1)
        msg["task"]["project"] = {"id": 7, "name": "x" * 300, "color": "red"}
        _, news = self.hub.take(KNOB, msg)
        self.assertLessEqual(len(news["task"]["desc"].encode()), 95)
        self.assertEqual((news["timer"]["st"], news["timer"]["acc"], news["timer"]["of"]), ("idle", 0, KNOB))
        self.assertNotIn("start", news["timer"])
        self.assertLessEqual(news["timer"]["rid"], 0xFFFFFFF0)
        self.assertEqual((news["pomo"]["st"], news["pomo"]["set"], news["pomo"]["left"]), ("done", 60, 0))
        self.assertLessEqual(news["pomo"]["total"], 2 * 3600 * MS)
        self.assertEqual(news["task"]["project"]["color"], "#8E8E94")
        self.assertLessEqual(len(news["task"]["project"]["name"].encode()), 47)

    def test_a_project_never_chosen_and_no_project_stay_apart(self):
        msg = board_clock()
        msg["task"]["project"] = {"id": -1}
        _, news = self.hub.take(KNOB, msg)
        self.assertEqual(news["task"]["project"]["id"], -1)
        msg = board_clock(seq=2, at=50_000)
        msg["task"]["project"] = None
        _, news = self.hub.take(KNOB, msg)
        self.assertEqual(news["task"]["project"]["id"], 0)


class TwoBoards(HomeCase):
    """One backend, two boards: each is its own session; only the clocks and the lock code are shared."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.backend = make_backend(clock=self.clock)
        self.knob = connect(self.backend, FakeLink(), uid=KNOB, name="knob-lcd-1.8")
        self.square = connect(self.backend, FakeLink(), uid=SQUARE, name="amoled-2.16")

    def test_a_change_on_one_board_shows_on_the_other(self):
        self.backend.on_message(board_clock(seq=3), self.knob)
        mine, theirs = self.knob.of_kind("clock"), self.square.of_kind("clock")
        self.assertEqual((mine[-1]["for"], mine[-1]["from"]), (3, KNOB))
        self.assertEqual(len(theirs), 1)
        self.assertEqual((theirs[0]["from"], theirs[0]["timer"]["st"], theirs[0]["timer"]["of"]), (KNOB, "running", KNOB))
        self.assertNotIn("for", theirs[0])

    def test_a_board_that_connects_later_catches_up(self):
        self.backend.on_message(board_clock(seq=3), self.knob)
        third = connect(self.backend, FakeLink(), uid="0badcafe", name="amoled-2.16")
        self.assertEqual(third.of_kind("clock")[0]["from"], KNOB)

    def test_an_older_change_loses_and_its_board_is_told(self):
        self.backend.on_message(board_clock(seq=3, now=50_000, at=49_000), self.knob)
        self.backend.on_message(board_clock(seq=9, now=50_000, at=40_000, st="paused"), self.square)
        answer = self.square.of_kind("clock")[-1]
        self.assertEqual((answer["for"], answer["from"], answer["timer"]["st"]), (9, KNOB, "running"))
        self.assertEqual(len(self.knob.of_kind("clock")), 1)  # only its own answer: nothing new for it

    def test_each_board_has_its_own_agents_and_answers(self):
        self.assertIsNot(self.backend._board(self.knob).agents, self.backend._board(self.square).agents)
        self.backend.on_message({"t": "toggl", "id": 4, "op": "start", "rid": 1, "at": T0, "desc": "x", "task_id": 0,
                                 "project": 0}, self.square)
        self.assertEqual(self.square.of_kind("ack"), [{"t": "ack", "id": 4}])
        self.assertEqual(self.knob.of_kind("ack"), [])

    def test_one_board_leaving_leaves_the_other(self):
        disconnect(self.backend, self.knob)
        self.assertEqual([b["uid"] for b in self.backend.http_status()["boards"]], [SQUARE])
        self.backend.on_message(board_clock(seq=1), self.square)
        self.assertEqual(self.square.of_kind("clock")[-1]["for"], 1)

    def test_the_status_lists_both(self):
        status = self.backend.http_status()
        self.assertEqual(sorted(b["uid"] for b in status["boards"]), sorted([KNOB, SQUARE]))
        self.assertEqual(status["link"], "ble")

    def test_the_lock_code_set_once_reaches_both(self):
        self.backend.lock_store.set_code(helpers.LOCK_CODE)
        self.backend.reload()
        self.assertEqual(len(self.knob.of_kind("lock_code")), 1)
        self.assertEqual(len(self.square.of_kind("lock_code")), 1)
        self.backend.reload()  # and only once
        self.assertEqual(len(self.knob.of_kind("lock_code")), 1)
        later = connect(self.backend, FakeLink(), uid="0badcafe", name="amoled-2.16")
        self.assertEqual(len(later.of_kind("lock_code")), 1)  # a board that was away gets it when it connects

    def test_a_voice_clip_survives_the_other_board_leaving(self):
        self.backend.on_message({"t": "voice_start", "vid": 1, "rate": 16000}, self.square)
        disconnect(self.backend, self.knob)
        self.assertIsNotNone(self.backend.voice.clip)
        disconnect(self.backend, self.square)
        self.assertIsNone(self.backend.voice.clip)


class RunsOfAnotherBoard(HomeCase):
    """A board that shows another board's run stops it, changes it and shifts it, by naming it with `of`."""

    def setUp(self):
        super().setUp()
        self.toggl = FakeToggl()
        self.addCleanup(self.toggl.close)
        self.clock = Clock()
        self.backend = make_backend(self.toggl, clock=self.clock)
        self.knob = connect(self.backend, FakeLink(), uid=KNOB)
        self.square = connect(self.backend, FakeLink(), uid=SQUARE)

    def toggl_msg(self, link, **msg):
        self.backend.on_message({"t": "toggl", **msg}, link)

    def entry(self):
        return next(iter(self.toggl.entries.values()))

    def test_the_square_stops_the_knobs_run(self):
        self.toggl_msg(self.knob, id=1, op="start", rid=5, at=T0, desc="Write it", task_id=0, project=0)
        self.clock.advance(5)
        drain(self.backend)
        self.clock.advance(60)
        self.toggl_msg(self.square, id=1, op="stop", rid=5, of=KNOB, at=T0 + 65)
        drain(self.backend)
        self.assertEqual(self.square.of_kind("ack"), [{"t": "ack", "id": 1}])
        self.assertEqual(self.backend.runs.queued(), 0)
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/stop")), 1)
        # The knob's own stop for it, later, changes nothing.
        self.toggl_msg(self.knob, id=2, op="stop", rid=5, at=T0 + 90)
        drain(self.backend)
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/stop")), 1)

    def test_a_change_from_the_other_board_renames_the_run(self):
        self.toggl_msg(self.knob, id=1, op="start", rid=5, at=T0, desc="Write it", task_id=0, project=0)
        self.clock.advance(5)
        drain(self.backend)
        self.toggl_msg(self.square, id=7, op="update", rid=5, of=KNOB, desc="Review it")
        self.clock.advance(5)
        drain(self.backend)
        self.assertEqual(self.entry()["description"], "Review it")

    def test_the_same_rid_from_two_boards_is_two_runs(self):
        self.toggl_msg(self.knob, id=1, op="start", rid=5, at=T0, desc="knob's", task_id=0, project=0)
        self.toggl_msg(self.square, id=1, op="start", rid=5, at=T0 + 30, desc="square's", task_id=0, project=0)
        self.toggl_msg(self.square, id=2, op="stop", rid=5, at=T0 + 100)  # the square's own run 5
        runs = {(r["uid"], r["rid"]): r for r in self.backend.runs.data["runs"]}
        self.assertEqual(runs[(KNOB, 5)]["stop"], T0 + 30)  # ended where the square's began
        self.assertEqual(runs[(SQUARE, 5)]["stop"], T0 + 100)

    def test_a_start_that_arrives_late_ends_where_the_later_run_began(self):
        self.toggl_msg(self.knob, id=1, op="start", rid=5, at=T0 + 300, desc="newer", task_id=0, project=0)
        self.toggl_msg(self.square, id=1, op="start", rid=2, at=T0, desc="from a board that was away", task_id=0,
                       project=0)
        runs = {r["uid"]: r for r in self.backend.runs.data["runs"]}
        self.assertEqual(runs[KNOB]["stop"], 0)  # still running
        self.assertEqual(runs[SQUARE]["stop"], T0 + 300)


class OverTcp(HomeCase):
    """The real links, two fake boards on two ports, one app."""

    def test_ports_can_be_one_a_list_or_a_string(self):
        self.assertEqual(ports({"display_port": 7777}), [7777])
        self.assertEqual(ports({"display_port": [7777, 7778, 7777]}), [7777, 7778])
        self.assertEqual(ports({"display_port": "7777, 7778"}), [7777, 7778])
        self.assertEqual(ports({"display_port": "nope"}), [7777])

    def test_a_clock_started_on_one_board_reaches_the_other(self):
        knob, square = FakeBoard(uid=KNOB), FakeBoard(uid=SQUARE, name="amoled-2.16")
        self.addCleanup(knob.stop)
        self.addCleanup(square.stop)
        backend = make_backend(clock=time.time, link="tcp", display_port=[knob.port, square.port])
        stop = threading.Event()
        links = [TcpLink(backend.cfg, backend, p) for p in ports(backend.cfg)]
        for link in links:
            link.reconnect_min_s = 0.05
            threading.Thread(target=link.run, args=(stop,), daemon=True).start()
        self.addCleanup(stop.set)
        knob.wait_for("hello")
        square.wait_for("hello")
        end = time.monotonic() + 5
        while not (len(backend.http_status()["boards"]) == 2 and all(b["uid"] for b in backend.http_status()["boards"])):
            self.assertLess(time.monotonic(), end, "both boards should have answered")
            time.sleep(0.02)
        knob.send(board_clock(seq=1, now=1_000, at=900, start=100))
        got = square.wait_for("clock", where=lambda m: m.get("from") == KNOB)
        self.assertEqual((got["timer"]["st"], got["timer"]["rid"], got["timer"]["of"]), ("running", 5, KNOB))
        self.assertLess(abs(got["now"] - got["timer"]["start"] - 900), 500)  # counted 0.9 s, give or take the trip
        answer = knob.wait_for("clock", where=lambda m: m.get("for") == 1)
        self.assertEqual(answer["from"], KNOB)
        self.assertIn(json.dumps(KNOB), json.dumps(answer))
