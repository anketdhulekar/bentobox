"""The backend between the board and Toggl: acks, the state's key fields, the list load, and what's owed to a board."""
import json
import time
import unittest

import helpers
from helpers import KEY, MODEL, ORG, OTHER_UID, SPEECH_KEY, T0, UID, WID, Clock, FakeToggl, HomeCase, connect, disconnect, drain, make_backend, iso

import config
from link.framing import LINE_MAX, encode
from secrets_store import MemoryStore, SecretStoreError
from toggl.runs import SETTLE_S


def start_msg(msg_id=17, rid=812, at=T0, desc="Write the Q4 plan", project=123):
    return {"t": "toggl", "id": msg_id, "op": "start", "rid": rid, "at": at, "desc": desc, "task_id": 0, "project": project}


class Base(HomeCase):
    def setUp(self):
        super().setUp()
        self.toggl = FakeToggl()
        self.addCleanup(self.toggl.close)
        self.toggl.projects = [{"id": 123, "name": "Deep work", "color": "#4FB3F6"}]
        self.clock = Clock()

    def backend(self, **kw):
        kw.setdefault("clock", self.clock)
        return make_backend(self.toggl, **kw)


class Ack(Base):
    def test_the_ack_comes_with_no_request_to_toggl(self):
        backend = self.backend()
        link = connect(backend)
        backend.runs.run_jobs()
        self.toggl.clear()
        backend.on_message(start_msg())
        self.assertEqual(link.of_kind("ack"), [{"t": "ack", "id": 17}])
        self.assertEqual(self.toggl.count(), 0)

    def test_it_is_saved_before_the_ack(self):
        backend = self.backend()
        seen = {}

        class Checking(helpers.FakeLink):
            def send(link, msg, **kw):
                if msg["t"] == "ack":
                    with open(config.state_path("toggl.json")) as f:
                        seen["saved"] = json.load(f)["runs"][0]["rid"]
                return super().send(msg, **kw)
        connect(backend, Checking())
        backend.on_message(start_msg())
        self.assertEqual(seen["saved"], 812)

    def test_a_repeated_id_is_acknowledged_again_with_no_effect(self):
        backend = self.backend()
        link = connect(backend)
        backend.on_message(start_msg())
        drain(backend)
        self.toggl.clear()
        backend.on_message(start_msg())
        drain(backend)
        self.assertEqual(link.of_kind("ack"), [{"t": "ack", "id": 17}, {"t": "ack", "id": 17}])
        self.assertEqual(self.toggl.count(), 0)

    def test_nothing_is_lost_over_a_restart_and_a_resend(self):
        backend = self.backend()
        link = connect(backend)
        backend.on_message(start_msg())
        backend.on_message({"t": "toggl", "id": 20, "op": "stop", "rid": 812, "at": T0 + 600})
        self.assertEqual(self.toggl.count(), 0)  # nothing has gone to Toggl for the run yet
        del backend

        backend = self.backend()  # the app restarted, and the ack didn't reach the board, so it sends them again
        link = connect(backend)
        backend.runs.run_jobs()
        backend.on_message(start_msg())
        backend.on_message({"t": "toggl", "id": 20, "op": "stop", "rid": 812, "at": T0 + 600})
        self.assertEqual([m["id"] for m in link.of_kind("ack")], [17, 20])
        drain(backend)
        self.assertEqual(self.toggl.reqs("POST", "time-entries")[0].body["duration"], 600)
        self.assertEqual(len(self.toggl.reqs("POST", "time-entries")), 1)
        self.assertEqual(self.toggl.reqs("POST", "tracking/start"), [])


