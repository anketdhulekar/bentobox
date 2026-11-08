"""What the Bluetooth and TCP links share: the hello, the state updates and the queue of outgoing messages.

A Link is one board's connection. The backend calls send() from any thread. Each link's session loop drains the queue and writes. Nothing is written
outside a session, and the queue is emptied when a session ends, because a message for a board that went away is either
sent again by its owner (lock_code, entry_status) or asked for again by the board.

`agents` lines skip the queue: each loop writes at most one, as the pace allows, and it's made just then. It goes ahead
of `state`, so that a board usually has its new agents by the time `n` counts them.
"""
import collections
import json
import threading
import time

from link.framing import LINE_MAX, encode
from util import log, short

KEEPALIVE_S = 10    # `state` at least this often
BUSY_WAIT_S = 30    # the board sent this Mac away: wait this long before trying again
RECONNECT_MIN_S = 2
RECONNECT_MAX_S = 8  # an unpaired Mac is only let in during the two minutes of pair mode: keep trying often enough to catch it
SCAN_S = 10


class Link:
    trusted_link = False  # a TCP link to a simulator on this Mac says so; a Bluetooth board must be on the list

    def __init__(self, cfg, backend):
        self.cfg, self.backend = cfg, backend
        self.connected = False
        self.session = 0
        self.busy = None             # the board sent this Mac away: its reason
        self.busy_wait_s = BUSY_WAIT_S
        self.reconnect_min_s = RECONNECT_MIN_S
        self.reconnect_max_s = RECONNECT_MAX_S
        self._queue = collections.deque()
        self._queue_lock = threading.Lock()
        self._sent_key = None
        self._last_state = 0.0

    # -- called by the backend, from any thread
    def send(self, msg, reliable=False, on_sent=None, session=None):
        """Queues a message. False if no board is connected, or if `session` is given and is over. With reliable,
        Bluetooth writes it with a response. on_sent runs once the message has been written."""
        with self._queue_lock:
            if not self.connected or (session is not None and session != self.session):
                return False
            self._queue.append((msg, reliable, on_sent))
            return True

    # -- used by the session loops
    def _start_session(self):
        with self._queue_lock:
            self._queue.clear()
            self.session += 1
            self.connected = True
        self._sent_key, self._last_state = None, 0.0
        self.backend.session_started(self)

    def _end_session(self):
        with self._queue_lock:
            was_connected = self.connected
            self.connected = False
            self._queue.clear()
        if was_connected:
            self.backend.session_ended(self)

    def _take_queue(self):
        with self._queue_lock:
            items = list(self._queue)
            self._queue.clear()
        return items

    def _state_due(self):
        """The `state` message to send now, or None."""
        key = self.backend.state_key()
        now = time.monotonic()
        if key == self._sent_key and now - self._last_state < KEEPALIVE_S:
            return None
        self._sent_key, self._last_state = key, now
        return self.backend.state()

    def _line(self, msg):
        if msg.get("t") == "clock" and "now" in msg:  # its times count back from `now`: the moment it's written
            msg = dict(msg, now=self.backend.now_ms())
        line = encode(msg)
        if len(line) > LINE_MAX:  # the senders keep to the limit, so this is a bug; the board would drop it anyway
            log(f"link: a {short(msg.get('t'), 20)} message is too long ({len(line)} bytes); not sent")
            return None
        return line

    @staticmethod
    def _sent(on_sent):
        if on_sent:
            try:
                on_sent()
            except Exception as e:
                log(f"link: after sending: {type(e).__name__}")

    def handle(self, line):
        """A line from the board. Nothing it sends can take the link down, and its content isn't logged."""
        kind = "?"
        try:
            msg = json.loads(line)
            if not isinstance(msg, dict):
                return
            kind = short(msg.get("t"), 20)
            if msg.get("t") == "busy":
                self.busy = short(msg.get("reason") or "busy", 40)
                return
            self.backend.on_message(msg, self)
        except ValueError:
            return
        except Exception as e:
            log(f"link: couldn't handle a {kind} message ({type(e).__name__})")
