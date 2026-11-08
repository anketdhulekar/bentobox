"""What the tests share. Import this before anything from the app.

It makes real services unreachable from this process: HOME is a temp folder, and the default Toggl and speech-to-text
addresses and the Keychain tool are replaced by dead ones, so a mistake in a test fails instead of reaching them.
"""
import atexit
import base64
import collections
import email.parser
import email.policy
import http.server
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

_HOME = tempfile.mkdtemp(prefix="bentobox-test-home-")
os.environ["HOME"] = _HOME
atexit.register(shutil.rmtree, _HOME, True)

TESTS = os.path.dirname(os.path.abspath(__file__))
DESKTOP = os.path.dirname(TESTS)
REPO = os.path.dirname(DESKTOP)
PROTOCOL = os.path.join(REPO, "protocol")
sys.path.insert(0, DESKTOP)

import config  # noqa: E402
import secrets_store  # noqa: E402
import util  # noqa: E402

DEAD = "http://127.0.0.1:9/blocked-by-tests"
config.DEFAULTS.update(toggl_api=DEAD, stt_url=DEAD, security_bin="/nonexistent/blocked-by-tests", http_port=9)
secrets_store.SECURITY_BIN = "/nonexistent/blocked-by-tests"

LOGS = io.StringIO()     # everything the app logs in this test run
util.log_stream = LOGS

KEY = "toggl_sk_TESTKEY_never_in_a_log_5f1e"
SPEECH_KEY = "sk-TESTSPEECHKEY-never-in-a-log-77ab"
LOCK_CODE = "7391"
TRANSCRIPT_MARK = "PRIVATE-ROOM-CONVERSATION"
ORG, WID = 111, 222
UID, OTHER_UID = "9c41f07a", "1f2e3d4c"
MODEL = "knob-lcd-1.8"
T0 = 1793826000


class Clock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def load_vectors():
    with open(os.path.join(PROTOCOL, "samples", "vectors.json")) as f:
        return json.load(f)