class Reflash(Base):
    """A reflashed board has a new uid and starts counting again. Nothing of the old board's may get in its way."""

    def setUp(self):
        super().setUp()
        self.b = self.backend()
        self.link = connect(self.b, uid=UID)
        self.b.runs.run_jobs()
        self.toggl.clear()
        self.link.sent.clear()

    def reflash(self):
        disconnect(self.b, self.link)
        connect(self.b, self.link, uid=OTHER_UID)
        self.b.runs.run_jobs()
        self.toggl.clear()
        self.link.sent.clear()

    def test_id_restarting_at_1_is_handled_normally(self):
        for i, op in ((1, "start"), (2, "stop")):
            self.b.on_message({"t": "toggl", "id": i, "op": op, "rid": 1, "at": T0 + (0 if op == "start" else 100), "desc": "Before", "task_id": 0, "project": 0})
        drain(self.b)
        self.assertEqual(self.toggl.reqs("POST", "time-entries")[0].body["description"], "Before")
        self.reflash()
        self.b.on_message(start_msg(msg_id=1, rid=1, at=T0 + 200, desc="After", project=0))
        self.assertEqual(self.link.of_kind("ack"), [{"t": "ack", "id": 1}])
        self.assertEqual(self.b.runs.queued(), 1)
        drain(self.b)
        self.assertEqual(self.toggl.entries[self.toggl.running]["description"], "After")
        self.b.on_message({"t": "toggl", "id": 2, "op": "stop", "rid": 1, "at": T0 + 500})
        drain(self.b)
        self.assertEqual(self.toggl.running, 0)
        self.assertEqual(self.b.runs.queued(), 0)

    def test_the_new_uids_own_repeats_still_count_also_after_a_restart(self):
        self.reflash()
        self.b.on_message(start_msg(msg_id=1, rid=1, desc="After", project=0))
        self.b.on_message(start_msg(msg_id=1, rid=1, desc="After", project=0))
        self.assertEqual(self.b.runs.queued(), 1)
        backend = self.backend()
        link = connect(backend, uid=OTHER_UID)
        backend.on_message(start_msg(msg_id=1, rid=1, desc="After", project=0))
        self.assertEqual(backend.runs.queued(), 1)
        self.assertEqual(link.of_kind("ack"), [{"t": "ack", "id": 1}])

    def test_a_run_the_old_uid_left_open_is_stopped_first(self):
        """Nobody can stop it from the board any more, because the board that started it has forgotten it."""
        self.b.on_message(start_msg(msg_id=50, rid=812, at=T0, desc="Before", project=0))
        drain(self.b)
        before = self.toggl.running
        self.reflash()
        self.b.on_message(start_msg(msg_id=1, rid=1, at=T0 + 300, desc="After", project=0))
        drain(self.b)
        self.assertEqual(self.toggl.entries[before]["end"], iso(T0 + 300))
        self.assertEqual(self.toggl.entries[self.toggl.running]["description"], "After")
        self.assertEqual(self.b.runs.queued(), 1)

    def test_a_swipe_down_takes_over_what_the_old_uid_left_running(self):
        self.b.on_message(start_msg(msg_id=50, rid=812, at=T0, desc="Before", project=0))
        drain(self.b)
        self.reflash()
        self.b.on_message({"t": "sync", "req": 7, "rid": 0})
        self.b.runs.run_jobs()
        result = self.link.of_kind("sync_result")[0]
        self.assertEqual((result["result"], result["rid"], result["desc"]), ("adopted", 1, "Before"))  # counted from its own 0
        self.assertEqual(self.b.runs.queued(), 1)
        running = self.toggl.running
        self.toggl.clear()
        self.b.on_message({"t": "toggl", "id": 1, "op": "stop", "rid": 1, "at": T0 + 5})  # adopted, so not deleted
        drain(self.b)
        self.assertEqual(self.toggl.entries[running]["end"], iso(T0 + 5))

    def test_its_lists_come_again_because_its_flash_was_erased(self):
        disconnect(self.b, self.link)
        connect(self.b, self.link, uid=OTHER_UID)
        self.b.runs.run_jobs()
        self.assertEqual([m["req"] for m in self.link.of_kind("projects")], [0])
        self.assertEqual([m["req"] for m in self.link.of_kind("tasks")], [0])

    def test_a_rejection_for_the_old_uid_never_reaches_the_new_one(self):
        self.b.on_message(start_msg(msg_id=50, rid=812))
        self.reflash()
        self.b.client.wid, self.b.client.oid = WID, ORG
        self.toggl.fail_next(422)
        drain(self.b)
        self.assertEqual(self.link.of_kind("entry_status"), [])


