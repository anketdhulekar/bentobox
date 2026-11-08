"""The Toggl client and the run manager, against a fake Toggl on this Mac."""
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import helpers
from helpers import KEY, ORG, T0, WID, Clock, FakeToggl, iso

from link.framing import LINE_MAX, encode, paginate
from toggl import client as client_mod
from toggl import lists, runs as runs_mod
from toggl.client import TogglClient, parse_iso, parse_retry_after, scan_org
from toggl.runs import SETTLE_S, RunManager

BOARD = helpers.UID


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bentobox-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.toggl = FakeToggl()
        self.addCleanup(self.toggl.close)
        self.clock = Clock()
        self.key = KEY
        self.org = ORG
        self.path = os.path.join(self.dir, "toggl.json")
        self.next_id = 0
        self.runs = self.make_runs()

    def make_runs(self):
        self.client = TogglClient(self.toggl.url, lambda: self.key, lambda: self.org, clock=self.clock)
        self.rejected = []
        runs = RunManager(self.path, self.client, has_key=lambda: bool(self.key), clock=self.clock)
        runs.on_outbox = lambda: self.rejected.extend(i["rid"] for i in runs.outbox_for(BOARD))
        return runs

    def send(self, op, rid=812, msg_id=None, uid=BOARD, **fields):
        if msg_id is None:
            self.next_id += 1
            msg_id = self.next_id
        return self.runs.accept(uid, {"t": "toggl", "id": msg_id, "op": op, "rid": rid, **fields})

    def start(self, rid=812, at=None, desc="Write the Q4 plan", task_id=0, project=123, **kw):
        return self.send("start", rid, at=at if at is not None else int(self.clock()), desc=desc, task_id=task_id,
                         project=project, **kw)

    def started(self, rid=812, **kw):
        """A run that has reached Toggl: returns its entry."""
        self.start(rid, **kw)
        self.runs.pump()
        entry = self.toggl.entries[self.toggl.running]
        self.toggl.clear()
        return entry

    def calls(self):
        return [(r.method, r.path.split(f"/workspaces/{WID}/")[-1].split("?")[0]) for r in self.toggl.reqs()]


class Start(Base):
    def test_the_first_call_finds_the_workspace_then_starts(self):
        self.assertTrue(self.start(at=T0, project=123))
        self.assertEqual(self.toggl.count(), 0)  # accepting a message makes no request
        self.runs.pump()
        self.assertEqual(self.calls(), [("GET", "/api/users/me/settings"), ("POST", "tracking/start")])
        body = self.toggl.reqs("POST", "tracking/start")[0].body
        self.assertEqual(body, {"type": "activity", "description": "Write the Q4 plan", "start": iso(T0), "project_id": 123})
        self.assertEqual(self.toggl.reqs()[0].auth, f"Bearer {KEY}")

    def test_no_project_and_a_task(self):
        self.start(project=0, task_id=55)
        self.runs.pump()
        body = self.toggl.reqs("POST", "tracking/start")[0].body
        self.assertNotIn("project_id", body)
        self.assertEqual(body["task_id"], 55)

    def test_a_start_turned_down_with_nothing_running_is_rejected(self):
        self.runs.pump()
        self.client.wid, self.client.oid = WID, ORG
        self.toggl.fail_next(422, body={"error": "archived project"})
        self.start()
        self.runs.pump()
        self.assertEqual(self.rejected, [812])
        self.assertEqual(self.runs.queued(), 0)
        self.assertEqual(self.calls(), [("POST", "tracking/start"), ("GET", "tracking/current")])
        self.assertTrue(self.send("stop", at=T0 + 60))  # a later stop for it is simply handled
        self.runs.pump()
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/stop")), 0)

    def test_a_new_start_stops_the_earlier_open_run_first(self):
        first = self.started(rid=812, at=T0)
        self.clock.advance(100)
        self.start(rid=813, at=T0 + 100, desc="Next")
        self.runs.pump()
        self.assertEqual(self.calls(), [("GET", "tracking/current"), ("POST", "tracking/stop"), ("POST", "tracking/start")])
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop")[0].body, {"end": iso(T0 + 100)})
        self.assertEqual(first["end"], iso(T0 + 100))
        self.assertEqual(self.toggl.entries[self.toggl.running]["description"], "Next")

    def test_a_new_start_within_10_s_deletes_the_earlier_run(self):
        first = self.started(rid=812, at=T0)
        self.clock.advance(4)
        self.start(rid=813, at=T0 + 4, desc="Next")
        self.runs.pump()
        self.assertNotIn(first["id"], self.toggl.entries)
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/stop")), 0)


