"""The clocks every board shows. The Mac keeps the latest change to them and passes it on (protocol/PROTOCOL.md, `clock`).

A board sends `clock` after each change made on it, and once after each connection. Its times are by its own clock:
`now` says when it sent the message, and every other time is read as that long before the message arrived. So a board
needs no Mac time, and two boards' clocks never have to agree.

The newest change wins. A board's change is taken if it happened at or after the latest one, and every `clock` from a
board gets an answer that says what won. A board that reports no change of its own only learns the latest, unless the
Mac has none yet: then what it shows becomes the latest.

The latest is saved, so a restarted app can still bring a board that was away up to date. Nothing here is logged but
who changed the clocks: never the task or the project.
"""
import json
import os
import threading
import time

from fields import DEFAULT_COLOR, clamped, color
from link.framing import UNKNOWN_BOARD, parse_uid
from util import log, write_json_atomic

VERSION = 1
TIMER_STATES = ("idle", "running", "paused")
POMO_STATES = ("idle", "running", "paused", "done")
TASK_BYTES = 95      # the board's LINK_TASK_LEN, less its NUL
NAME_BYTES = 47      # LINK_NAME_LEN, less its NUL
LATEST_KEYS = ("from", "seq", "at", "task", "timer", "pomo")
DAY_MS = 24 * 3600 * 1000
MAX_ACC_MS = 400 * DAY_MS
MAX_POMO_MS = 2 * 3600 * 1000  # a Pomodoro is set from 1 to 60 minutes, and the knob can't take it past 60
MAX_RID = 0xFFFFFFF0
_int = clamped


def _text(value, max_bytes):
    """At most max_bytes of UTF-8, never cut inside a character."""
    if not isinstance(value, str):
        return ""
    raw = value.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", "ignore")


def _project(value):
    """id > 0 is a project, 0 is "No project" (or null), and -1 means none was ever chosen."""
    if not isinstance(value, dict):
        return {"id": 0, "name": "", "color": DEFAULT_COLOR}
    pid = _int(value.get("id"), -1, 9e15)
    if pid <= 0:
        return {"id": pid, "name": "", "color": DEFAULT_COLOR}
    return {"id": pid, "name": _text(value.get("name"), NAME_BYTES), "color": color(value.get("color"))}


SEQ_BITS = 30  # a board counts its changes in 30 bits, from a random start


def seq_after(a, b):
    """Whether a board's change numbered a comes after its change numbered b, in its wrapping 30-bit count."""
    d = (a - b) % (1 << SEQ_BITS)
    return 0 < d < 1 << (SEQ_BITS - 1)


class ClockHub:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.Lock()
        self.rev, self.latest = self._load()

    def now_ms(self):
        return int(self.clock() * 1000)

    # ---- the file
    def _load(self):
        """The saved rev and latest. The latest goes through the same checks as a board's `clock`, so a file of the
        right JSON but the wrong shape can't break sync: it's dropped, and the next change starts afresh."""
        try:
            with open(self.path) as f:
                data = json.load(f)
            latest = data.get("latest")
            rev = data.get("rev")
            if data.get("version") != VERSION or isinstance(rev, bool) or not isinstance(rev, int) or rev < 0 \
                    or not isinstance(latest, dict) or not parse_uid(latest.get("from")):
                raise ValueError
            latest = self._read(parse_uid(latest["from"]), dict(latest, now=self.now_ms()))
            if latest is None:
                raise ValueError
            return rev, latest
        except FileNotFoundError:
            return 0, None
        except (OSError, ValueError, AttributeError, TypeError):
            log("clocks: the saved clocks can't be read; starting with none")
            return 0, None

    def _save(self):
        try:
            write_json_atomic(self.path, {"version": VERSION, "rev": self.rev, "latest": self.latest})
        except OSError as e:
            log(f"clocks: couldn't save them ({type(e).__name__})")

    # ---- what a board sent
    def _read(self, uid, msg):
        """The board's clocks, in the Mac's Unix milliseconds. None if it isn't a `clock` that can be used."""
        now = self.now_ms()
        sent = msg.get("now")
        if isinstance(sent, bool) or not isinstance(sent, (int, float)) or not (-9e15 <= sent <= 9e15):
            return None
        offset = now - int(sent)

        def when(value):  # a time by the board's clock, as the Mac's; never later than now
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not (-9e15 <= value <= 9e15):
                return None
            return min(now, int(value) + offset)

        def owner(part):
            return parse_uid(part.get("of")) or uid

        task = msg.get("task") if isinstance(msg.get("task"), dict) else {}
        timer = msg.get("timer") if isinstance(msg.get("timer"), dict) else {}
        pomo = msg.get("pomo") if isinstance(msg.get("pomo"), dict) else {}
        t_state = timer.get("st") if timer.get("st") in TIMER_STATES else "idle"
        t = {"st": t_state, "acc": _int(timer.get("acc"), 0, MAX_ACC_MS), "rid": _int(timer.get("rid"), 0, MAX_RID),
             "of": owner(timer)}
        if t_state == "running":
            start = when(timer.get("start"))
            t["start"] = start if start is not None and start > now - MAX_ACC_MS else now
        p_state = pomo.get("st") if pomo.get("st") in POMO_STATES else "idle"
        total = _int(pomo.get("total"), 0, MAX_POMO_MS)
        p = {"st": p_state, "set": _int(pomo.get("set"), 1, 60, 25), "total": total,
             "left": _int(pomo.get("left"), 0, total), "rid": _int(pomo.get("rid"), 0, MAX_RID), "of": owner(pomo)}
        at = when(msg.get("at"))
        return {"from": uid, "seq": _int(msg.get("seq"), 0, 2 ** 31 - 1), "at": at if at is not None else 0,
                "task": {"desc": _text(task.get("desc"), TASK_BYTES), "task_id": _int(task.get("task_id"), 0, 9e15),
                         "project": _project(task.get("project"))},
                "timer": t, "pomo": p}

    def message(self, answering=None):
        """The latest as a `clock` for a board, or None if there's none. The link sets `now` as it writes it."""
        with self.lock:
            if self.latest is None:
                return None
            msg = {"t": "clock", "rev": self.rev}
            msg.update((k, v) for k, v in self.latest.items() if k in LATEST_KEYS)  # never t, rev or now
            msg["now"] = self.now_ms()
        if answering is not None:
            msg["for"] = answering
        return msg

    def take(self, uid, msg):
        """A board's `clock`. Returns (answer for that board, news for every other board or None)."""
        if not parse_uid(uid) or uid == UNKNOWN_BOARD:  # an old build's: its runs can't be told apart
            return None, None
        snap = self._read(uid, msg)
        if snap is None:
            return None, None
        changed = msg.get("changed") is True
        with self.lock:
            latest = self.latest
            # A later change of the same board's wins over its earlier one whatever the times say: they're read from when
            # each message arrived, and a quick turn's changes come over Bluetooth in a burst. Other boards go by time.
            later_here = latest is not None and latest["from"] == uid and seq_after(snap["seq"], latest["seq"])
            took = latest is None or (changed and (later_here or snap["at"] >= latest["at"]))
            if took:
                self.rev += 1
                self.latest = snap
                self._save()
        if took:
            news = self.message()
            return dict(news, **{"for": snap["seq"]}), news
        return self.message(answering=snap["seq"]), None