class SameModel(Base):
    """Two boards of the same model: the same name, and the same id and rid numbers, with different uids."""

    def setUp(self):
        super().setUp()
        self.b = self.backend()
        self.link = helpers.FakeLink()

    def use(self, uid):
        if self.b.board_uid:
            disconnect(self.b, self.link)
        connect(self.b, self.link, uid=uid, name=MODEL)
        self.b.runs.run_jobs()
        self.link.sent.clear()

    def test_the_same_id_and_rid_dont_collide(self):
        self.use(UID)
        self.b.on_message(start_msg(msg_id=17, rid=812, at=T0, desc="First board", project=0))
        drain(self.b)
        first = self.toggl.running
        self.use(OTHER_UID)
        self.b.on_message(start_msg(msg_id=17, rid=812, at=T0 + 100, desc="Second board", project=0))
        self.assertEqual(self.link.of_kind("ack"), [{"t": "ack", "id": 17}])
        drain(self.b)
        second = self.toggl.running
        self.assertNotEqual(first, second)
        self.assertEqual(self.toggl.entries[first]["end"], iso(T0 + 100))  # the protocol: an earlier open run stops first
        self.assertEqual(self.toggl.entries[second]["description"], "Second board")

    def test_a_stop_from_one_never_touches_the_others_run(self):
        self.use(UID)
        self.b.on_message(start_msg(msg_id=17, rid=812, at=T0, desc="First board", project=0))
        drain(self.b)
        self.use(OTHER_UID)
        self.b.on_message(start_msg(msg_id=17, rid=812, at=T0 + 100, desc="Second board", project=0))
        drain(self.b)
        second = self.toggl.running
        self.use(UID)  # the first board's saved stop arrives, with the same rid as the second board's run
        self.b.on_message({"t": "toggl", "id": 18, "op": "stop", "rid": 812, "at": T0 + 150})
        self.assertEqual(self.link.of_kind("ack"), [{"t": "ack", "id": 18}])
        self.toggl.clear()
        drain(self.b)
        self.assertEqual(self.toggl.running, second)
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])
        self.assertEqual(self.b.runs.queued(), 1)

    def test_an_update_from_one_never_changes_the_others_run(self):
        self.use(UID)
        self.b.on_message(start_msg(msg_id=17, rid=812, at=T0, desc="First board", project=0))
        drain(self.b)
        self.use(OTHER_UID)
        self.b.on_message(start_msg(msg_id=17, rid=812, at=T0 + 100, desc="Second board", project=0))
        drain(self.b)
        self.use(UID)
        self.b.on_message({"t": "toggl", "id": 18, "op": "update", "rid": 812, "desc": "From the first board"})
        self.clock.advance(5)
        self.toggl.clear()
        drain(self.b)
        self.assertEqual(self.toggl.reqs("PATCH"), [])
        self.assertEqual(self.toggl.entries[self.toggl.running]["description"], "Second board")

    def test_a_sync_never_takes_the_others_run_for_its_own(self):
        """Both use rid 812. The timer running in Toggl is the first board's, so the second one isn't "in sync": it adopts."""
        self.use(UID)
        self.b.on_message(start_msg(msg_id=17, rid=812, at=T0, desc="First board", project=0))
        drain(self.b)
        running = self.toggl.running
        self.use(OTHER_UID)
        self.b.on_message({"t": "sync", "req": 7, "rid": 812})
        self.b.runs.run_jobs()
        result = self.link.of_kind("sync_result")[0]
        self.assertEqual((result["result"], result["rid"], result["desc"]), ("adopted", 813, "First board"))
        self.assertEqual(self.toggl.running, running)
        self.assertEqual(self.b.runs.queued(), 1)

    def test_seen_ids_are_separate_over_a_restart_too(self):
        self.use(UID)
        self.b.on_message(start_msg(msg_id=17, rid=1, at=T0, desc="A", project=0))
        self.use(OTHER_UID)
        self.b.on_message(start_msg(msg_id=18, rid=2, at=T0 + 50, desc="B", project=0))
        backend = self.backend()
        connect(backend, uid=UID)
        backend.on_message(start_msg(msg_id=18, rid=2, at=T0 + 60, desc="Not a repeat for this uid", project=0))
        self.assertEqual(backend.runs.queued(), 3)
        backend.on_message(start_msg(msg_id=17, rid=1, at=T0, desc="A repeat for this uid", project=0))
        self.assertEqual(backend.runs.queued(), 3)

    def test_adopted_rids_are_counted_per_uid(self):
        self.use(UID)
        self.b.on_message(start_msg(msg_id=1, rid=812, at=T0, desc="A", project=0))
        self.b.on_message({"t": "toggl", "id": 2, "op": "stop", "rid": 812, "at": T0 + 100})
        drain(self.b)
        self.use(OTHER_UID)
        self.b.on_message(start_msg(msg_id=1, rid=5, at=T0 + 200, desc="B", project=0))
        self.b.on_message({"t": "toggl", "id": 2, "op": "stop", "rid": 5, "at": T0 + 300})
        drain(self.b)
        self.toggl.foreign_timer()
        self.b.on_message({"t": "sync", "req": 7, "rid": 0})
        self.b.runs.run_jobs()
        self.assertEqual(self.link.of_kind("sync_result")[0]["rid"], 6)
        self.use(UID)
        self.b.on_message({"t": "sync", "req": 8, "rid": 0})
        self.b.runs.run_jobs()
        self.assertEqual(self.link.of_kind("sync_result")[0]["rid"], 813)

    def test_a_new_name_or_firmware_on_the_same_uid_is_the_same_board(self):
        backend = self.backend()
        link = connect(backend, uid=UID, name="knob-lcd-1.8")
        backend.on_message(start_msg(msg_id=17, rid=1))
        backend.runs.run_jobs()
        disconnect(backend, link)
        connect(backend, link, uid=UID, name="renamed-in-a-new-build")
        backend.on_message(start_msg(msg_id=17, rid=1))
        backend.runs.run_jobs()
        self.assertEqual(backend.runs.queued(), 1)
        self.assertEqual(len(link.of_kind("projects")), 1)

    def test_a_uid_is_the_same_however_it_is_written(self):
        backend = self.backend()
        link = connect(backend, uid="9C41F07A")
        self.assertEqual(backend.board_uid, "9c41f07a")
        backend.on_message(start_msg(msg_id=17, rid=1))
        disconnect(backend, link)
        connect(backend, link, uid="9c41f07a")
        backend.on_message(start_msg(msg_id=17, rid=1))
        self.assertEqual(backend.runs.queued(), 1)