class Updates(Base):
    def test_the_settle_time_is_the_protocols(self):
        self.assertEqual(SETTLE_S, 1.5)

    def test_changes_coalesce_into_one_patch_with_only_what_changed(self):
        entry = self.started(at=T0, project=123, desc="Old")
        self.clock.advance(100)
        self.send("update", desc="Acme")
        self.clock.advance(0.5)
        self.send("update", desc="Acme review", task_id=0, project=123)  # task and project are the same as before
        self.clock.advance(0.5)
        self.send("shift", start=T0 - 60)
        self.clock.advance(1.0)
        self.send("shift", start=T0 - 120)
        self.runs.pump()
        self.assertEqual(self.toggl.count(), 0)  # still settling
        self.clock.advance(SETTLE_S - 0.1)
        self.assertLess(self.runs.pump(), 0.2)
        self.assertEqual(self.toggl.count(), 0)
        self.clock.advance(0.2)
        self.runs.pump()
        patches = self.toggl.reqs("PATCH")
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0].body, {"description": "Acme review", "start": iso(T0 - 120)})
        self.assertIn(f"time-entries/{entry['id']}", patches[0].path)
        self.runs.pump()
        self.assertEqual(self.toggl.count(), 1)

    def test_a_shift_alone_sends_only_the_start(self):
        self.started(at=T0)
        self.send("shift", start=T0 - 60)
        self.clock.advance(SETTLE_S)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("PATCH")[0].body, {"start": iso(T0 - 60)})

    def test_project_zero_clears_the_project_and_task_zero_the_task(self):
        self.started(at=T0, project=123, task_id=55)
        self.send("update", project=0, task_id=0)
        self.clock.advance(SETTLE_S)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("PATCH")[0].body, {"project_id": None, "task_id": None})

    def test_a_change_undone_before_it_settled_sends_nothing(self):
        self.started(desc="A")
        self.send("update", desc="B")
        self.send("update", desc="A")
        self.clock.advance(SETTLE_S)
        self.runs.pump()
        self.assertEqual(self.toggl.count(), 0)

    def test_a_change_before_the_entry_exists_goes_in_its_start(self):
        self.start(at=T0, desc="Old")
        self.send("update", desc="New")
        self.send("shift", start=T0 - 30)
        self.runs.pump()
        body = self.toggl.reqs("POST", "tracking/start")[0].body
        self.assertEqual((body["description"], body["start"]), ("New", iso(T0 - 30)))
        self.assertEqual(self.toggl.reqs("PATCH"), [])

    def test_a_stop_flushes_changes_at_once_then_stops(self):
        self.started(at=T0, desc="Old")
        self.clock.advance(100)
        self.send("update", desc="New")
        self.send("stop", at=T0 + 100)
        self.runs.pump()
        self.assertEqual(self.calls(), [("PATCH", f"time-entries/{self.toggl.next_id}"), ("GET", "tracking/current"), ("POST", "tracking/stop")])

    def test_a_change_during_a_request_is_not_lost(self):
        self.started(at=T0, desc="A")
        self.send("update", desc="B")
        self.clock.advance(SETTLE_S)
        real = self.client.patch_entry

        def during(entry_id, body):
            reply = real(entry_id, body)
            self.send("update", desc="C")  # arrives while the PATCH for B is in flight
            return reply
        with mock.patch.object(self.client, "patch_entry", during):
            self.runs.pump()
        self.clock.advance(SETTLE_S)
        self.runs.pump()
        self.assertEqual([r.body["description"] for r in self.toggl.reqs("PATCH")], ["B", "C"])

    def test_a_patch_turned_down_is_reported_once_and_the_run_goes_on(self):
        self.started(at=T0, desc="A")
        self.send("update", desc="B")
        self.clock.advance(SETTLE_S)
        self.toggl.fail_next(400)
        self.runs.pump()
        self.runs.pump()
        self.assertEqual(self.rejected, [812])
        self.assertEqual(len(self.toggl.reqs("PATCH")), 1)
        self.assertEqual(self.runs.queued(), 1)

    def test_an_update_for_an_unknown_run_is_handled(self):
        self.assertTrue(self.send("update", rid=999, desc="x"))
        self.assertTrue(self.send("shift", rid=999, start=T0))
        self.assertTrue(self.send("stop", rid=999, at=T0))
        self.assertEqual(self.runs.queued(), 0)


class Stop(Base):
    def test_a_long_run_is_stopped_after_checking_it_is_ours(self):
        self.started(at=T0)
        self.clock.advance(600)
        self.send("stop", at=T0 + 600)
        self.runs.pump()
        self.assertEqual(self.calls(), [("GET", "tracking/current"), ("POST", "tracking/stop")])
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop")[0].body, {"end": iso(T0 + 600)})
        self.assertEqual(self.toggl.running, 0)
        self.assertEqual(self.runs.queued(), 0)

    def test_the_answer_may_come_wrapped_in_data(self):
        self.toggl.wrap_current = True
        self.started(at=T0)
        self.send("stop", at=T0 + 600)
        self.runs.pump()
        self.assertEqual(self.toggl.running, 0)

    def test_a_run_under_10_s_is_deleted_not_stopped(self):
        entry = self.started(at=T0)
        self.send("stop", at=T0 + 9)
        self.runs.pump()
        self.assertNotIn(entry["id"], self.toggl.entries)
        self.assertEqual(self.calls(), [("DELETE", f"time-entries/{entry['id']}")])
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])

    def test_a_run_of_exactly_10_s_stays(self):
        entry = self.started(at=T0)
        self.send("stop", at=T0 + 10)
        self.runs.pump()
        self.assertIn(entry["id"], self.toggl.entries)
        self.assertEqual(len(self.toggl.reqs("DELETE")), 0)

    def test_a_shift_counts_towards_the_10_s(self):
        entry = self.started(at=T0)
        self.send("shift", start=T0 - 60)
        self.send("stop", at=T0 + 3)
        self.runs.pump()
        self.assertIn(entry["id"], self.toggl.entries)

    def test_a_short_run_that_never_reached_toggl_costs_no_request(self):
        self.start(at=T0)
        self.send("stop", at=T0 + 3)
        self.runs.pump()
        self.assertEqual(self.toggl.count(), 0)
        self.assertEqual(self.runs.queued(), 0)

    def test_if_toggl_wont_delete_a_running_entry_it_is_stopped_first(self):
        self.toggl.refuse_delete_running = True
        entry = self.started(at=T0)
        self.send("stop", at=T0 + 3)
        self.runs.pump()
        self.assertEqual(self.calls(), [("DELETE", f"time-entries/{entry['id']}"), ("GET", "tracking/current"),
                                        ("POST", "tracking/stop"), ("DELETE", f"time-entries/{entry['id']}")])
        self.assertNotIn(entry["id"], self.toggl.entries)

    def test_a_deleted_entry_is_fine(self):
        entry = self.started(at=T0)
        del self.toggl.entries[entry["id"]]
        self.toggl.running = 0
        self.send("stop", at=T0 + 3)
        self.runs.pump()
        self.assertEqual((self.runs.queued(), self.rejected), (0, []))


