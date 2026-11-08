"""The Toggl 2.0 API.

The API is at focus.toggl.com/api with a Bearer key. Almost everything lives under
/organizations/{org}/workspaces/{ws}/. The workspace comes from the user's settings. The organization comes from the
config (`bentobox.py set-org`), else from any response that mentions it.

This class also keeps the backoff, because every answer from Toggl feeds it. Reported limits are about 30 requests an
hour on the free plan. Requests all go out on the run manager's one worker thread. The one call from elsewhere is
reset(), from the HTTP thread when the CLI changed the key or the organization: it moves a generation count, and a
request or a setup that started before it can't change the client's state when it ends, so a fresh key is never
undone by an answer about the old one.
Logs say what failed and its status, never a key, a response or an entry's text.
"""
import calendar
import json
import re
import threading
import time
from email.utils import parsedate_to_datetime

import fields
import http_call
from util import log

BACKOFF_MIN_S = 5
BACKOFF_MAX_S = 600
RETRY_AFTER_MAX_S = 6 * 3600   # a nonsense Retry-After mustn't silence Toggl for days
TIMEOUT_S = 15
ORG_KEYS = ("organization_id", "current_organization_id", "default_organization_id", "mcp_active_organization_id")
ORG_PROBES = ("/workspaces/{}/context", "/workspaces/{}/users/me/settings", "/workspaces/{}/settings",
              "/workspaces/{}/permissions")
PROJECTS_PER_PAGE = 100
PROJECT_PAGES_MAX = 10   # 1000 projects: more than anyone has, and a limit on requests


def iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


_ISO = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(?:Z|([+-])(\d\d)(?::?(\d\d))?)?")


def parse_iso(text):
    """'2026-10-31T12:34:56(.123)(Z|+02:00)' -> seconds since 1970, 0 if unreadable."""
    m = _ISO.match(text or "")
    if not m:
        return 0
    y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
    try:
        t = calendar.timegm((y, mo, d, h, mi, s))
    except ValueError:
        return 0
    if m.group(7):
        offset = int(m.group(8)) * 3600 + int(m.group(9) or 0) * 60
        t -= offset if m.group(7) == "+" else -offset
    return t


def number(obj, key):
    """A number field, also looked up inside a "data" wrapper."""
    if not isinstance(obj, dict):
        return 0
    v = obj.get(key)
    if v is None and isinstance(obj.get("data"), dict):
        v = obj["data"].get(key)
    return fields.number(v)


def scan_org(text):
    """An organization id anywhere in a response: fields Toggl's spec doesn't list sometimes appear anyway."""
    for key in ORG_KEYS:
        for m in re.finditer(rf'"{key}"\s*:\s*(\d+)', text or ""):
            if int(m.group(1)) > 0:
                return int(m.group(1))
    return 0


def parse_retry_after(value):
    """Seconds, from a Retry-After header in either form. 0 if there's none."""
    if not value:
        return 0
    try:
        return max(0, min(RETRY_AFTER_MAX_S, int(value)))
    except ValueError:
        pass
    try:
        return max(0, min(RETRY_AFTER_MAX_S, int(parsedate_to_datetime(value).timestamp() - time.time())))
    except (TypeError, ValueError):
        return 0


class Reply:
    def __init__(self, status, body=b"", retry_after=0):
        self.status = status
        self.text = body.decode("utf-8", "replace") if body else ""
        self.retry_after = retry_after
        try:
            self.data = json.loads(self.text) if self.text else None
        except ValueError:
            self.data = None

    @property
    def ok(self):
        return 200 <= self.status < 300

    @property
    def temporary(self):
        """No answer, rate limited or a server error: worth trying again later."""
        return self.status < 0 or self.status == 429 or self.status >= 500


class Current:
    """Toggl's running timer. id 0 means none is running."""

    def __init__(self, entry=None):
        entry = entry if isinstance(entry, dict) else {}
        self.id = number(entry, "id")
        self.desc = entry.get("description") if isinstance(entry.get("description"), str) else ""
        self.project_id = number(entry, "project_id")
        self.task_id = number(entry, "task_id")
        self.start = parse_iso(entry.get("start")) if isinstance(entry.get("start"), str) else 0
        self.project = entry.get("project") if isinstance(entry.get("project"), dict) else None