class NoUid(Base):
    """A `board` with no uid comes from an old build: a protocol error, not a crash."""

    def logs_during(self, action):
        mark = helpers.LOGS.tell()
        action()
        helpers.LOGS.seek(mark)
        text = helpers.LOGS.read()
        helpers.LOGS.seek(0, 2)
        return text

    def test_it_is_logged_and_the_connection_works_as_the_fixed_unknown_board(self):
        backend = self.backend()
        holder = {}
        text = self.logs_during(lambda: holder.update(link=connect(backend, uid=None)))
        link = holder["link"]
        self.assertIn("protocol error", text)
        self.assertIn("uid", text)
        self.assertEqual(backend.board_uid, "unknown")
        backend.runs.run_jobs()
        self.assertEqual([m["req"] for m in link.of_kind("projects")], [0])  # it still gets what a board gets
        backend.on_message(start_msg())
        self.assertEqual(link.of_kind("ack"), [{"t": "ack", "id": 17}])
        drain(backend)
        self.assertTrue(self.toggl.running)
        backend.on_message({"t": "toggl", "id": 18, "op": "stop", "rid": 812, "at": T0 + 100})
        drain(backend)
        self.assertEqual(self.toggl.running, 0)

    def test_it_is_one_fixed_board_across_connections_and_restarts(self):
        backend = self.backend()
        link = connect(backend, uid=None)
        backend.runs.run_jobs()
        backend.on_message(start_msg())
        disconnect(backend, link)
        connect(backend, link, uid=None, name="another-old-build")
        backend.on_message(start_msg())  # the same id: a repeat
        backend.runs.run_jobs()
        self.assertEqual(backend.runs.queued(), 1)
        self.assertEqual(len(link.of_kind("projects")), 1)
        backend = self.backend()
        link = connect(backend, uid=None)
        backend.on_message(start_msg())
        self.assertEqual(backend.runs.queued(), 1)

    def test_it_never_mixes_with_a_real_uid(self):
        backend = self.backend()
        link = connect(backend, uid=None)
        backend.on_message(start_msg(msg_id=17, rid=1))
        disconnect(backend, link)
        connect(backend, link, uid=UID)
        backend.on_message(start_msg(msg_id=17, rid=1, at=T0 + 50))
        self.assertEqual(backend.runs.queued(), 2)

    def test_a_bad_uid_counts_the_same(self):
        for bad in ("", "short", "9c41f07a0", "zzzzzzzz", 12345678, True, ["9c41f07a"], {"a": 1}):
            with self.subTest(bad):
                backend = self.backend()
                link = helpers.FakeLink()
                backend.session_started(link)
                text = self.logs_during(lambda: backend.on_message({"t": "board", "v": 4, "uid": bad, "name": MODEL}))
                self.assertIn("protocol error", text)
                self.assertEqual(backend.board_uid, "unknown")

    def test_a_good_uid_is_no_error(self):
        backend = self.backend()
        text = self.logs_during(lambda: connect(backend, uid=UID))
        self.assertNotIn("protocol error", text)
        self.assertIn(UID, text)

    def test_a_board_with_nothing_else_either(self):
        backend = self.backend()
        link = helpers.FakeLink()
        backend.session_started(link)
        backend.on_message({"t": "board"})
        self.assertEqual((backend.board_uid, backend.board_name), ("unknown", "board"))

    def test_status_shows_both(self):
        backend = self.backend()
        connect(backend, uid=UID)
        self.assertEqual(backend.http_status()["boards"], [{"uid": UID, "name": MODEL, "trusted": True}])


