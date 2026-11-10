"""The backend: everything that happens between the boards and the Mac's services.

Each connected board is a session of its own. The links hand the backend each message with the link it came on. It
answers through that link, keeps every board's `state` and list of agents current, sends what's owed to a board that
was away, and keeps the boards' clocks in step. It never blocks a link's thread on a network call: Toggl work goes to
the run manager's worker, and speech to a thread of its own.
"""
import threading
import time

import config
from agents.sync import AgentSync
from clock_sync import ClockHub
from link.framing import UNKNOWN_BOARD, VERSION, paginate, parse_uid
from lockcode import LockStore
from secrets_store import NAMES as KEY_NAMES, SecretStoreError
from speech.clips import VoiceDesk
from speech.stt import SpeechClient
from toggl.client import TogglClient
from toggl.runs import RunManager
from trust import TrustStore
from util import log, short

VOICE_MESSAGES = ("voice_start", "audio", "voice_end", "voice_cancel")
LISTS = ("projects", "tasks")
KEY_RECHECK_S = 60   # a missing key is looked for again this often. Only the Keychain is asked: this is not Toggl.


class Keys:
    """API keys from the store. Asking the Keychain runs a program that can take seconds, so only refresh() asks, from
    the main loop or a reload, and the links' threads only ever read the answer it left. A key's value is read when a
    worker first needs it. Errors count as "no key"."""

    def __init__(self, store, clock=time.monotonic):
        self.store, self.clock = store, clock
        self._present, self._values = {}, {}
        self.checked = 0.0
        self.lock = threading.Lock()
        self.refresh()

    def refresh(self):
        present = {}
        for name in KEY_NAMES:
            try:
                present[name] = bool(self.store.has(name))
            except SecretStoreError as e:
                log(f"keys: {e}")
                present[name] = False
        with self.lock:
            self._present, self._values = present, {}
            self.checked = self.clock()

    def has(self, name):
        with self.lock:
            return self._present.get(name, False)

    def get(self, name):
        with self.lock:
            if not self._present.get(name):
                return None
            value = self._values.get(name)
        if value:
            return value
        try:
            value = self.store.get(name)
        except SecretStoreError as e:
            log(f"keys: {e}")
            value = None
        with self.lock:
            self._values[name] = value
        return value


class Board:
    """One connected board: its link, the session it's in, and what this session knows about it."""

    def __init__(self, link):
        self.link, self.session = link, link.session
        self.uid = None               # from its `board` answer: its uid, or UNKNOWN_BOARD
        self.name = None              # its own name, such as "Bento Knob", or its model: only to show
        self.trusted = False          # on this Mac's list, or on a TCP link: only then does it get more than `state`
        self.agents = None            # what it holds, from its `board` answer on
        self.lists_queued = False
        self.outbox_going = set()
        self.lock_going = None

    @property
    def current(self):
        return bool(self.link.connected) and self.link.session == self.session

    def send(self, msg, **kwargs):
        """Queues a message, only while this session lasts: the link checks, under its own lock."""
        return self.link.send(msg, session=self.session, **kwargs)

    def label(self):
        return self.name or "a board"