class TogglClient:
    def __init__(self, base, get_key, get_org=lambda: 0, clock=time.time, timeout=TIMEOUT_S):
        self.base = base.rstrip("/")
        self.get_key, self.get_org, self.clock, self.timeout = get_key, get_org, clock, timeout
        self.wid = self.oid = 0
        self.key_rejected = False     # Toggl said 401: nothing more until the key changes
        self.no_org = False           # couldn't find the organization: nothing more until `set-org`
        self.next_try = 0.0           # the time of the next attempt after a failure
        self.backoff_s = BACKOFF_MIN_S
        self.rate_until = 0.0         # Toggl answered 429: no requests at all until then
        self.fail_kind = None         # "offline" or "backoff", while the last attempt stands failed
        self.retrying = False         # the run manager has work waiting for next_try
        self.requests = 0             # for the run manager: requests that were answered in this pass
        self.generation = 0           # moved by reset(): what started before it can't change the state after it
        self._reset_lock = threading.Lock()

    # ---- the backoff
    def rate_limited(self):
        return self.clock() < self.rate_until

    def _note(self, reply, what):
        if reply.status == 401:
            log(f"Toggl rejected the API key ({what}). Set a new one: bentobox.py set-key toggl")
            self.key_rejected = True
        elif reply.temporary:
            self._wait(reply, what)
        else:
            self.requests += 1
            if not reply.ok:
                log(f"Toggl {what}: HTTP {reply.status}")

    def _wait(self, reply, what):
        now = self.clock()
        delay = self.backoff_s
        if reply.status == 429:
            delay = max(delay, reply.retry_after)
            self.rate_until = now + delay
            self.fail_kind = "backoff"
        else:
            self.fail_kind = "offline"
        self.next_try = now + delay
        log(f"Toggl {what}: {'no answer' if reply.status < 0 else f'HTTP {reply.status}'}; waiting {delay} s")
        self.backoff_s = min(BACKOFF_MAX_S, self.backoff_s * 2)

    def _cant_tell(self, reply, what):
        """A refusal that says nothing about an entry, on a request that a step can't go without. It's treated like an
        outage, so that the work is tried again later, slowly, instead of waiting forever or being given up."""
        if reply.status != 401 and not reply.temporary:
            self._wait(reply, what)

    def clear(self, reset_backoff):
        """A request or a pass worked. reset_backoff is only for a whole pass: one good GET in a pass that keeps
        failing mustn't keep the delay short, or the free plan's requests would go in minutes."""
        self.fail_kind = None
        self.next_try = 0.0
        if reset_backoff:
            self.backoff_s = BACKOFF_MIN_S

    def reset(self):
        """The key or the organization may have changed. Safe from any thread."""
        with self._reset_lock:
            self.generation += 1
            self.wid = self.oid = 0
            self.key_rejected = self.no_org = False

    # ---- requests
    def request(self, method, path, body=None, what=""):
        key = self.get_key()
        if not key:
            return Reply(401)
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode()
        generation = self.generation
        status, content, resp_headers = http_call.call(method, self.base + path, headers, data, self.timeout)
        reply = Reply(status, content, parse_retry_after(resp_headers.get("Retry-After")) if status == 429 else 0)
        if generation == self.generation:  # else the key or the organization changed meanwhile: this says nothing now
            self._note(reply, what or path)
        return reply

    def ensure(self):
        """True once requests can go out: there's a key, Toggl hasn't rejected it, and the workspace is known."""
        if self.key_rejected or self.no_org or not self.get_key():
            return False
        if self.wid and self.oid:
            return True
        return False if self.rate_limited() else self._setup()

    def _setup(self):
        generation = self.generation
        reply = self.request("GET", "/users/me/settings", what="settings")
        if not reply.ok:
            self._cant_tell(reply, "settings")
            return False
        wid = number(reply.data, "current_workspace_id")
        if not wid:
            log("Toggl: no current workspace in your settings")
            self.no_org = True
            return False
        oid = self.get_org() or scan_org(reply.text)
        for probe in ORG_PROBES:  # workspace endpoints that accept an API key; any may mention the organization
            if oid:
                break
            answer = self.request("GET", probe.format(wid), what="looking for your organization")
            if answer.temporary:
                return False
            oid = scan_org(answer.text) if answer.ok else 0
        if generation != self.generation:  # the key or the organization changed while this ran: start again next time
            return False
        if not oid:
            log("Toggl: can't find your organization with an API key. Run: bentobox.py set-org <id> "
                "(the first number in focus.toggl.com/<organization>/workspaces/...)")
            self.no_org = True
            return False
        self.wid, self.oid = wid, oid
        log(f"Toggl ready: organization {oid}, workspace {wid}")
        return True

    def _ws(self, path):
        return f"/organizations/{self.oid}/workspaces/{self.wid}/{path}"

    def current(self):
        """The running timer (id 0: none), or None if Toggl couldn't be asked."""
        reply = self.request("GET", self._ws("tracking/current"), what="check timer")
        if not reply.ok:
            self._cant_tell(reply, "check timer")
            return None
        if reply.status == 204 or reply.data is None:
            return Current()
        data = reply.data
        return Current(data["data"] if isinstance(data, dict) and isinstance(data.get("data"), dict) else data)

    def start_timer(self, body):
        return self.request("POST", self._ws("tracking/start"), body, what="start")

    def stop_timer(self, end):
        """Stops WHATEVER is running in Toggl. Always check current() first."""
        return self.request("POST", self._ws("tracking/stop"), {"end": iso(end)}, what="stop")

    def log_entry(self, body, task_id=0):
        path = f"tasks/{task_id}/time-entries" if task_id else "time-entries"
        return self.request("POST", self._ws(path), body, what="log entry")

    def patch_entry(self, entry_id, body):
        return self.request("PATCH", self._ws(f"time-entries/{entry_id}"), body, what="update")

    def delete_entry(self, entry_id):
        return self.request("DELETE", self._ws(f"time-entries/{entry_id}"), what="delete")

    def get_projects(self):
        """Every project, a page of 100 at a time: one request unless there are more. None if a page failed. A page
        that brings nothing new ends it too, in case Toggl ignores the page number."""
        rows, seen = [], set()
        for page in range(1, PROJECT_PAGES_MAX + 1):
            reply = self.request("GET", self._ws(f"projects?per_page={PROJECTS_PER_PAGE}&page={page}"), what="projects")
            if not reply.ok:
                return None
            data = reply.data
            items = data.get("data") if isinstance(data, dict) else data
            if not isinstance(items, list):
                return data if page == 1 else rows
            fresh = [p for p in items if not (isinstance(p, dict) and p.get("id") in seen)]
            seen.update(p.get("id") for p in items if isinstance(p, dict))
            rows += fresh
            if len(items) < PROJECTS_PER_PAGE or not fresh:
                break
        return rows

    def get_entries(self, per_page):
        now = self.clock()
        query = (f"per_page={per_page}&order_by=-start&include_taskless=true&type=activity"
                 f"&date_from={iso(now - 30 * 86400)}&date_to={iso(now + 60)}")
        reply = self.request("GET", self._ws(f"time-entries?{query}"), what="entries")
        return reply.data if reply.ok else None