class ForeignTimer(Base):
    def test_a_stop_never_ends_a_timer_that_isnt_ours(self):
        self.started(at=T0)
        foreign = self.toggl.foreign_timer()
        self.send("stop", at=T0 + 600)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])
        self.assertEqual(self.toggl.running, foreign["id"])
        self.assertEqual(self.runs.queued(), 0)

    def test_stopped_elsewhere_is_fine(self):
        self.started(at=T0)
        self.toggl.stop_elsewhere()
        self.send("stop", at=T0 + 600)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])
        self.assertEqual((self.runs.queued(), self.rejected), (0, []))

    def test_the_delete_fallback_never_stops_a_foreign_timer(self):
        entry = self.started(at=T0)
        foreign = self.toggl.foreign_timer()
        self.toggl.fail_next(400)  # Toggl refuses the first delete, which is what leads to the check and the stop
        self.send("stop", at=T0 + 3)
        self.runs.pump()
        self.assertEqual(self.calls(), [("DELETE", f"time-entries/{entry['id']}"), ("GET", "tracking/current"),
                                        ("DELETE", f"time-entries/{entry['id']}")])
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])
        self.assertEqual(self.toggl.running, foreign["id"])
        self.assertNotIn(entry["id"], self.toggl.entries)

    def test_a_start_refused_because_of_a_foreign_timer_leaves_it_running(self):
        foreign = self.toggl.foreign_timer()
        self.start(at=T0, desc="Mine", project=0)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])
        self.assertEqual(self.toggl.running, foreign["id"])
        self.assertEqual(self.rejected, [])
        # It waits, spending no more requests...
        self.toggl.clear()
        self.clock.advance(3600)
        self.runs.pump()
        self.send("update", desc="Mine, renamed")
        self.runs.pump()
        self.assertEqual(self.toggl.count(), 0)
        # ...and when it stops, it's logged whole, with the timer still running.
        self.send("stop", at=T0 + 600)
        self.runs.pump()
        self.assertEqual(self.calls(), [("POST", "time-entries")])
        body = self.toggl.reqs("POST", "time-entries")[0].body
        self.assertEqual((body["description"], body["start"], body["duration"]), ("Mine, renamed", iso(T0), 600))
        self.assertEqual(self.toggl.running, foreign["id"])
        self.assertEqual(self.runs.queued(), 0)

    def test_a_held_short_run_leaves_nothing(self):
        self.toggl.foreign_timer()
        self.start(at=T0)
        self.runs.pump()
        self.toggl.clear()
        self.send("stop", at=T0 + 5)
        self.runs.pump()
        self.assertEqual(self.toggl.count(), 0)
        self.assertEqual(self.runs.queued(), 0)