def reference_adpcm():
    """protocol/gen_vectors.py, the reference encoder."""
    spec = importlib.util.spec_from_file_location("bento_gen_vectors", os.path.join(PROTOCOL, "gen_vectors.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audio_messages(pcm, vid=3, samples_per_packet=1600):
    """The `audio` messages a board would send for these samples, each with its own decoder state."""
    gen = reference_adpcm()
    messages, predictor, index = [], 0, 0
    for seq, at in enumerate(range(0, len(pcm), samples_per_packet)):
        part = pcm[at:at + samples_per_packet]
        data = gen.encode(part, predictor, index)
        messages.append({"t": "audio", "vid": vid, "seq": seq, "p": predictor, "i": index, "n": len(part),
                         "b64": base64.b64encode(data).decode()})
        for byte in data:
            for nibble in (byte & 15, byte >> 4):
                predictor, index = gen.nibble_step(predictor, index, nibble)
        if len(part) % 2:  # the odd last nibble is padding, not a sample
            predictor, index = messages[-1]["p"], messages[-1]["i"]
    return messages


def speech_pcm(seconds=1.0, rate=16000, amplitude=9000):
    """Quiet, then a loud tone, then quiet: what prepare() takes for speech."""
    import math
    n = int(seconds * rate)
    out = []
    for i in range(n):
        loud = n // 4 <= i < 3 * n // 4
        out.append(int((amplitude if loud else 3) * math.sin(i * 2 * math.pi * 1000 / rate)))
    return out


def assert_logs_clean(case, text):
    """No key, no lock code and no transcript in what the app logged."""
    for secret in (KEY, SPEECH_KEY, TRANSCRIPT_MARK, "Bearer", "Acme design review"):  # (the last: what fake speech "heard")
        case.assertNotIn(secret, text)
    case.assertNotRegex(text, rf"\b{LOCK_CODE}\b")


class HomeCase(unittest.TestCase):
    """A test with its own empty HOME, so the config folder starts empty. Afterwards, what the app logged during the
    test must hold no secret: checked for every test, in any order and with any runner."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="bentobox-test-")
        self.addCleanup(shutil.rmtree, self.home, True)
        patch = mock.patch.dict(os.environ, {"HOME": self.home})
        patch.start()
        self.addCleanup(patch.stop)
        mark = len(LOGS.getvalue())
        self.addCleanup(lambda: assert_logs_clean(self, LOGS.getvalue()[mark:]))


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):  # no host-name lookup
        http.server.socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class Req:
    def __init__(self, method, path, body, auth):
        self.method, self.path, self.body, self.auth = method, path, body, auth

    def __repr__(self):
        return f"{self.method} {self.path} {self.body}"


class FakeToggl:
    """Toggl 2.0, as much of it as cloud.c uses, with one running timer at a time."""

    def __init__(self, key=KEY, settings_org=True):
        self.key, self.settings_org = key, settings_org
        self.entries = {}
        self.running = 0
        self.next_id = 5000
        self.projects = []
        self.log = []                   # every request that reached this server
        self.script = collections.deque()  # answers to give instead of the real ones: (status, headers, body)
        self.down = False               # close the connection, as a network failure would
        self.drop_after_start = False   # do the next start, but lose the answer
        self.refuse_when_running = True
        self.refuse_delete_running = False
        self.wrap_current = False       # {"data": entry}, as some of Toggl's answers are
        self.org_endpoint = None        # a path under /workspaces/<id> that mentions the organization
        self.lock = threading.Lock()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw
                with fake.lock:
                    fake.log.append(Req(self.command, self.path, body, self.headers.get("Authorization")))
                    if fake.down:
                        answer = None
                    else:
                        answer = fake._answer(self.command, self.path, body, self.headers.get("Authorization"))
                if answer is None:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                status, headers, content = answer
                data = b"" if content is None else (content if isinstance(content, bytes) else json.dumps(content).encode())
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_DELETE = _go

        self.server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- for the tests
    def reqs(self, method=None, part=None):
        with self.lock:
            return [r for r in self.log if (method is None or r.method == method) and (part is None or part in r.path)]

    def clear(self):
        with self.lock:
            self.log.clear()

    def count(self):
        with self.lock:
            return len(self.log)

    def fail_next(self, status, headers=None, body=None, times=1):
        for _ in range(times):
            self.script.append((status, headers or {}, body))

    def foreign_timer(self, desc="From my phone", start=T0 - 1000, project_id=0, task_id=0):
        """A timer started on another device."""
        entry = self._new({"description": desc, "start": iso(start), "project_id": project_id, "task_id": task_id})
        self.running = entry["id"]
        return entry

    def stop_elsewhere(self):
        self.running = 0

    # -- the API
    def _new(self, body):
        self.next_id += 1
        entry = {"id": self.next_id, "description": (body or {}).get("description", ""),
                 "start": (body or {}).get("start"), "project_id": (body or {}).get("project_id"),
                 "task_id": (body or {}).get("task_id"), "duration": (body or {}).get("duration")}
        self.entries[entry["id"]] = entry
        return entry

    def _answer(self, method, path, body, auth):
        if self.script:
            return self.script.popleft()
        if auth != f"Bearer {self.key}":
            return 401, {}, {"error": "unauthorized"}
        route, _, _query = path.partition("?")
        route = route.removeprefix("/api")
        if route == "/users/me/settings":
            settings = {"current_workspace_id": WID}
            if self.settings_org:
                settings["organization_id"] = ORG
            return 200, {}, settings
        if self.org_endpoint and route == f"/workspaces/{WID}{self.org_endpoint}":
            return 200, {}, {"organization_id": ORG}
        prefix = f"/organizations/{ORG}/workspaces/{WID}/"
        if not route.startswith(prefix):
            return 404, {}, {"error": "not found"}
        route = route[len(prefix):]
        if route == "tracking/start" and method == "POST":
            if self.running and self.refuse_when_running:
                return 409, {}, {"error": "a timer is running"}
            entry = self._new(body)
            self.running = entry["id"]
            if self.drop_after_start:
                self.drop_after_start = False
                return None
            return 200, {}, entry
        if route == "tracking/stop" and method == "POST":
            if not self.running:
                return 404, {}, {"error": "nothing is running"}
            entry = self.entries[self.running]
            entry["end"] = (body or {}).get("end")
            self.running = 0
            return 200, {}, entry
        if route == "tracking/current" and method == "GET":
            if not self.running:
                return 204, {}, None
            entry = self.entries[self.running]
            return 200, {}, {"data": entry} if self.wrap_current else entry
        if route == "time-entries" and method == "POST":
            return 200, {}, self._new(body)
        m = re.fullmatch(r"tasks/(\d+)/time-entries", route)
        if m and method == "POST":
            return 200, {}, self._new(dict(body, task_id=int(m.group(1))))
        m = re.fullmatch(r"time-entries/(\d+)", route)
        if m:
            entry = self.entries.get(int(m.group(1)))
            if entry is None:
                return 404, {}, {"error": "no such entry"}
            if method == "PATCH":
                entry.update(body)
                return 200, {}, entry
            if method == "DELETE":
                if self.running == entry["id"] and self.refuse_delete_running:
                    return 400, {}, {"error": "it's running"}
                del self.entries[entry["id"]]
                if self.running == entry["id"]:
                    self.running = 0
                return 204, {}, None
        if route == "projects" and method == "GET":
            return 200, {}, self.projects
        if route == "time-entries" and method == "GET":
            rows = sorted(self.entries.values(), key=lambda e: e.get("start") or "", reverse=True)
            return 200, {}, rows
        return 404, {}, {"error": "not found"}


class FakeSpeech:
    """The speech-to-text service: it parses what it gets, and answers with self.text."""

    def __init__(self, key=SPEECH_KEY, text="Acme design review."):
        self.key, self.text = key, text
        self.requests = []
        self.script = collections.deque()   # statuses to answer with first; None closes the connection
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.requests.append(fake._parse(self.path, self.headers, raw))
                status = fake.script.popleft() if fake.script else 200
                if status is None:
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if self.headers.get("Authorization") != f"Bearer {fake.key}":
                    status = 401
                data = json.dumps({"text": fake.text} if status == 200 else {"error": "no"}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/audio/transcriptions"

    @staticmethod
    def _parse(path, headers, raw):
        msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
            b"Content-Type: " + headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
        fields = {"keywords": [], "path": path, "auth": headers.get("Authorization")}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            value = part.get_payload(decode=True)
            if name == "file":
                fields["file"], fields["filename"] = value, part.get_filename()
                fields["file_type"] = part.get_content_type()
            elif name == "keywords[]":
                fields["keywords"].append(value.decode())
            else:
                fields[name] = value.decode()
        return fields

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeLink:
    """Stands in for a connected link: writes succeed at once, in order."""

    def __init__(self):
        self.sent = []
        self.reliable = []
        self.connected = True
        self.session = 1
        self.fail_writes = False

    def send(self, msg, reliable=False, on_sent=None, session=None):
        if not self.connected or (session is not None and session != self.session):
            return False
        self.sent.append(msg)
        if reliable:
            self.reliable.append(msg)
        if on_sent and not self.fail_writes:
            on_sent()
        return True

    def of_kind(self, kind):
        return [m for m in self.sent if m.get("t") == kind]


def _test_backend_class():
    """Backend, with the shortcuts many tests use for "the board that connected last". The app itself always names a
    board's link."""
    from backend import Backend

    class TestBackend(Backend):
        def _newest(self):
            with self._lock:
                return next(reversed(self.boards.values()), None)

        def _board(self, link=None):
            return super()._board(link) if link is not None else self._newest()

        def on_message(self, msg, link=None):
            b = self._board(link)
            if b is not None:
                super().on_message(msg, b.link)

        def agents_due(self, link=None):
            b = self._board(link)
            return super().agents_due(b.link) if b else None

        @property
        def board_uid(self):
            b = self._newest()
            return b.uid if b else None

        @property
        def board_name(self):
            b = self._newest()
            return b.name if b else None

    return TestBackend


def make_backend(toggl=None, speech=None, store=None, clock=None, **cfg_changes):
    """A Backend with no threads: the test runs its jobs and passes with drain(). Needs a HomeCase."""
    from agents.zoo import Zoo
    Backend = _test_backend_class()
    cfg = dict(config.DEFAULTS, toggl_api=toggl.url if toggl else DEAD, stt_url=speech.url if speech else DEAD,
               toggl_org=ORG, **cfg_changes)
    clock = clock or Clock()
    backend = Backend(cfg, Zoo(clock), store if store is not None else secrets_store.MemoryStore(toggl=KEY, speech=SPEECH_KEY),
                      clock=clock, pace_clock=clock, speech_spawn=lambda fn: fn())
    return backend


def connect(backend, link=None, uid=UID, name=MODEL, board=True, trusted=True):
    """What the link does when a board connects and answers `hello`. uid=None is an old build's `board`, with no uid.
    trusted: the board is on this Mac's list first, as a paired board would be."""
    from link.framing import UNKNOWN_BOARD
    link = link or FakeLink()
    link.connected = True
    if trusted:
        backend.trust.trust(uid or UNKNOWN_BOARD)
    backend.session_started(link)
    if board:
        who = {} if uid is None else {"uid": uid}
        backend.on_message({"t": "board", "v": 4, **who, "name": name, "fw": "0.1.0", "w": 360, "h": 360, "has": ["knob"],
                            "lock": False}, link)
    return link


def disconnect(backend, link):
    link.connected = False
    backend.session_ended(link)
    link.session += 1


def drain(backend):
    backend.runs.run_jobs()
    backend.runs.pump()