class Backend:
    def __init__(self, cfg, zoo, store, clock=time.time, on_tap=None, where_of=None, host="Mac", speech_spawn=None,
                 pace_clock=time.monotonic):
        self.cfg, self.zoo, self.clock, self.host, self.pace_clock = cfg, zoo, clock, host, pace_clock
        self.on_tap = on_tap or (lambda *_: False)
        self.where_of = where_of or (lambda a: "")
        self.keys = Keys(store)
        self.org = int(cfg.get("toggl_org") or 0)
        self.client = TogglClient(cfg["toggl_api"], lambda: self.keys.get("toggl"), lambda: self.org, clock=clock)
        self.runs = RunManager(config.state_path("toggl.json"), self.client, has_key=lambda: self.keys.has("toggl"),
                               clock=clock, on_outbox=self._flush_outbox)
        self.lock_store = LockStore(config.state_path("lock.json"))
        self.clocks = ClockHub(config.state_path("clock.json"), clock=clock)
        self.trust = TrustStore(config.state_path("boards.json"), clock=clock)
        self.trust.start(self.runs.known_uids() | set(self.lock_store.sent_to()))
        self.speech = SpeechClient(cfg["stt_url"], lambda: self.keys.get("speech"))
        self.voice = VoiceDesk(self._send_in, self.speech.transcribe, lambda: self.keys.has("speech"),
                               lambda: self.runs.projects, spawn=speech_spawn)
        self._lock = threading.RLock()
        self.boards = {}                   # link -> Board, for each board connected now
        self._lists_sent = {}              # uid -> the lists it has had since this app started
        self._seq = 0
        self._state_key = None

    def start(self):
        self.runs.start()

    def stop(self):
        self.runs.stop()

    def now_ms(self):
        return int(self.clock() * 1000)

    # ---- the boards' sessions
    def hello(self):
        return {"t": "hello", "v": VERSION, "host": self.host}

    def session_started(self, link):
        with self._lock:
            self.boards[link] = Board(link)

    def session_ended(self, link):
        with self._lock:
            board = self.boards.pop(link, None)
        if board is None:
            return
        self.voice.disconnected(board)
        log(f"board: {board.name} disconnected" if board.name else "board: disconnected")

    def _board(self, link):
        with self._lock:
            return self.boards.get(link)

    def _connected(self):
        with self._lock:
            return [b for b in self.boards.values() if b.current]

    @staticmethod
    def _send_in(msg, session, **kwargs):
        """Sends only if that board's session is still the current one."""
        if session is not None:
            session.send(msg, **kwargs)

    # ---- `state`: the same for every board
    def state_key(self):
        """What makes a `state` due at once. `now` isn't in it: it moves by itself, and the keepalive carries it."""
        key = (time.localtime(self.clock()).tm_gmtoff, self.runs.status(), self.keys.has("speech"), self.zoo.count())
        with self._lock:
            if key != self._state_key:
                self._state_key = key
                self._seq += 1
        return key

    def state(self):
        now = self.clock()
        return {"t": "state", "v": VERSION, "seq": self._seq, "now": int(now), "utc": time.localtime(now).tm_gmtoff,
                "toggl": self.runs.status(), "voice": self.keys.has("speech"), "n": self.zoo.count()}

    # ---- `agents`
    def agents_due(self, link):
        """The next `agents` line for that board, for its link's loop to write. None until a trusted board has answered,
        or between lines."""
        b = self._board(link)
        return b.agents.due() if b and b.agents else None

    def http_status(self):
        boards = [{"name": b.name, "uid": b.uid, "trusted": b.trusted} for b in self._connected()]
        return {"now": int(self.clock()), "utc": time.localtime(self.clock()).tm_gmtoff, "link": self.cfg["link"],
                "boards": boards, "toggl": self.runs.status(), "voice": self.keys.has("speech")}

    # ---- messages from a board
    def on_message(self, msg, link):
        b = self._board(link)
        if b is None:
            return
        kind = msg.get("t")
        if kind == "board":
            self._on_board(msg, b)
        elif not b.trusted:
            return  # an untrusted board gets `state`, and nothing it sends is taken
        elif kind == "resync":
            if b.agents:  # before `board`, the whole list is on its way anyway
                b.agents.restart()
        elif kind == "toggl":
            self._on_toggl(msg, b)
        elif kind == "clock":
            self._on_clock(msg, b)
        elif kind == "tap":
            if isinstance(msg.get("id"), str):  # focusing runs rex or herdr-app, and can wait for Herdr to open: don't hold up the link
                threading.Thread(target=self.on_tap, args=(msg["id"], "board"), daemon=True).start()
        elif kind == "fetch":
            self._on_fetch(msg, b)
        elif kind == "sync":
            self._on_sync(msg, b)
        elif kind in VOICE_MESSAGES:
            self.voice.handle(msg, b)

    def _on_board(self, msg, b):
        uid = parse_uid(msg.get("uid"))
        if uid is None:
            log("board: protocol error: its answer has no valid uid, so it's from an old build. Treating it as one fixed "
                "unknown board: update its firmware, or its ids can be mistaken for another old build's.")
            uid = UNKNOWN_BOARD
        with self._lock:
            b.uid = uid
            b.name = short(msg.get("title") or msg.get("name") or "board", 40)  # "Bento Knob", or its model
        if msg.get("v") != VERSION:
            log(f"board: it speaks protocol {short(msg.get('v'), 8)}, this app {VERSION}")
        log(f"board: {b.name} ({uid}), firmware {short(msg.get('fw'), 16)}")
        b.trusted = getattr(b.link, "trusted_link", False) or self.trust.is_trusted(uid)
        if not b.trusted:
            self.trust.note_seen(uid, b.name)
            log(f"board: {b.name} ({uid}) isn't one this Mac trusts yet, so it gets only the time. If it's yours: "
                f"bentobox.py boards trust {uid}")
            return
        self._welcome(b)

    def _welcome(self, b):
        """A trusted board has answered: its agents, the clocks, and everything owed to it."""
        b.agents = AgentSync(self.zoo, self.where_of, self.pace_clock)
        latest = self.clocks.message()
        if latest:
            b.send(latest)
        self._flush_outbox(b)
        self._deliver_lock(b)
        self._load_lists_if_needed(b)

    def _on_toggl(self, msg, b):
        if not b.uid:  # the protocol has `toggl` only after `board`. With no owner it gets no ack: the board keeps it.
            log("board: a toggl message before its `board` answer; not taking it")
            return
        if self.runs.accept(b.uid, msg):
            b.send({"t": "ack", "id": msg["id"]})

    def _on_clock(self, msg, b):
        """A board's clocks: answer it, and pass a change that won to every other board."""
        answer, news = self.clocks.take(b.uid, msg)
        if answer:
            b.send(answer)
        if news:
            others = [o for o in self._connected() if o is not b and o.trusted and o.uid and o.uid != UNKNOWN_BOARD]
            for other in others:
                other.send(news)
            if others:
                log(f"clocks: {b.label()} changed them; sent to {', '.join(o.label() for o in others)}")

    def _on_fetch(self, msg, b):
        req, what, uid = msg.get("req"), msg.get("what"), b.uid
        if not isinstance(req, int):
            return

        def job():
            rows = self.runs.load_list(what) if what in LISTS else None
            if rows is None:
                b.send({"t": "fetch_failed", "req": req, "what": short(what, 16)})
            else:
                self._send_pages(what, req, rows, b, uid)
        self.runs.submit(job)

    def _on_sync(self, msg, b):
        req, rid, uid = msg.get("req"), msg.get("rid", 0), b.uid
        if not isinstance(req, int) or not isinstance(rid, int):
            return
        if not uid:  # whose timer is it? The rids of an unnamed board can't be matched to anything.
            b.send({"t": "sync_result", "req": req, "result": "failed"})
            return
        self.runs.submit(lambda: b.send(self.runs.sync(uid, req, rid)))

    def _send_pages(self, what, req, rows, b, uid):
        def sent():
            if uid:
                with self._lock:
                    self._lists_sent.setdefault(uid, set()).add(what)
        pages = paginate(what, req, rows)
        for page in pages[:-1]:
            b.send(page)
        b.send(pages[-1], on_sent=sent)

    def _load_lists_if_needed(self, b=None):
        """The one list load the protocol allows without a `fetch`: both lists, once per uid each time this app starts,
        with req 0, because that's what fills a board's empty cache. A failed load is tried again only at the next
        connection."""
        for board in [b] if b else self._connected():
            with self._lock:
                uid = board.uid
                if not uid or not board.trusted or board.lists_queued:
                    continue
                missing = [w for w in LISTS if w not in self._lists_sent.get(uid, ())]
                if not missing or not self.keys.has("toggl") or self.client.key_rejected or self.client.no_org \
                        or self.client.rate_limited():
                    continue
                board.lists_queued = True
            self.runs.submit(lambda board=board, uid=uid, missing=missing: self._load_lists(board, uid, missing))

    def _load_lists(self, board, uid, missing):
        try:
            for what in missing:
                rows = self.runs.load_list(what)
                if rows is None:
                    break
                self._send_pages(what, 0, rows, board, uid)  # req 0: no `fetch` asked for these
        finally:
            with self._lock:
                board.lists_queued = False

    # ---- what's owed to a board
    def _flush_outbox(self, b=None):
        for board in [b] if b else self._connected():
            with self._lock:
                if not board.uid or not board.trusted:
                    continue
                for item in self.runs.outbox_for(board.uid):
                    n = item["n"]
                    if n in board.outbox_going:
                        continue
                    board.outbox_going.add(n)
                    board.send({"t": "entry_status", "rid": item["rid"], "status": "rejected"}, reliable=True,
                               on_sent=lambda n=n: self.runs.outbox_done(n))

    def _deliver_lock(self, b=None):
        """The lock code, set once on the Mac, goes to every board that doesn't have it yet."""
        for board in [b] if b else self._connected():
            with self._lock:
                uid = board.uid
                if not uid or not board.trusted:
                    continue
                pending = self.lock_store.pending_for(uid)
                if not pending:
                    continue
                msg, nonce = pending
                if board.lock_going == nonce:
                    continue
                board.lock_going = nonce

                def sent(nonce=nonce, uid=uid, name=board.label()):
                    self.lock_store.mark_sent(nonce, uid)
                    log(f"board: lock code sent to {name}")
                board.send(msg, reliable=True, on_sent=sent)

    # ---- housekeeping
    def reload(self):
        """The CLI changed a key, the organization, the lock code or the trusted boards. Nothing is sent to Toggl
        because of this alone, except a board's first list load, if it couldn't happen earlier."""
        self.keys.refresh()
        self.org = int(config.load_config().get("toggl_org") or 0)
        self.client.reset()
        self.speech.no_keywords = False
        self.runs.wake()
        self._recheck_trust()
        self._deliver_lock()
        self._load_lists_if_needed()

    def _recheck_trust(self):
        """A board trusted just now says hello again, so it sends its `board` and its saved messages, and gets
        everything. A board no longer trusted gets only `state` from now on."""
        for b in self._connected():
            if not b.uid or getattr(b.link, "trusted_link", False):
                continue
            trusted = self.trust.is_trusted(b.uid)
            if trusted and not b.trusted:
                b.send(self.hello())
            elif b.trusted and not trusted:
                b.trusted = False
                b.agents = None

    def tick(self):
        self.voice.tick()
        if not (self.keys.has("toggl") and self.keys.has("speech")) \
                and time.monotonic() - self.keys.checked > KEY_RECHECK_S:
            self.keys.refresh()