class Sync(Base):
    def sync(self, req=7, rid=812):
        return self.runs.sync(BOARD, req, rid)

    def test_in_sync(self):
        self.started(at=T0)
        self.assertEqual(self.sync(), {"t": "sync_result", "req": 7, "result": "in_sync"})

    def test_in_sync_when_nothing_runs_anywhere(self):
        self.assertEqual(self.sync(rid=0)["result"], "in_sync")

    def test_the_boards_timer_stopped_elsewhere(self):
        self.started(at=T0)
        self.toggl.stop_elsewhere()
        self.assertEqual(self.sync(), {"t": "sync_result", "req": 7, "result": "stopped"})
        self.assertEqual(self.runs.queued(), 0)
        self.assertEqual(self.sync()["result"], "stopped")  # a timer the app doesn't know, with nothing in Toggl

    def test_a_queued_start_goes_out_first(self):
        self.start(at=T0)
        self.assertEqual(self.sync()["result"], "in_sync")
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/start")), 1)

    def test_a_timer_started_elsewhere_is_adopted(self):
        self.runs.projects = [{"id": 123, "name": "Deep work", "color": "#4FB3F6"}]
        foreign = self.toggl.foreign_timer("From my phone", start=T0 - 1000, project_id=123, task_id=9)
        self.assertEqual(self.sync(rid=0), {
            "t": "sync_result", "req": 7, "result": "adopted", "rid": 1, "desc": "From my phone", "task_id": 9,
            "project": {"id": 123, "name": "Deep work", "color": "#4FB3F6"}, "start": T0 - 1000})
        self.assertEqual(self.toggl.running, foreign["id"])

    def test_the_adopted_rid_is_one_above_any_the_board_used(self):
        self.started(rid=812, at=T0)
        self.toggl.stop_elsewhere()
        self.toggl.foreign_timer()
        result = self.sync(rid=812)
        self.assertEqual((result["result"], result["rid"], result["project"]), ("adopted", 813, None))
        self.assertEqual(self.runs.queued(), 1)  # the board's own run is gone

    def test_the_adopted_rid_counts_only_this_uids_numbers(self):
        self.send("update", rid=9000, uid=helpers.OTHER_UID)
        self.toggl.foreign_timer()
        self.assertEqual(self.runs.sync(BOARD, 7, 0)["rid"], 1)
        self.assertEqual(self.runs.data["high_rid"], {helpers.OTHER_UID: 9000, BOARD: 1})

    def test_each_adoption_moves_the_uids_counter_on(self):
        self.send("update", rid=812, desc="x")
        self.toggl.foreign_timer()
        self.assertEqual(self.sync(rid=0)["rid"], 813)
        self.toggl.stop_elsewhere()
        self.toggl.foreign_timer(desc="Another phone timer")
        self.assertEqual(self.sync(rid=813)["rid"], 814)

    def test_a_uid_seen_only_through_the_sync_counts_that_rid(self):
        self.toggl.foreign_timer()
        self.assertEqual(self.sync(rid=500)["rid"], 501)

    def test_an_adopted_run_under_10_s_is_not_deleted(self):
        foreign = self.toggl.foreign_timer(start=int(self.clock()) - 2)
        rid = self.sync(rid=0)["rid"]
        self.toggl.clear()
        self.send("stop", rid=rid, at=int(self.clock()))
        self.runs.pump()
        self.assertEqual(self.calls(), [("GET", "tracking/current"), ("POST", "tracking/stop")])
        self.assertIn(foreign["id"], self.toggl.entries)

    def test_an_adopted_run_can_be_changed_and_stopped(self):
        foreign = self.toggl.foreign_timer(desc="Phone", start=T0 - 1000)
        rid = self.sync(rid=0)["rid"]
        self.toggl.clear()
        self.send("update", rid=rid, desc="Phone", project=0)  # the same as Toggl has
        self.send("update", rid=rid, desc="Renamed")
        self.clock.advance(SETTLE_S)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("PATCH")[0].body, {"description": "Renamed"})
        self.send("stop", rid=rid, at=T0 + 50)
        self.runs.pump()
        self.assertEqual(self.toggl.entries[foreign["id"]]["end"], iso(T0 + 50))

    def test_the_old_rid_is_forgotten_after_adopting(self):
        self.started(rid=812, at=T0)
        self.toggl.stop_elsewhere()
        self.toggl.foreign_timer()
        self.sync(rid=812)
        self.toggl.clear()
        self.send("stop", rid=812, at=T0 + 50)
        self.runs.pump()
        self.assertEqual(self.toggl.count(), 0)

    def test_a_run_waiting_behind_a_foreign_timer_gives_way_to_it(self):
        self.toggl.foreign_timer()
        self.start(at=T0)
        self.runs.pump()
        result = self.sync(rid=812)
        self.assertEqual((result["result"], result["rid"]), ("adopted", 813))
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])

    def test_a_run_waiting_behind_a_timer_that_ended_starts_on_sync(self):
        self.toggl.foreign_timer()
        self.start(at=T0)
        self.runs.pump()
        self.toggl.stop_elsewhere()
        self.assertEqual(self.sync(rid=812)["result"], "in_sync")
        self.assertEqual(self.toggl.entries[self.toggl.running]["description"], "Write the Q4 plan")

    def test_if_the_board_lost_track_of_its_own_entry_it_takes_it_back(self):
        entry = self.started(rid=812, at=T0)
        result = self.sync(rid=0)
        self.assertEqual((result["result"], result["rid"]), ("adopted", 813))
        self.assertEqual(self.runs.queued(), 1)
        self.toggl.clear()
        self.send("stop", rid=813, at=T0 + 2)  # adopted now: not deleted
        self.runs.pump()
        self.assertIn(entry["id"], self.toggl.entries)

    def test_failed_when_toggl_is_out_of_reach(self):
        self.started(at=T0)
        self.toggl.down = True
        self.assertEqual(self.sync(), {"t": "sync_result", "req": 7, "result": "failed"})
        self.assertEqual(self.runs.queued(), 1)  # nothing is forgotten

    def test_failed_with_no_key(self):
        self.key = None
        self.assertEqual(self.sync()["result"], "failed")
        self.assertEqual(self.toggl.count(), 0)

    def test_an_offline_backoff_doesnt_stop_a_person_from_trying(self):
        self.start(at=T0)
        self.toggl.down = True
        self.runs.pump()
        self.toggl.down = False
        self.assertEqual(self.sync()["result"], "in_sync")
        self.assertEqual(self.runs.status(), "ok")