class BeforeTheBoardAnswers(Base):
    """The protocol has these only after `board`. With no uid yet they can't be told apart, so nothing is kept for them."""

    def test_a_toggl_message_gets_no_ack_and_is_taken_when_sent_again(self):
        backend = self.backend()
        link = connect(backend, board=False)
        backend.on_message(start_msg())
        self.assertEqual(link.of_kind("ack"), [])
        self.assertEqual(backend.runs.queued(), 0)
        self.assertEqual(backend.runs.data["seen"], {})
        backend.on_message({"t": "board", "v": 4, "uid": UID, "name": MODEL})
        backend.on_message(start_msg())
        self.assertEqual(link.of_kind("ack"), [{"t": "ack", "id": 17}])
        self.assertEqual(backend.runs.queued(), 1)

    def test_a_sync_gets_nothing_and_no_request(self):
        """Before `board`, nobody knows whose board this is, so it's untrusted: the board's own wait says it failed."""
        backend = self.backend()
        link = connect(backend, board=False)
        backend.on_message({"t": "sync", "req": 7, "rid": 812})
        backend.runs.run_jobs()
        self.assertEqual(link.of_kind("sync_result"), [])
        self.assertEqual(self.toggl.count(), 0)

    def test_the_uid_goes_with_the_session(self):
        backend = self.backend()
        link = connect(backend, uid=UID)
        disconnect(backend, link)
        self.assertIsNone(backend.board_uid)
        connect(backend, link, board=False)
        backend.on_message(start_msg())
        self.assertEqual(link.of_kind("ack"), [])


class NoKey(Base):
    def test_state_says_so(self):
        backend = self.backend(store=MemoryStore())
        connect(backend)
        state = backend.state()
        self.assertEqual((state["toggl"], state["voice"]), ("no_key", False))

    def test_with_keys(self):
        backend = self.backend()
        state = backend.state()
        self.assertEqual((state["toggl"], state["voice"]), ("ok", True))

    def test_each_key_counts_on_its_own(self):
        state = self.backend(store=MemoryStore(toggl=KEY)).state()
        self.assertEqual((state["toggl"], state["voice"]), ("ok", False))
        state = self.backend(store=MemoryStore(speech=SPEECH_KEY)).state()
        self.assertEqual((state["toggl"], state["voice"]), ("no_key", True))

    def test_a_keychain_error_counts_as_no_key(self):
        class Broken(MemoryStore):
            def has(self, name):
                raise SecretStoreError("couldn't run the macOS security tool")
            get = has
        state = self.backend(store=Broken()).state()
        self.assertEqual((state["toggl"], state["voice"]), ("no_key", False))

    def test_messages_are_held_and_nothing_goes_to_toggl(self):
        backend = self.backend(store=MemoryStore())
        link = connect(backend)
        backend.on_message(start_msg())
        backend.on_message({"t": "fetch", "req": 5, "what": "projects"})
        backend.on_message({"t": "sync", "req": 7, "rid": 812})
        drain(backend)
        self.assertEqual(self.toggl.count(), 0)
        self.assertEqual(link.of_kind("ack"), [{"t": "ack", "id": 17}])
        self.assertEqual(link.of_kind("fetch_failed"), [{"t": "fetch_failed", "req": 5, "what": "projects"}])
        self.assertEqual(link.of_kind("sync_result"), [{"t": "sync_result", "req": 7, "result": "failed"}])
        self.assertEqual(link.of_kind("projects"), [])

    def test_setting_a_key_starts_everything_that_waited(self):
        store = MemoryStore()
        backend = self.backend(store=store)
        link = connect(backend)
        backend.on_message(start_msg())
        drain(backend)
        store.set("toggl", KEY)
        store.set("speech", SPEECH_KEY)
        backend.reload()
        self.assertEqual(backend.state()["toggl"], "ok")
        self.assertTrue(backend.state()["voice"])
        drain(backend)
        self.assertEqual(self.toggl.reqs("POST", "tracking/start")[0].body["start"], iso(T0))
        self.assertEqual(len(link.of_kind("projects")), 1)  # and the list load that couldn't happen before

    def test_a_key_toggl_rejected_is_like_no_key_until_it_changes(self):
        store = MemoryStore(toggl="wrong")
        backend = self.backend(store=store)
        connect(backend)
        backend.runs.run_jobs()
        drain(backend)
        self.assertEqual(backend.state()["toggl"], "no_key")
        store.set("toggl", KEY)
        backend.reload()
        self.assertEqual(backend.state()["toggl"], "ok")

    def test_the_organization_can_be_set_later(self):
        self.toggl.settings_org = False
        backend = self.backend(store=MemoryStore(toggl=KEY))
        backend.org = 0
        link = connect(backend)
        backend.runs.run_jobs()
        self.assertEqual(backend.state()["toggl"], "no_key")
        self.assertEqual(link.of_kind("projects"), [])
        config.save_config(dict(config.load_config(), toggl_org=ORG))
        backend.reload()
        self.assertEqual(backend.state()["toggl"], "ok")
        backend.runs.run_jobs()
        self.assertEqual(len(link.of_kind("projects")), 1)


