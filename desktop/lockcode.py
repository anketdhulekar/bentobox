"""The board's lock code: 4 digits, set on the Mac, checked on the board with no Mac connected.

The code itself is never stored or logged. Only its salt and PBKDF2 hash are kept, here and on the board. Four digits
are easy to brute-force from that hash, so it never goes in a log either, and the file is readable only by this user.
The CLI writes this file and the running app marks it sent, so both take a file lock around their changes.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re

from util import write_json_atomic

ITERATIONS = 2000  # the board checks this on an ESP32-S3 with no Mac: more would make unlocking slow
SALT_BYTES = 16


def is_valid(code):
    return bool(re.fullmatch(r"[0-9]{4}", code or ""))


def hash_code(code, salt, iterations=ITERATIONS):
    return hashlib.pbkdf2_hmac("sha256", code.encode(), salt, iterations, 32).hex()


class LockStore:
    def __init__(self, path):
        self.path = path

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
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    # One code for every board. `nonce` names this code (or its removal), and `sent` lists the boards that have it:
    # each board that connects gets it once, whenever it connects.
    def set_code(self, code):
        if not is_valid(code):
            raise ValueError("the lock code must be 4 digits")
        salt = os.urandom(SALT_BYTES)
        with self._locked():
            write_json_atomic(self.path, {"salt": salt.hex(), "iter": ITERATIONS, "hash": hash_code(code, salt),
                                          "nonce": os.urandom(8).hex(), "sent": []})

    def remove(self):
        with self._locked():
            write_json_atomic(self.path, {"clear": True, "nonce": os.urandom(8).hex(), "sent": []})

    @staticmethod
    def _nonce(data):
        return data.get("nonce") or ""  # a file with no nonce still names one code: the one it holds

    @staticmethod
    def _sent(data):
        sent = data.get("sent")
        return sent if isinstance(sent, list) else []

    def is_set(self):
        return bool(self._read().get("hash"))

    def is_pending(self):
        """A code was set or removed, and no board has had it yet."""
        data = self._read()
        return bool(data) and (bool(data.get("hash")) or bool(data.get("clear"))) and not self._sent(data)

    def sent_to(self):
        """The uids of the boards that have the current code, or its removal."""
        return list(self._sent(self._read()))

    def pending_for(self, uid):
        """The message that still has to reach this board, as (message, nonce), or None."""
        data = self._read()
        if not data or uid in self._sent(data):
            return None
        if data.get("clear"):
            return {"t": "lock_code", "clear": True}, self._nonce(data)
        if not (data.get("salt") and data.get("iter") and data.get("hash")):
            return None
        return {"t": "lock_code", "salt": data["salt"], "iter": data["iter"], "hash": data["hash"]}, self._nonce(data)

    def mark_sent(self, nonce, uid):
        """That board has it. A code set meanwhile has another nonce, and still counts as not sent."""
        with self._locked():
            data = self._read()
            if not data or self._nonce(data) != nonce:
                return
            sent = self._sent(data)
            if uid not in sent:
                data["nonce"] = nonce
                data["sent"] = sent + [uid]
                write_json_atomic(self.path, data)