class Backoff(Base):
    def test_429_with_retry_after_holds_everything_until_then(self):
        self.runs.pump()
        self.client.wid, self.client.oid = WID, ORG
        self.start(at=T0)
        self.toggl.fail_next(429, {"Retry-After": "120"})
        self.assertEqual(self.runs.pump(), 120)
        self.assertEqual(self.runs.status(), "backoff")
        self.assertEqual(self.toggl.count(), 1)
        self.clock.advance(119)
        self.assertEqual(self.runs.pump(), 1)
        self.assertEqual(self.runs.sync(BOARD, 7, 812)["result"], "failed")
        self.assertIsNone(self.runs.load_list("projects"))
        self.assertIsNone(self.runs.load_list("tasks"))
        self.assertEqual(self.toggl.count(), 1)  # no request for any of those
        self.clock.advance(1)
        self.runs.pump()
        self.assertEqual(self.runs.status(), "ok")
        self.assertEqual(self.toggl.count(), 3)  # the check after a try that may have worked, then the start
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/start")), 2)

    def test_429_with_a_date_or_no_header_uses_the_backoff(self):
        self.runs.pump()
        self.client.wid, self.client.oid = WID, ORG
        self.start(at=T0)
        self.toggl.fail_next(429)
        self.assertEqual(self.runs.pump(), client_mod.BACKOFF_MIN_S)
        self.assertEqual(self.runs.status(), "backoff")

    def test_a_short_retry_after_doesnt_shorten_the_backoff(self):
        self.client.backoff_s = 60
        self.client.wid, self.client.oid = WID, ORG
        self.start(at=T0)
        self.toggl.fail_next(429, {"Retry-After": "2"})
        self.assertEqual(self.runs.pump(), 60)

    def test_retry_after_parsing(self):
        self.assertEqual(parse_retry_after("30"), 30)
        self.assertEqual(parse_retry_after(None), 0)
        self.assertEqual(parse_retry_after("soon"), 0)
        self.assertEqual(parse_retry_after("99999999"), client_mod.RETRY_AFTER_MAX_S)
        self.assertEqual(parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT"), 0)  # already past

    def test_a_429_on_a_list_needs_no_queue_to_hold(self):
        self.client.wid, self.client.oid = WID, ORG
        self.toggl.fail_next(429, {"Retry-After": "50"})
        self.assertIsNone(self.runs.load_list("projects"))
        self.assertEqual(self.runs.status(), "backoff")
        self.assertIsNone(self.runs.pump())  # nothing to wake up for
        self.clock.advance(50)
        self.assertEqual(self.runs.status(), "ok")


class Offline(Base):
    def test_a_start_is_retried_with_a_growing_backoff(self):
        self.toggl.down = True
        self.start(at=T0)
        seen = []
        for _ in range(4):
            before = self.toggl.count()
            delay = self.runs.pump()
            seen.append(delay)
            self.assertEqual(self.runs.status(), "offline")
            self.assertEqual(self.toggl.count(), before + 1)
            self.clock.advance(delay - 0.5)
            self.runs.pump()
            self.assertEqual(self.toggl.count(), before + 1)  # not before it's time
            self.clock.advance(0.5)
        self.assertEqual(seen, [5, 10, 20, 40])
        self.toggl.down = False
        self.runs.pump()
        self.assertEqual(self.runs.status(), "ok")
        self.assertEqual(self.client.backoff_s, client_mod.BACKOFF_MIN_S)
        self.assertEqual(self.toggl.entries[self.toggl.running]["start"], iso(T0))

    def test_the_backoff_tops_out(self):
        self.toggl.down = True
        self.start(at=T0)
        for _ in range(12):
            self.clock.advance(self.runs.pump())
        self.assertEqual(self.client.backoff_s, client_mod.BACKOFF_MAX_S)

    def test_a_good_get_doesnt_reset_the_backoff_when_the_next_step_fails(self):
        self.started(at=T0)
        self.send("stop", at=T0 + 100)
        delays = []
        for _ in range(3):
            self.toggl.script.append((200, {}, self.toggl.entries[self.toggl.running]))  # current works...
            self.toggl.fail_next(500)                                                     # ...stop doesn't
            delay = self.runs.pump()
            delays.append(delay)
            self.clock.advance(delay)
        self.assertEqual(delays, [5, 10, 20])

    def test_a_list_that_loads_doesnt_shorten_the_backoff_of_a_failing_queue(self):
        self.client.wid, self.client.oid = WID, ORG
        self.start(at=T0)
        self.toggl.fail_next(500)
        self.assertEqual(self.runs.pump(), 5)
        self.assertEqual(self.client.backoff_s, 10)
        self.assertIsNotNone(self.runs.load_list("tasks"))  # a person asked, and Toggl answered
        self.assertEqual(self.client.next_try, 0)           # so the queue may try again now...
        self.assertEqual(self.client.backoff_s, 10)         # ...but if it fails again, the delay keeps growing
        self.toggl.script.append((200, {}, None))           # the check that a start may have worked: nothing runs
        self.toggl.fail_next(500)
        self.assertEqual(self.runs.pump(), 10)

    def test_a_run_that_ended_while_offline_is_logged_whole_in_order(self):
        self.client.wid, self.client.oid = WID, ORG
        self.toggl.down = True
        self.start(rid=1, at=T0, desc="One", project=123)
        self.send("stop", rid=1, at=T0 + 300)
        self.start(rid=2, at=T0 + 400, desc="Two", project=0, task_id=55)
        self.send("stop", rid=2, at=T0 + 500)
        self.start(rid=3, at=T0 + 600, desc="Three", project=0)
        self.clock.advance(self.runs.pump())
        self.toggl.down = False
        self.toggl.clear()
        self.runs.pump()
        self.assertEqual(self.calls(), [("POST", "time-entries"), ("POST", "tasks/55/time-entries"), ("POST", "tracking/start")])
        one, two, three = [r.body for r in self.toggl.reqs("POST")]
        self.assertEqual(one, {"type": "activity", "description": "One", "start": iso(T0), "project_id": 123, "duration": 300})
        self.assertEqual((two["description"], two["duration"]), ("Two", 100))
        self.assertEqual(three["start"], iso(T0 + 600))

    def test_a_server_error_counts_as_offline(self):
        self.client.wid, self.client.oid = WID, ORG
        self.start(at=T0)
        self.toggl.fail_next(503)
        self.runs.pump()
        self.assertEqual(self.runs.status(), "offline")

    def test_offline_from_a_list_lapses_with_no_work_waiting(self):
        self.client.wid, self.client.oid = WID, ORG
        self.toggl.down = True
        self.assertIsNone(self.runs.load_list("tasks"))
        self.assertEqual(self.runs.status(), "offline")
        self.clock.advance(client_mod.BACKOFF_MIN_S)
        self.assertEqual(self.runs.status(), "ok")  # nothing has tried since: it isn't known to be down

    def test_offline_stays_during_the_retry(self):
        self.toggl.down = True
        self.start(at=T0)
        self.clock.advance(self.runs.pump())
        self.assertEqual(self.runs.status(), "offline")  # the moment the retry is due, before its answer

    def test_a_lost_answer_doesnt_start_the_run_twice(self):
        self.client.wid, self.client.oid = WID, ORG
        self.toggl.drop_after_start = True
        self.start(at=T0)
        self.clock.advance(self.runs.pump())
        self.assertEqual(self.runs.status(), "offline")
        self.runs.pump()
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/start")), 1)
        self.assertEqual(len(self.toggl.entries), 1)
        self.send("stop", at=T0 + 100)
        self.runs.pump()
        self.assertEqual(self.toggl.running, 0)


class CantTell(Base):
    def test_a_refused_check_leaves_the_run_waiting_and_tries_again_slowly(self):
        self.started(at=T0)
        self.send("stop", at=T0 + 600)
        self.toggl.fail_next(403)
        self.assertEqual(self.runs.pump(), client_mod.BACKOFF_MIN_S)  # not None: it won't wait forever
        self.assertEqual(self.runs.queued(), 1)
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])  # and it never stops blindly
        self.assertEqual(self.rejected, [])
        self.clock.advance(client_mod.BACKOFF_MIN_S)
        self.runs.pump()
        self.assertEqual(self.toggl.running, 0)
        self.assertEqual(self.runs.queued(), 0)

    def test_refused_settings_try_again_slowly_too(self):
        self.start(at=T0)
        self.toggl.fail_next(403)
        self.assertEqual(self.runs.pump(), client_mod.BACKOFF_MIN_S)
        self.clock.advance(client_mod.BACKOFF_MIN_S)
        self.runs.pump()
        self.assertTrue(self.toggl.running)