class State(Base):
    def test_the_fields(self):
        backend = self.backend()
        now = time.time()
        backend.clock = lambda: now
        state = backend.state()
        self.assertEqual(state["v"], 4)
        self.assertEqual(state["now"], int(now))
        self.assertEqual(state["utc"], time.localtime(now).tm_gmtoff)
        self.assertEqual(state["n"], 0)
        self.assertNotIn("agents", state)

    def test_seq_moves_when_anything_shown_changes(self):
        backend = self.backend()
        backend.state_key()
        seq = backend.state()["seq"]
        backend.state_key()
        self.assertEqual(backend.state()["seq"], seq)
        backend.zoo.on_hook({"event": "UserPromptSubmit", "session_id": "s", "project": "p"})
        backend.state_key()
        self.assertEqual(backend.state()["seq"], seq + 1)
        backend.client.key_rejected = True
        backend.state_key()
        self.assertEqual(backend.state()["seq"], seq + 2)

    def test_toggl_status_shows_backoff_and_offline(self):
        backend = self.backend()
        link = connect(backend)
        backend.runs.run_jobs()
        backend.on_message(start_msg())
        self.toggl.fail_next(429, {"Retry-After": "90"})
        drain(backend)
        self.assertEqual(backend.state()["toggl"], "backoff")
        self.clock.advance(90)
        self.toggl.down = True
        drain(backend)
        self.assertEqual(backend.state()["toggl"], "offline")
        self.toggl.down = False
        self.clock.advance(backend.client.next_try - self.clock())
        drain(backend)
        self.assertEqual(backend.state()["toggl"], "ok")
        self.assertEqual(link.of_kind("ack"), [{"t": "ack", "id": 17}])

    def test_a_state_stays_small_however_many_agents_there_are(self):
        backend = self.backend()
        for i in range(500):
            backend.zoo.on_hook({"event": "PreToolUse", "session_id": f"s{i}", "project": "プ" * 40, "activity": "編" * 48})
        msg = backend.state()
        self.assertEqual(msg["n"], 500)
        self.assertLess(len(encode(msg)), 200)


class InitialLists(Base):
    def test_both_lists_are_sent_once_after_connecting(self):
        backend = self.backend()
        self.toggl.entries[1] = {"id": 1, "description": "Task", "start": iso(T0), "project_id": 123, "task_id": 0}
        link = connect(backend)
        self.assertEqual(self.toggl.count(), 0)  # never on the link's thread
        backend.runs.run_jobs()
        self.assertEqual(self.toggl.count(), 3)  # settings, projects, entries
        self.assertEqual(link.of_kind("projects")[0]["rows"], [{"id": 123, "name": "Deep work", "color": "#4FB3F6"}])
        self.assertEqual(link.of_kind("projects")[0]["req"], 0)
        self.assertEqual(link.of_kind("tasks")[0]["rows"][0]["desc"], "Task")
        self.assertEqual(link.of_kind("tasks")[0]["rows"][0]["project"]["name"], "Deep work")
        disconnect(backend, link)
        connect(backend, link)
        backend.runs.run_jobs()
        self.assertEqual(self.toggl.count(), 3)  # not on a reconnect: that spends the free plan's requests on syncing
        self.assertEqual(len(link.of_kind("projects")), 1)

    def test_another_board_gets_its_own_even_of_the_same_model(self):
        backend = self.backend()
        link = connect(backend, uid=UID)
        backend.runs.run_jobs()
        disconnect(backend, link)
        connect(backend, link, uid=OTHER_UID)
        backend.runs.run_jobs()
        self.assertEqual([m["req"] for m in link.of_kind("projects")], [0, 0])
        self.assertEqual([m["req"] for m in link.of_kind("tasks")], [0, 0])
        for uid in (UID, OTHER_UID):  # and each only once
            disconnect(backend, link)
            connect(backend, link, uid=uid)
            backend.runs.run_jobs()
        self.assertEqual(len(link.of_kind("projects")), 2)

    def test_a_restarted_app_loads_them_once_again(self):
        first = self.backend()
        link = connect(first)
        first.runs.run_jobs()
        second = self.backend()
        connect(second, link)
        second.runs.run_jobs()
        self.assertEqual([m["req"] for m in link.of_kind("projects")], [0, 0])
        self.assertEqual([m["req"] for m in link.of_kind("tasks")], [0, 0])

    def test_a_failed_load_is_tried_again_only_at_the_next_connection(self):
        backend = self.backend()
        self.toggl.down = True
        link = connect(backend)
        backend.runs.run_jobs()
        self.assertEqual(self.toggl.count(), 1)  # no more requests once one fails
        self.assertEqual(link.of_kind("fetch_failed"), [])  # no fetch asked: nobody waits for an answer
        self.clock.advance(3600)
        backend.runs.run_jobs()
        backend.runs.pump()
        self.assertEqual(self.toggl.count(), 1)
        self.toggl.down = False
        disconnect(backend, link)
        connect(backend, link)
        backend.runs.run_jobs()
        self.assertEqual(len(link.of_kind("projects")), 1)
        self.assertEqual(len(link.of_kind("tasks")), 1)

    def test_a_list_the_board_got_by_fetch_isnt_sent_again(self):
        backend = self.backend()
        backend.client.rate_until = self.clock() + 100  # so no load at this connection
        link = connect(backend)
        backend.runs.run_jobs()
        backend.client.rate_until = 0
        backend.on_message({"t": "fetch", "req": 5, "what": "projects"})
        backend.runs.run_jobs()
        disconnect(backend, link)
        connect(backend, link)
        backend.runs.run_jobs()
        self.assertEqual([m["req"] for m in link.of_kind("projects")], [5])
        self.assertEqual([m["req"] for m in link.of_kind("tasks")], [0])

    def test_no_load_during_a_rate_limit(self):
        backend = self.backend()
        backend.client.rate_until = self.clock() + 100
        connect(backend)
        backend.runs.run_jobs()
        self.assertEqual(self.toggl.count(), 0)


