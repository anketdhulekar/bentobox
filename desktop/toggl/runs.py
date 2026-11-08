"""The run manager: turns the board's `toggl` messages into Toggl entries.

The board names each running stretch with a `rid`. This keeps a queue of runs, oldest first, and moves each towards
Toggl one request at a time, on one worker thread:

  * accept() runs on the link's thread. It records the message on disk before the board gets its ack, so an acknowledged
    message survives a restart. It makes no request.
  * pump() runs on the worker. It's the only thing that talks to Toggl, apart from fetch and sync, which run on the same
    thread. It runs only when a board message left work to do, and only at the times worked out from the settle delay
    and the backoff. There is no polling.

Rules that matter (see protocol/PROTOCOL.md):
  * Toggl's stop ends WHATEVER is running, so a run is stopped only after checking that the running timer is its own.
  * A run shorter than 10 s is deleted, unless it was adopted.
  * A run that ended before reaching Toggl is logged whole.
  * Changes settle for 1.5 s, then go out as one PATCH with only what differs from what Toggl has.
  * Text from entries isn't logged.

Everything per board is kept per `uid`, the board's own random id: its seen ids, its runs and its highest rid. `id` and
`rid` are unique only within a uid, so a reflashed board or a second board of the same model can't be mistaken for another.
A board that shows another board's run, because the boards' clocks are kept in step, names it with `of`: that run is
(of, rid), and its messages are counted as its own (uid, id).
"""
import collections
import copy
import json
import os
import threading
import time

from fields import whole
from link.framing import parse_uid
from toggl.client import iso, number
from toggl.lists import ENTRIES_FETCH, MAX_TEXT, project_object, project_rows, task_rows
from util import log, short, write_json_atomic

SETTLE_S = 1.5
MIN_RUN_S = 10
STATE_VERSION = 2    # 2: per uid. A file from before that is set aside: nothing real ever ran on it.
SEEN_KEEP = 1024     # ids remembered per uid; the board holds at most 64 unacknowledged messages
OUTBOX_KEEP = 256
MAX_DESC = 1000
LOST_START_SLACK_S = 1

DONE, AGAIN, HOLD, FAIL = "done", "again", "hold", "fail"
FIELDS = ("desc", "task_id", "project", "start")


_int = whole


def _id(value):
    """A project or task id from the board: 0 means none, and so does anything else that can't be one."""
    number = _int(value)
    return number if number and number > 0 else 0


def _is_short(run):
    return not run["adopted"] and run["stop"] and run["stop"] - run["start"] < MIN_RUN_S


def _changes(run):
    """The PATCH body: only what differs from what Toggl has."""
    synced, body = run["synced"], {}
    if run["desc"] != synced["desc"]:
        body["description"] = run["desc"]
    if run["project"] != synced["project"]:
        body["project_id"] = run["project"] or None
    if run["task_id"] != synced["task_id"]:
        body["task_id"] = run["task_id"] or None
    if run["start"] != synced["start"]:
        body["start"] = iso(run["start"])
    return body


def _entry_body(run):
    body = {"type": "activity", "description": run["desc"], "start": iso(run["start"])}
    if run["project"]:
        body["project_id"] = run["project"]
    return body


def _synced(run):
    return {f: run[f] for f in FIELDS}