class OddValues(Base):
    def test_a_negative_project_is_no_project(self):
        self.start(project=-1, task_id=-1, at=T0)
        self.runs.pump()
        body = self.toggl.reqs("POST", "tracking/start")[0].body
        self.assertNotIn("project_id", body)
        self.assertNotIn("task_id", body)
        self.send("update", project=-1)
        self.clock.advance(SETTLE_S)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("PATCH"), [])

    def test_a_very_long_adopted_description_still_fits_a_line(self):
        self.toggl.foreign_timer(desc="x" * 50000)
        result = self.runs.sync(BOARD, 7, 0)
        self.assertLess(len(encode(result)), LINE_MAX)
        self.assertEqual(result["result"], "adopted")
        self.assertEqual(len(self.runs.data["runs"][0]["desc"]), 50000)  # what Toggl has isn't cut
        self.send("stop", rid=result["rid"], at=T0 + 100)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("PATCH"), [])


class KeyProblems(Base):
    def test_a_rejected_key_keeps_the_work_until_a_new_key(self):
        self.key = "wrong"
        self.start(at=T0)
        self.assertIsNone(self.runs.pump())
        self.assertEqual(self.runs.status(), "no_key")
        self.assertEqual(self.runs.queued(), 1)
        before = self.toggl.count()
        self.clock.advance(3600)
        self.runs.pump()
        self.assertEqual(self.toggl.count(), before)  # no retries with a rejected key
        self.key = KEY
        self.client.reset()
        self.runs.pump()
        self.assertEqual(self.runs.status(), "ok")
        self.assertEqual(self.toggl.entries[self.toggl.running]["start"], iso(T0))

    def test_no_key_holds_the_work_without_a_request(self):
        self.key = None
        self.start(at=T0)
        self.send("stop", at=T0 + 100)
        self.assertIsNone(self.runs.pump())
        self.assertEqual(self.toggl.count(), 0)
        self.assertEqual(self.runs.status(), "no_key")
        self.key = KEY
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "time-entries")[0].body["duration"], 100)


class Organization(Base):
    def test_the_config_comes_first(self):
        self.toggl.settings_org = False
        self.start(at=T0)
        self.runs.pump()
        self.assertEqual(self.client.oid, ORG)
        self.assertEqual(len(self.toggl.reqs("GET", "/workspaces/")), 0)

    def test_else_it_comes_from_the_settings(self):
        self.org = 0
        self.start(at=T0)
        self.runs.pump()
        self.assertEqual(self.client.oid, ORG)
        self.assertEqual(self.calls()[0], ("GET", "/api/users/me/settings"))
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/start")), 1)

    def test_else_from_a_workspace_endpoint(self):
        self.org = 0
        self.toggl.settings_org = False
        self.toggl.org_endpoint = "/settings"
        self.start(at=T0)
        self.runs.pump()
        self.assertEqual(self.client.oid, ORG)

    def test_not_found_means_not_set_up_with_no_more_requests(self):
        self.org = 0
        self.toggl.settings_org = False
        self.start(at=T0)
        self.runs.pump()
        self.assertEqual(self.runs.status(), "no_key")
        before = self.toggl.count()
        self.assertEqual(before, 5)  # the settings and four probes, once
        self.clock.advance(3600)
        self.runs.pump()
        self.assertEqual(self.toggl.count(), before)
        self.assertEqual(self.runs.queued(), 1)
        self.org = ORG  # bentobox.py set-org, which then reloads
        self.client.reset()
        self.runs.pump()
        self.assertEqual(self.runs.status(), "ok")
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/start")), 1)

    def test_scan_org(self):
        self.assertEqual(scan_org('{"a":1,"mcp_active_organization_id" : 42}'), 42)
        self.assertEqual(scan_org('{"organization_id":0,"current_organization_id":7}'), 7)
        self.assertEqual(scan_org("{}"), 0)

    def test_parse_iso(self):
        self.assertEqual(parse_iso("2026-10-31T12:34:56Z"), parse_iso("2026-10-31T14:34:56+02:00"))
        self.assertEqual(parse_iso("2026-10-31T12:34:56.123Z"), parse_iso("2026-10-31T12:34:56Z"))
        self.assertEqual(parse_iso("2026-10-31T12:34:56-0130"), parse_iso("2026-10-31T14:04:56Z"))
        self.assertEqual(parse_iso("2026-10-31T12:34:56"), parse_iso("2026-10-31T12:34:56Z"))
        self.assertEqual(parse_iso("nonsense"), 0)
        self.assertEqual(iso(parse_iso("2026-10-31T12:34:56Z")), "2026-10-31T12:34:56Z")