class Fetch(Base):
    def setUp(self):
        super().setUp()
        self.b = self.backend()
        self.link = connect(self.b)
        self.b.runs.run_jobs()
        self.toggl.clear()
        self.link.sent.clear()

    def test_one_request_and_the_answer_pages_stay_under_the_limit(self):
        self.toggl.projects = [{"id": i + 1, "name": f"{i:03d} " + "プ" * 200, "color": "#123456"} for i in range(99)]
        self.b.on_message({"t": "fetch", "req": 5, "what": "projects"})
        self.b.runs.run_jobs()
        self.assertEqual(self.toggl.count(), 1)
        pages = self.link.of_kind("projects")
        self.assertGreater(len(pages), 1)
        self.assertEqual([p["page"] for p in pages], list(range(len(pages))))
        self.assertEqual({p["pages"] for p in pages}, {len(pages)})
        self.assertEqual({p["req"] for p in pages}, {5})
        self.assertTrue(all(len(encode(p)) < LINE_MAX for p in pages))
        self.assertEqual(sum(len(p["rows"]) for p in pages), 99)

    def test_a_failure_says_which_list(self):
        self.toggl.fail_next(500)
        self.b.on_message({"t": "fetch", "req": 6, "what": "tasks"})
        self.b.runs.run_jobs()
        self.assertEqual(self.link.of_kind("fetch_failed"), [{"t": "fetch_failed", "req": 6, "what": "tasks"}])

    def test_after_429_it_answers_at_once_with_no_request(self):
        self.toggl.fail_next(429, {"Retry-After": "300"})
        self.b.on_message({"t": "fetch", "req": 6, "what": "tasks"})
        self.b.runs.run_jobs()
        self.toggl.clear()
        self.b.on_message({"t": "fetch", "req": 7, "what": "projects"})
        self.b.on_message({"t": "sync", "req": 8, "rid": 0})
        self.b.runs.run_jobs()
        self.assertEqual(self.toggl.count(), 0)
        self.assertEqual(self.link.of_kind("fetch_failed")[-1]["req"], 7)
        self.assertEqual(self.link.of_kind("sync_result"), [{"t": "sync_result", "req": 8, "result": "failed"}])

    def test_an_unknown_list_makes_no_request(self):
        self.b.on_message({"t": "fetch", "req": 6, "what": "cats"})
        self.b.runs.run_jobs()
        self.assertEqual(self.toggl.count(), 0)
        self.assertEqual(self.link.of_kind("fetch_failed")[0]["req"], 6)

    def test_a_fetch_with_no_req_is_ignored(self):
        self.b.on_message({"t": "fetch", "what": "tasks"})
        self.b.runs.run_jobs()
        self.assertEqual(self.link.sent, [])

    def test_an_answer_to_a_board_that_left_is_dropped(self):
        self.b.on_message({"t": "fetch", "req": 5, "what": "projects"})
        disconnect(self.b, self.link)
        connect(self.b, self.link, name="knob-lcd-1.8")
        self.link.sent.clear()
        self.b.runs.run_jobs()
        self.assertEqual(self.link.of_kind("projects"), [])