class RunManager:
    def __init__(self, path, client, has_key=lambda: True, clock=time.time, on_outbox=None, projects_path=None):
        self.path, self.client, self.has_key, self.clock = path, client, has_key, clock
        self.on_outbox = on_outbox or (lambda: None)
        self.projects_path = projects_path or os.path.join(os.path.dirname(path), "projects.json")
        self.lock = threading.RLock()
        self.data = self._load()
        self.projects = self._load_projects()
        self.last_failed = False       # the last pump pass stopped at a failure
        self._jobs = collections.deque()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # ---- the file on disk
    @staticmethod
    def _empty():
        return {"version": STATE_VERSION, "runs": [], "seen": {}, "high_rid": {}, "outbox": [], "next": 1}

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                raise ValueError
            for key, value in self._empty().items():
                if type(data.get(key)) is not type(value):
                    raise ValueError
            return data
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError):
            moved = f"{self.path}.unreadable-{int(time.time())}"
            try:
                os.replace(self.path, moved)
                log(f"Toggl state: unreadable or from an older version; kept it as {os.path.basename(moved)} and started empty")
            except OSError:
                log("Toggl state: couldn't read it; started empty")
            return self._empty()

    def _save(self):
        write_json_atomic(self.path, self.data)

    def _save_quietly(self):
        """After a request: a failure here only risks doing a harmless step again after a restart."""
        try:
            self._save()
        except OSError as e:
            log(f"Toggl state: couldn't save ({type(e).__name__})")

    def _load_projects(self):
        try:
            with open(self.projects_path) as f:
                rows = json.load(f)
            return rows if isinstance(rows, list) else []
        except (OSError, ValueError):
            return []

    # ---- messages from the board (the link's thread)
    def accept(self, uid, msg):
        """Takes responsibility for a `toggl` message. True means it's saved, or was handled before, so send the ack.
        False means it couldn't be saved: no ack, and the board sends it again after its next connection."""
        msg_id = _int(msg.get("id"))
        if msg_id is None:
            return False
        with self.lock:
            seen = self.data["seen"].setdefault(uid, [])
            if msg_id in seen:
                return True
            before = copy.deepcopy(self.data)
            self._apply(uid, msg)
            seen.append(msg_id)
            del seen[:-SEEN_KEEP]
            try:
                self._save()
            except OSError as e:
                self.data = before
                log(f"Toggl state: couldn't save message {msg_id} ({type(e).__name__}); not acknowledging it")
                return False
        self.wake()
        return True

    def _apply(self, uid, msg):
        op, rid, now = msg.get("op"), _int(msg.get("rid")), self.clock()
        if rid is None:
            return
        owner = uid if op == "start" else parse_uid(msg.get("of")) or uid  # a start is always the sender's own run
        high = self.data["high_rid"]
        high[owner] = max(high.get(owner, 0), rid)
        if op == "start":
            at = _int(msg.get("at")) or int(now)
            later = [r["start"] for r in self.data["runs"] if not r["stop"] and r["start"] > at]
            for run in self.data["runs"]:
                if not run["stop"] and run["start"] <= at:  # an earlier run still open: it stops where this one starts
                    run["stop"] = at
            # A start that arrives late, from a board that was away, after a run that began later: Toggl runs one timer
            # at a time, so this one ended where that one began.
            self.data["runs"].append({
                "key": self.data["next"], "uid": uid, "rid": rid, "entry_id": 0, "start": at, "stop": min(later) if later else 0,
                "desc": self._desc(msg.get("desc")), "task_id": _id(msg.get("task_id")),
                "project": _id(msg.get("project")), "adopted": False, "blocked": False, "attempted": False,
                "changed": now, "synced": None})
            self.data["next"] += 1
            return
        run = self._find_rid(owner, rid)
        if run is None:  # Toggl turned it down, or a sync replaced it: nothing to change
            return
        if op == "update":
            if isinstance(msg.get("desc"), str):
                run["desc"] = self._desc(msg["desc"])
            if _int(msg.get("task_id")) is not None:
                run["task_id"] = _id(msg["task_id"])
            if _int(msg.get("project")) is not None:
                run["project"] = _id(msg["project"])
            run["changed"] = now
        elif op == "shift":
            if _int(msg.get("start")):
                run["start"] = msg["start"]
                run["changed"] = now
        elif op == "stop" and not run["stop"]:
            run["stop"] = _int(msg.get("at")) or int(now)

    @staticmethod
    def _desc(value):
        return value[:MAX_DESC] if isinstance(value, str) else ""

    def _find_rid(self, uid, rid):
        for run in reversed(self.data["runs"]):
            if run["uid"] == uid and run["rid"] == rid:
                return run
        return None

    def _find_key(self, key):
        for run in self.data["runs"]:
            if run["key"] == key:
                return run
        return None

    def _update(self, key, **fields):
        with self.lock:
            run = self._find_key(key)
            if run is not None:
                run.update(fields)
                self._save_quietly()

    def _remove(self, key):
        with self.lock:
            self.data["runs"] = [r for r in self.data["runs"] if r["key"] != key]
            self._save_quietly()

    # ---- entry_status, which must reach the board even if it's away now
    def _reject(self, run):
        log(f"Toggl turned down run {run['rid']} for good")
        with self.lock:
            self.data["outbox"].append({"n": self.data["next"], "uid": run["uid"], "rid": run["rid"]})
            self.data["next"] += 1
            del self.data["outbox"][:-OUTBOX_KEEP]
            self._save_quietly()
        self.on_outbox()

    def outbox_for(self, uid):
        with self.lock:
            return [dict(i) for i in self.data["outbox"] if i["uid"] == uid]

    def outbox_done(self, n):
        with self.lock:
            self.data["outbox"] = [i for i in self.data["outbox"] if i["n"] != n]
            self._save_quietly()

    # ---- the worker thread
    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True, name="toggl")
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=20)

    def wake(self):
        self._wake.set()

    def submit(self, job):
        """Runs job on the worker thread, so that requests go out one at a time."""
        self._jobs.append(job)
        self.wake()

    def run_jobs(self):
        while self._jobs:
            job = self._jobs.popleft()
            try:
                job()
            except Exception as e:
                log(f"Toggl: a job failed ({type(e).__name__})")

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            self.run_jobs()
            try:
                delay = self.pump()
            except Exception as e:
                log(f"Toggl: {type(e).__name__} in the run queue")
                delay = 30
            if not self._jobs and not self._stop.is_set():
                self._wake.wait(delay)

    # ---- moving the runs to Toggl
    def pump(self, user=False):
        """One pass over the queue, oldest first. It stops at the first failure, so that entries reach Toggl in order.
        Returns the seconds until the next pass is due, or None if nothing is due until something happens. `user`: a
        person is waiting, so try now even during an offline backoff. A 429's backoff always holds."""
        c = self.client
        self.last_failed = False
        now = self.clock()
        if c.rate_limited() or (not user and now < c.next_try):
            self.last_failed = True
            return max(c.next_try, c.rate_until) - now if self.data["runs"] or c.retrying else None
        c.requests = 0
        due = None
        with self.lock:
            keys = [r["key"] for r in self.data["runs"]]
        for key in keys:
            result = self._advance(key)
            if result == FAIL:
                self.last_failed = True
                c.retrying = True
                left = c.next_try - self.clock()
                return left if left > 0 else None  # no wait means no key: a reload wakes this
            if isinstance(result, float):
                due = result if due is None else min(due, result)
        if c.requests:
            c.clear(reset_backoff=True)
        c.retrying = False
        return due

    def _advance(self, key):
        while True:
            result = self._step(key)
            if result != AGAIN:
                return result

    def _step(self, key):
        """One step for one run. Works on a snapshot, because the link's thread can change a run during a request."""
        c = self.client
        with self.lock:
            run = self._find_key(key)
            if run is None:
                return DONE
            snap = copy.deepcopy(run)
        short_run = _is_short(snap)
        if not snap["entry_id"]:
            if short_run:  # over within 10 s and never reached Toggl: nothing to do
                self._remove(key)
                return DONE
            if not snap["stop"] and snap["blocked"]:
                return HOLD
            if not c.ensure():
                return FAIL
            return self._log_finished(snap) if snap["stop"] else self._start_entry(snap)
        if short_run:
            return self._delete_entry(snap) if c.ensure() else FAIL
        body = _changes(snap)
        if body:
            if not snap["stop"]:
                left = snap["changed"] + SETTLE_S - self.clock()
                if left > 0:
                    return float(left)  # more changes may follow (knob clicks)
            return self._patch_entry(snap, body) if c.ensure() else FAIL
        if snap["stop"]:
            return self._stop_entry(snap) if c.ensure() else FAIL
        return HOLD

    def _failed_for_good(self, snap, reply):
        """The usual end of a request that didn't work."""
        if reply.temporary or reply.status == 401:
            return FAIL
        self._reject(snap)
        self._remove(snap["key"])
        return DONE

    def _started(self, snap, entry_id):
        synced = _synced(snap)
        self._update(snap["key"], entry_id=entry_id, synced=synced, attempted=False, blocked=False)
        return AGAIN

    @staticmethod
    def _is_our_lost_start(current, snap):
        """Toggl's answer to an earlier start was lost, and this is the entry it made."""
        return (current.id and abs(current.start - snap["start"]) <= LOST_START_SLACK_S
                and current.desc == snap["desc"])

    def _start_entry(self, snap):
        c = self.client
        if snap["attempted"]:  # the last try may have worked: a crash or a lost answer
            current = c.current()
            if current is None:
                return FAIL
            if self._is_our_lost_start(current, snap):
                return self._started(snap, current.id)
        else:
            self._update(snap["key"], attempted=True)
        body = _entry_body(snap)
        if snap["task_id"]:
            body["task_id"] = snap["task_id"]
        reply = c.start_timer(body)
        if reply.ok:
            entry_id = number(reply.data, "id")
            if not entry_id:
                log(f"Toggl started run {snap['rid']} but gave no id")
                self._remove(snap["key"])
                return DONE
            return self._started(snap, entry_id)
        if reply.status in (400, 409, 422):
            current = c.current()
            if current is None:
                return FAIL
            if not current.id:  # nothing runs, so the refusal is about the entry (an archived project, say)
                return self._failed_for_good(snap, reply)
            if self._is_our_lost_start(current, snap):
                return self._started(snap, current.id)
            # A timer from another device runs. It isn't ours to stop. This run waits, with no more requests: a swipe
            # down can adopt that timer, and otherwise this run is logged whole when it stops.
            log(f"Toggl: another timer runs, so run {snap['rid']} waits and that timer keeps running")
            self._update(snap["key"], blocked=True, attempted=False)
            return HOLD
        return self._failed_for_good(snap, reply)

    def _log_finished(self, snap):
        """It already ended (no Mac, or Toggl was out of reach): log it whole."""
        body = _entry_body(snap)
        body["duration"] = snap["stop"] - snap["start"]
        reply = self.client.log_entry(body, snap["task_id"])
        if reply.ok:
            self._remove(snap["key"])
            return DONE
        return self._failed_for_good(snap, reply)

    def _delete_entry(self, snap):
        """An accidental start: leave nothing behind. Toggl may refuse to delete a running entry, so then it's stopped
        first, but only if it's still ours."""
        c = self.client
        reply = c.delete_entry(snap["entry_id"])
        if not reply.ok and reply.status != 404 and not reply.temporary and reply.status != 401:
            current = c.current()
            if current is None:
                return FAIL
            if current.id == snap["entry_id"]:
                stopped = c.stop_timer(snap["stop"])
                if not stopped.ok:
                    return self._failed_for_good(snap, stopped)
            reply = c.delete_entry(snap["entry_id"])
        if reply.ok or reply.status == 404:
            self._remove(snap["key"])
            return DONE
        return self._failed_for_good(snap, reply)

    def _patch_entry(self, snap, body):
        reply = self.client.patch_entry(snap["entry_id"], body)
        if not reply.ok:
            if reply.temporary or reply.status == 401:
                return FAIL
            self._reject(snap)  # the board says so; the run itself goes on
        with self.lock:
            run = self._find_key(snap["key"])
            if run is not None:
                run["synced"] = _synced(snap)
                self._save_quietly()
        return AGAIN

    def _stop_entry(self, snap):
        """Toggl's stop ends whatever runs, so first make sure that's still this entry."""
        c = self.client
        current = c.current()
        if current is None:
            return FAIL
        if current.id != snap["entry_id"]:
            log(f"Toggl: run {snap['rid']} was already stopped elsewhere")
            self._remove(snap["key"])
            return DONE
        reply = c.stop_timer(snap["stop"])
        if reply.ok:
            self._remove(snap["key"])
            return DONE
        return self._failed_for_good(snap, reply)

    # ---- sync (swipe down), on the worker thread
    def sync(self, uid, req, rid):
        """Compares Toggl's running timer with the board's, and answers with a `sync_result`."""
        failed = {"t": "sync_result", "req": req, "result": "failed"}
        c = self.client
        if c.rate_limited() or not c.ensure():
            return failed
        with self.lock:  # a person asked, so a run held back by another timer gets another try
            for run in self.data["runs"]:
                if run["uid"] == uid and run["rid"] == rid:
                    run["blocked"] = False
        self.pump(user=True)  # first send what's queued, so Toggl knows about the board's timer
        if self.last_failed:
            return failed
        current = c.current()
        if current is None:
            return failed
        c.clear(reset_backoff=False)
        self.wake()
        with self.lock:
            ours = self._open_run(uid, rid) if rid else None
            if current.id and ours and ours["entry_id"] == current.id:
                return {"t": "sync_result", "req": req, "result": "in_sync"}
            if current.id:
                return self._adopt(uid, req, rid, current)
            # Nothing runs in Toggl: every open run that reached Toggl was stopped elsewhere.
            gone = [r for r in self.data["runs"] if not r["stop"] and r["entry_id"]]
            self.data["runs"] = [r for r in self.data["runs"] if r not in gone]
            if gone:
                self._save_quietly()
            if (ours and ours in gone) or (rid and not ours):
                log(f"Toggl: run {rid} was stopped elsewhere")
                return {"t": "sync_result", "req": req, "result": "stopped"}
            return {"t": "sync_result", "req": req, "result": "in_sync"}

    def _open_run(self, uid, rid):
        for run in reversed(self.data["runs"]):
            if run["uid"] == uid and run["rid"] == rid and not run["stop"]:
                return run
        return None

    def _adopt(self, uid, req, rid, current):
        """Another timer runs. Toggl runs one at a time, so any open run here has ended, and the board takes this one,
        even if another uid started it. Its new rid is one above the highest seen from this uid, as the protocol says."""
        new_rid = max(self.data["high_rid"].get(uid, 0), rid) + 1
        self.data["high_rid"][uid] = new_rid
        existing = next((r for r in self.data["runs"] if r["entry_id"] == current.id), None)
        self.data["runs"] = [r for r in self.data["runs"] if r["stop"] or r is existing]
        run = existing
        if run is None:
            run = {"key": self.data["next"], "entry_id": current.id, "stop": 0, "blocked": False, "attempted": False,
                   "changed": self.clock()}
            self.data["next"] += 1
            self.data["runs"].append(run)
        run.update(uid=uid, rid=new_rid, adopted=True, stop=0, start=current.start or int(self.clock()),
                   desc=current.desc, task_id=current.task_id, project=current.project_id)
        run["synced"] = _synced(run)
        self._save_quietly()
        log(f"Toggl: adopted the running entry {current.id} as run {new_rid}")
        return {"t": "sync_result", "req": req, "result": "adopted", "rid": new_rid, "desc": short(run["desc"], MAX_TEXT),
                "task_id": run["task_id"], "project": project_object(run["project"], current.project, self.projects),
                "start": run["start"]}

    # ---- lists, on the worker thread
    def load_list(self, what):
        """One Toggl request for that list. The rows, or None if it can't be had now. After a 429, this answers at once
        with no request until the backoff ends."""
        c = self.client
        if c.rate_limited() or not c.ensure():
            return None
        if what == "projects":
            data = c.get_projects()
            if data is None:
                return None
            rows = project_rows(data)
            self.projects = rows
            try:
                write_json_atomic(self.projects_path, rows)
            except OSError as e:
                log(f"Toggl projects: couldn't save them ({type(e).__name__})")
        else:
            data = c.get_entries(ENTRIES_FETCH)
            if data is None:
                return None
            rows = task_rows(data, self.projects)
        c.clear(reset_backoff=False)
        self.wake()
        log(f"Toggl {what}: {len(rows)}")
        return rows

    # ---- what the board sees
    def status(self):
        """`state.toggl`."""
        c = self.client
        if not self.has_key() or c.key_rejected or c.no_org:
            return "no_key"
        if c.rate_limited():
            return "backoff"
        if c.fail_kind and (self.clock() < c.next_try or c.retrying):
            return c.fail_kind
        return "ok"

    def known_uids(self):
        """Every board this Mac has taken a Toggl message from: what the first list of trusted boards starts with."""
        with self.lock:
            return set(self.data["seen"]) | set(self.data["high_rid"]) | {r["uid"] for r in self.data["runs"]}

    def queued(self):
        with self.lock:
            return len(self.data["runs"])