class Persistence(Base):
    def test_an_accepted_message_survives_a_restart_before_any_request(self):
        self.start(at=T0)
        self.send("stop", at=T0 + 300)
        self.assertEqual(self.toggl.count(), 0)
        self.runs = self.make_runs()
        self.assertEqual(self.runs.queued(), 1)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "time-entries")[0].body["duration"], 300)

    def test_a_running_entry_is_still_known_after_a_restart(self):
        entry = self.started(at=T0)
        self.runs = self.make_runs()
        self.send("stop", at=T0 + 300)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "tracking/start"), [])
        self.assertEqual(entry["end"], iso(T0 + 300))

    def test_a_change_waiting_to_settle_survives_a_restart(self):
        self.started(at=T0)
        self.send("update", desc="New")
        self.runs = self.make_runs()
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("PATCH"), [])  # still within 1.5 s of the change
        self.clock.advance(SETTLE_S)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("PATCH")[0].body, {"description": "New"})

    def test_a_crash_after_a_start_that_worked_doesnt_start_it_twice(self):
        self.client.wid, self.client.oid = WID, ORG
        self.start(at=T0)
        self.runs._update(1, attempted=True)  # what's on disk just before the request
        self.toggl.foreign_timer("Write the Q4 plan", start=T0)  # the request went through; the answer never came back
        self.runs = self.make_runs()
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "tracking/start"), [])
        self.send("stop", at=T0 + 100)
        self.runs.pump()
        self.assertEqual(self.toggl.running, 0)

    def test_repeated_ids_are_handled_once_also_after_a_restart(self):
        self.assertTrue(self.send("start", msg_id=17, at=T0, desc="A", task_id=0, project=0))
        self.assertTrue(self.send("start", msg_id=17, at=T0, desc="A", task_id=0, project=0))
        self.assertEqual(self.runs.queued(), 1)
        self.runs = self.make_runs()
        self.assertTrue(self.send("start", msg_id=17, at=T0, desc="A", task_id=0, project=0))
        self.assertEqual(self.runs.queued(), 1)
        self.runs.pump()
        self.assertTrue(self.send("start", msg_id=17, at=T0, desc="A", task_id=0, project=0))
        self.runs.pump()
        self.assertEqual(len(self.toggl.reqs("POST", "tracking/start")), 1)

    def test_a_repeated_stop_changes_nothing(self):
        self.started(at=T0)
        self.send("stop", msg_id=50, at=T0 + 100)
        self.send("stop", msg_id=50, at=T0 + 999)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop")[0].body, {"end": iso(T0 + 100)})

    def test_ids_and_rids_belong_to_a_uid(self):
        self.send("start", rid=1, msg_id=1, uid=helpers.UID, at=T0, desc="A", task_id=0, project=0)
        self.send("start", rid=1, msg_id=1, uid=helpers.OTHER_UID, at=T0 + 100, desc="B", task_id=0, project=0)
        self.assertEqual(self.runs.queued(), 2)
        self.send("update", rid=1, msg_id=2, uid=helpers.OTHER_UID, desc="B2")
        self.assertEqual([r["desc"] for r in self.runs.data["runs"]], ["A", "B2"])
        self.send("stop", rid=1, msg_id=3, uid=helpers.OTHER_UID, at=T0 + 200)
        self.assertEqual([r["stop"] for r in self.runs.data["runs"]], [T0 + 100, T0 + 200])

    def test_a_uid_is_kept_over_a_restart(self):
        self.send("start", rid=1, msg_id=1, uid=helpers.UID, at=T0, desc="A", task_id=0, project=0)
        self.runs = self.make_runs()
        self.assertEqual(self.runs.data["seen"], {helpers.UID: [1]})
        self.assertEqual(self.runs.data["high_rid"], {helpers.UID: 1})
        self.assertEqual(self.runs.data["runs"][0]["uid"], helpers.UID)

    def test_state_from_before_uids_is_set_aside_and_ignored(self):
        """Keyed by model name: nothing real ever ran on this, so it isn't migrated."""
        old = {"version": 1, "next": 9, "seen": {helpers.MODEL: [1, 2, 3]}, "high_rid": {helpers.MODEL: 812}, "outbox": [],
               "runs": [{"key": 8, "board": helpers.MODEL, "rid": 812, "entry_id": 77, "start": T0, "stop": 0, "desc": "Old",
                         "task_id": 0, "project": 0, "adopted": False, "blocked": False, "attempted": False, "changed": T0,
                         "synced": {"desc": "Old", "task_id": 0, "project": 0, "start": T0}}]}
        with open(self.path, "w") as f:
            json.dump(old, f)
        self.runs = self.make_runs()
        self.assertEqual((self.runs.queued(), self.runs.data["seen"], self.runs.data["high_rid"]), (0, {}, {}))
        self.assertTrue(self.send("start", rid=1, msg_id=1, at=T0, desc="New", task_id=0, project=0))  # id 1 isn't a repeat
        self.runs.pump()
        self.assertEqual(self.toggl.entries[self.toggl.running]["description"], "New")
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop"), [])  # and the old open run is gone with its file
        kept = [n for n in os.listdir(self.dir) if ".unreadable-" in n]
        self.assertEqual(len(kept), 1)

    def test_the_seen_list_is_bounded_and_keeps_more_than_the_board_can_resend(self):
        for i in range(runs_mod.SEEN_KEEP + 50):
            self.send("update", rid=999, msg_id=i)
        seen = self.runs.data["seen"][BOARD]
        self.assertEqual(len(seen), runs_mod.SEEN_KEEP)
        self.assertGreater(runs_mod.SEEN_KEEP, 64)
        self.assertEqual(seen[-1], runs_mod.SEEN_KEEP + 49)

    def test_if_it_cant_be_saved_there_is_no_ack_and_no_change(self):
        with mock.patch.object(runs_mod, "write_json_atomic", side_effect=OSError("disk full")):
            self.assertFalse(self.start(at=T0))
        self.assertEqual(self.runs.queued(), 0)
        self.assertNotIn(self.next_id, self.runs.data["seen"].get(BOARD, []))
        self.assertTrue(self.send("start", msg_id=self.next_id, at=T0, desc="A", task_id=0, project=0))  # sent again
        self.assertEqual(self.runs.queued(), 1)

    def test_a_message_with_no_id_gets_no_ack(self):
        self.assertFalse(self.runs.accept(BOARD, {"t": "toggl", "op": "start", "rid": 1}))
        self.assertFalse(self.runs.accept(BOARD, {"t": "toggl", "id": "seven", "op": "start", "rid": 1}))

    def test_an_unknown_op_is_acknowledged(self):
        self.assertTrue(self.send("teleport"))
        self.assertEqual(self.runs.queued(), 0)

    def test_a_missing_time_means_now(self):
        self.send("start", desc="A", task_id=0, project=0)
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "tracking/start")[0].body["start"], iso(T0))
        self.clock.advance(60)
        self.send("stop")
        self.runs.pump()
        self.assertEqual(self.toggl.reqs("POST", "tracking/stop")[0].body["end"], iso(T0 + 60))

    def test_an_unreadable_file_is_kept_aside(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.runs = self.make_runs()
        self.assertEqual(self.runs.queued(), 0)
        self.assertTrue(self.start(at=T0))
        self.assertTrue([n for n in os.listdir(self.dir) if ".unreadable-" in n])

    def test_the_file_is_private(self):
        self.start(at=T0)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_rejections_survive_a_restart(self):
        self.client.wid, self.client.oid = WID, ORG
        self.toggl.fail_next(422)
        self.start(at=T0)
        self.runs.pump()
        self.runs = self.make_runs()
        items = self.runs.outbox_for(BOARD)
        self.assertEqual([i["rid"] for i in items], [812])
        self.runs.outbox_done(items[0]["n"])
        self.runs = self.make_runs()
        self.assertEqual(self.runs.outbox_for(BOARD), [])


class NoPolling(Base):
    def test_an_idle_app_never_asks_toggl(self):
        self.runs.pump()
        self.clock.advance(100000)
        self.assertIsNone(self.runs.pump())
        self.assertEqual(self.toggl.count(), 0)

    def test_a_running_timer_needs_no_requests(self):
        self.started(at=T0)
        for _ in range(50):
            self.clock.advance(3600)
            self.assertIsNone(self.runs.pump())
        self.assertEqual(self.toggl.count(), 0)


class Lists(Base):
    def test_projects_are_filtered_sorted_and_cached(self):
        self.toggl.projects = [
            {"id": 3, "name": "zebra", "color": "#112233"},
            {"id": 1, "name": "Apple", "color": "aabbcc", "active": False},  # "active": false is still in use
            {"id": 2, "name": "archived", "archived_at": "2026-01-01"},
            {"id": 4, "name": "deleted", "deleted_at": "2026-01-01"},
            {"id": 5, "name": "template", "is_template": True},
            {"id": 6, "name": "draft", "draft": True},
            {"id": 7, "name": "Mango"},
            {"name": "no id"},
            "junk",
        ]
        rows = self.runs.load_list("projects")
        self.assertEqual(rows, [{"id": 1, "name": "Apple", "color": "#AABBCC"}, {"id": 7, "name": "Mango", "color": "#8E8E94"},
                                {"id": 3, "name": "zebra", "color": "#112233"}])
        self.assertEqual(self.runs.projects, rows)
        self.assertIn("per_page=100", self.toggl.reqs("GET", "projects")[0].path)
        self.assertEqual(self.make_runs().projects, rows)  # kept for the speech keywords, with no request

    def test_a_data_wrapper_is_fine(self):
        self.assertEqual(lists.project_rows({"data": [{"id": 1, "name": "A"}]})[0]["id"], 1)
        self.assertEqual(lists.project_rows({"nope": 1}), [])

    def test_tasks_are_the_last_20_distinct_pairs_newest_first(self):
        self.runs.projects = [{"id": 5, "name": "Listed", "color": "#010203"}]
        entries = [
            {"id": 1, "description": "Newest", "start": iso(T0), "project_id": 5, "task_id": 9},
            {"id": 2, "description": "newest", "start": iso(T0 - 1), "project_id": 5},       # the same pair, any case
            {"id": 3, "description": "Newest", "start": iso(T0 - 2), "project_id": 6, "project": {"name": "Other", "color": "#0a0b0c"}},
            {"id": 4, "description": "", "task": {"name": "From a task"}, "start": iso(T0 - 3), "task_id": 7},
            {"id": 5, "description": "Planned, not started", "start": None},
            {"id": 6, "description": "", "start": iso(T0 - 4)},                              # nothing to show
        ] + [{"id": 100 + i, "description": f"Old {i}", "start": iso(T0 - 100 - i)} for i in range(40)]
        rows = lists.task_rows(entries, self.runs.projects)
        self.assertEqual(len(rows), lists.MAX_TASKS)
        self.assertEqual(rows[0], {"desc": "Newest", "task_id": 9, "project": {"id": 5, "name": "Listed", "color": "#010203"}})
        self.assertEqual(rows[1]["project"], {"id": 6, "name": "Other", "color": "#0A0B0C"})
        self.assertEqual(rows[2], {"desc": "From a task", "task_id": 7, "project": None})
        self.assertEqual(rows[3]["desc"], "Old 0")

    def test_the_tasks_request(self):
        self.toggl.entries[1] = {"id": 1, "description": "x", "start": iso(T0)}
        self.assertEqual(self.runs.load_list("tasks")[0]["desc"], "x")
        path = self.toggl.reqs("GET", "time-entries")[0].path
        for part in ("per_page=50", "order_by=-start", "include_taskless=true", "type=activity",
                     f"date_from={iso(T0 - 30 * 86400)}", f"date_to={iso(T0 + 60)}"):
            self.assertIn(part, path)

    def test_a_long_real_list_pages_under_the_limit(self):
        self.toggl.projects = [{"id": i + 1, "name": f"{i:03d} " + "プ" * 300, "color": "#123456"} for i in range(100)]
        rows = self.runs.load_list("projects")
        pages = paginate("projects", 5, rows)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(encode(p)) < LINE_MAX for p in pages))
        self.assertEqual(sum(len(p["rows"]) for p in pages), 100)

    def test_one_fetch_is_one_request(self):
        self.client.wid, self.client.oid = WID, ORG
        self.runs.load_list("projects")
        self.assertEqual(self.toggl.count(), 1)

    def test_a_failure_is_none(self):
        self.client.wid, self.client.oid = WID, ORG
        self.toggl.fail_next(500)
        self.assertIsNone(self.runs.load_list("projects"))


if __name__ == "__main__":
    unittest.main()