class EntryStatus(Base):
    def rejected(self, backend):
        backend.client.wid, backend.client.oid = WID, ORG
        self.toggl.fail_next(422)

    def test_a_rejection_goes_out_at_once_and_only_once(self):
        backend = self.backend()
        link = connect(backend)
        backend.runs.run_jobs()
        self.rejected(backend)
        backend.on_message(start_msg())
        drain(backend)
        self.assertEqual(link.of_kind("entry_status"), [{"t": "entry_status", "rid": 812, "status": "rejected"}])
        disconnect(backend, link)
        connect(backend, link)
        backend.runs.run_jobs()
        self.assertEqual(len(link.of_kind("entry_status")), 1)

    def test_a_rejection_with_the_board_away_waits_for_it(self):
        backend = self.backend()
        link = connect(backend)
        backend.runs.run_jobs()
        backend.on_message(start_msg())
        disconnect(backend, link)
        self.rejected(backend)
        drain(backend)
        self.assertEqual(link.of_kind("entry_status"), [])
        backend = self.backend()  # also over a restart
        connect(backend, link)
        self.assertEqual(link.of_kind("entry_status"), [{"t": "entry_status", "rid": 812, "status": "rejected"}])
        self.assertIn(link.of_kind("entry_status")[0], link.reliable)

    def test_it_goes_to_the_board_it_belongs_to_not_one_of_the_same_model(self):
        backend = self.backend()
        link = connect(backend, uid=UID)
        backend.runs.run_jobs()
        backend.on_message(start_msg())
        disconnect(backend, link)
        self.rejected(backend)
        drain(backend)
        connect(backend, link, uid=OTHER_UID)
        self.assertEqual(link.of_kind("entry_status"), [])
        disconnect(backend, link)
        connect(backend, link, uid=UID)
        self.assertEqual(link.of_kind("entry_status"), [{"t": "entry_status", "rid": 812, "status": "rejected"}])

    def test_success_sends_nothing(self):
        backend = self.backend()
        link = connect(backend)
        backend.runs.run_jobs()
        backend.on_message(start_msg())
        drain(backend)
        backend.on_message({"t": "toggl", "id": 18, "op": "stop", "rid": 812, "at": T0 + 100})
        drain(backend)
        self.assertEqual(link.of_kind("entry_status"), [])


class Oddities(Base):
    def test_messages_with_odd_fields_do_no_harm(self):
        backend = self.backend()
        link = connect(backend)
        for msg in ({"t": "unknown"}, {"t": "toggl"}, {"t": "toggl", "id": 1}, {"t": "toggl", "id": 2, "op": "start"},
                    {"t": "toggl", "id": 3, "op": "start", "rid": "x"}, {"t": "toggl", "id": 4, "op": "update", "rid": 1, "desc": 7},
                    {"t": "sync"}, {"t": "sync", "req": "x"}, {"t": "fetch", "req": 1}, {"t": "tap"}, {"t": "tap", "id": 7},
                    {"t": "voice_start"}, {"t": "audio", "vid": "x"}, {"t": "voice_end", "vid": 1}, {"t": "voice_cancel"},
                    {"t": "board"}, {}):
            backend.on_message(msg)
        drain(backend)
        self.assertEqual(backend.runs.queued(), 0)
        self.assertIn({"t": "ack", "id": 4}, link.sent)

    def test_a_tap_runs_the_callback_off_the_links_thread(self):
        taps = []
        backend = self.backend()
        backend.on_tap = lambda agent, source: taps.append((agent, source))
        connect(backend)
        backend.on_message({"t": "tap", "id": "a1b2c3d4"})
        deadline = time.monotonic() + 3
        while not taps and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(taps, [("a1b2c3d4", "board")])

    def test_an_update_settles_through_the_backend_too(self):
        backend = self.backend()
        link = connect(backend)
        backend.runs.run_jobs()
        backend.on_message(start_msg())
        drain(backend)
        backend.on_message({"t": "toggl", "id": 18, "op": "update", "rid": 812, "desc": "Acme review", "task_id": 0, "project": 123})
        self.toggl.clear()
        drain(backend)
        self.assertEqual(self.toggl.count(), 0)
        self.clock.advance(SETTLE_S)
        drain(backend)
        self.assertEqual(self.toggl.reqs("PATCH")[0].body, {"description": "Acme review"})
        self.assertEqual([m["id"] for m in link.of_kind("ack")], [17, 18])


class KeyCache(Base):
    def test_a_key_is_read_once(self):
        calls = []

        class Counting(MemoryStore):
            def get(self, name):
                calls.append(name)
                return super().get(name)
        backend = self.backend(store=Counting(toggl=KEY))
        connect(backend)
        backend.runs.run_jobs()
        self.assertEqual(calls.count("toggl"), 1)
        backend.reload()
        backend.client.get_key()
        self.assertEqual(calls.count("toggl"), 2)

    def test_a_missing_key_is_never_read_and_is_looked_for_again(self):
        calls = []

        class Counting(MemoryStore):
            def get(self, name):
                calls.append(name)
                return super().get(name)
        store = Counting()
        backend = self.backend(store=store)
        self.assertFalse(backend.keys.has("toggl"))
        self.assertIsNone(backend.keys.get("toggl"))
        self.assertEqual(calls, [])
        store.set("toggl", KEY)
        backend.keys.checked = -10000.0
        backend.tick()
        self.assertTrue(backend.keys.has("toggl"))


if __name__ == "__main__":
    unittest.main()
