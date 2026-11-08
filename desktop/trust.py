"""Which boards this Mac trusts.

A board's Bluetooth link to this Mac is paired and encrypted on the board's side, but the Mac can't check that for
itself: anything that advertises the board's service could answer as a board. So the Mac keeps its own list. A board
not on it gets only `state` (the time and counts, nothing private), and nothing it sends is taken: no Toggl message, no
clock, no voice. Its uid is noted, and `bentobox.py boards trust` adds it.

Boards on a TCP link (a simulator on this Mac) are trusted without the list. The first time the list is made, it starts
with the boards this Mac has already worked with, from the Toggl and lock-code records, so nothing stops working.

The file is `boards.json` in the config folder, written whole each time, under a lock: the app and the CLI share it.
"""
import contextlib
import fcntl
import json
import os
import time

from link.framing import UNKNOWN_BOARD, parse_uid
from util import short, write_json_atomic

VERSION = 1
SEEN_KEEP = 32


class TrustStore:
    def __init__(self, path, clock=time.time):
        self.path, self.clock = path, clock

    @contextlib.contextmanager
    def _locked(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("version") != VERSION:
            return None
        for key in ("trusted", "seen"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def _write(self, data):
        write_json_atomic(self.path, data)

    @staticmethod
    def _uid(uid):
        return UNKNOWN_BOARD if uid == UNKNOWN_BOARD else parse_uid(uid)

    def start(self, known_uids):
        """Makes the list the first time, with the boards this Mac already knows. Later calls change nothing."""
        with self._locked():
            if os.path.exists(self.path):
                return
            now = int(self.clock())
            trusted = {u: {"name": "", "since": now} for u in sorted({self._uid(u) for u in known_uids} - {None})}
            self._write({"version": VERSION, "trusted": trusted, "seen": {}})

    def is_trusted(self, uid):
        data = self._read()
        return bool(data) and self._uid(uid) in data["trusted"]

    def trusted(self):
        data = self._read()
        return dict(data["trusted"]) if data else {}

    def seen(self):
        """Boards that connected but aren't trusted, newest first: {uid: {"name", "at"}}."""
        data = self._read()
        if not data:
            return {}
        return dict(sorted(data["seen"].items(), key=lambda kv: -kv[1].get("at", 0)))

    def note_seen(self, uid, name):
        uid = self._uid(uid)
        if not uid:
            return
        with self._locked():
            data = self._read() or {"version": VERSION, "trusted": {}, "seen": {}}
            if uid in data["trusted"]:
                return
            data["seen"][uid] = {"name": short(name, 40), "at": int(self.clock())}
            keep = sorted(data["seen"].items(), key=lambda kv: -kv[1].get("at", 0))[:SEEN_KEEP]
            data["seen"] = dict(keep)
            self._write(data)

    def trust(self, uid, name=""):
        """True if it's trusted now. False if the uid isn't one."""
        uid = self._uid(uid)
        if not uid:
            return False
        with self._locked():
            data = self._read() or {"version": VERSION, "trusted": {}, "seen": {}}
            seen = data["seen"].pop(uid, {})
            data["trusted"][uid] = {"name": short(name or seen.get("name", ""), 40), "since": int(self.clock())}
            self._write(data)
        return True

    def forget(self, uid):
        """True if it was trusted."""
        uid = self._uid(uid)
        with self._locked():
            data = self._read()
            if not data or uid not in data["trusted"]:
                return False
            del data["trusted"][uid]
            self._write(data)
        return True
