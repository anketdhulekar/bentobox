"""The zoo: all the agents on this Mac, merged from hook events and the agent list.

Sources:
  * hook events from hook.py, from Claude Code, Codex and Grok (instant): POST http://127.0.0.1:<port>/hook
  * `claude agents --json` every few seconds (the full list); the session files in ~/.claude/sessions are the
    fallback (only safe fields are read; never the .key files)
  * the Codex windows (`codex` processes with a terminal) and the Grok windows (~/.grok/active_sessions.json: only the
    session id, the pid, the start time and the folder's name are read), every few seconds
  * the labels of the Rex panes that hold agents (`rex block ls`: only the pane's id and label are kept), and the names
    of the Herdr terminals that do (`hc ls`: only the address, the name and the `where` label), every few seconds: a pane
    or terminal someone named gives its agent that name
See sources.py, rex.py and herdr.py for how each is read.
"""
import hashlib
import json
import os
import threading
import time

from agents.herdr import TERMINAL_ID as HC_TERMINAL_ID
from agents.rex import BLOCK_ID as REX_BLOCK_ID, SESSION_ID as REX_SESSION_ID
from agents.sources import match_codex, pid_alive, window_alive
from link.framing import VERSION
from util import short

DONE_TO_IDLE_S = 600          # "Done" turns into "Idle" after 10 minutes without events
HOOK_TRUST_S = 15             # the poll doesn't override a hook event younger than this
GONE_AFTER_S = 60             # an agent missing from the list (and silent) for this long has ended
LATE_EVENTS_S = 60            # a Codex or Grok session that ended: its late events are ignored for this long
MAX_AGENTS, MAX_SUBS = 512, 6
MAX_STORED = 2 * MAX_AGENTS   # agents kept, listed or not: a flood of made-up sessions can't grow without end

# What a hook event may hold. Events come over local HTTP, so each field is checked for its type and size, and
# anything else is dropped (clean_event).
ID_MAX = 64
TEXT_MAX = {"event": 40, "project": 80, "title": 120, "activity": 120, "notice": 240, "agent_type": 40,
            "notification_type": 40, "source": 20, "reason": 40, "entrypoint": 40}
AGENT_KINDS = ("claude", "codex", "grok")


def short_id(session_id):
    return hashlib.sha1(str(session_id).encode()).hexdigest()[:8]


def clean_event(ev):
    """A hook event with only the fields the zoo reads, each of the right type and no longer than it may be. None if
    it has no usable session id."""
    if not isinstance(ev, dict):
        return None
    sid = ev.get("session_id")
    if not isinstance(sid, str) or not sid or len(sid) > ID_MAX:
        return None
    out = {"session_id": sid, "agent": ev.get("agent") if ev.get("agent") in AGENT_KINDS else "claude"}
    for key, limit in TEXT_MAX.items():
        value = ev.get(key)
        if isinstance(value, str) and value:
            out[key] = value[:limit]
    agent_id = ev.get("agent_id")
    if isinstance(agent_id, str) and 0 < len(agent_id) <= ID_MAX:
        out["agent_id"] = agent_id
    pid = ev.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2 ** 31:
        out["pid"] = pid
    if ev.get("needs") is True:
        out["needs"] = True
    block, session = ev.get("rex_block"), ev.get("rex_session")
    if isinstance(block, str) and REX_BLOCK_ID.fullmatch(block):
        out["rex_block"] = block
        out["rex_session"] = session if isinstance(session, str) and REX_SESSION_ID.fullmatch(session) else ""
    terminal = ev.get("hc_terminal")
    if isinstance(terminal, str) and HC_TERMINAL_ID.fullmatch(terminal):
        out["hc_terminal"] = terminal
    return out


def _count(value):
    """A pid or the like from a local list: a whole number from 0 to 2**31, or 0."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return n if 0 <= n < 2 ** 31 else 0


def is_desktop(entrypoint):
    return "desktop" in (entrypoint or "").lower()


class Agent:
    def __init__(self, sid, now):
        self.sid = sid
        self.id = short_id(sid)
        self.name = ""
        self.pane_name = ""        # the name someone gave its Rex pane or Herdr terminal
        self.title = ""
        self.project = ""
        self.kind = "interactive"
        self.state = "idle"
        self.activity = "Idle"
        self.notice = ""
        self.since = now
        self.started = now
        self.rex_session = ""
        self.rex_block = ""
        self.hc_terminal = ""      # its Herdr terminal: hc's address for it, such as "t7"
        self.subs = {}             # agent_id -> {"type", "activity"}
        self.last_hook = 0.0
        self.last_seen = now
        self.pid = 0
        self.ended = False
        self.agent = "claude"      # or "codex" or "grok"
        self.window = False        # a Codex or Grok window (keyed by its process), with or without a session yet

    def set(self, state, activity, now, notice=None):
        if state != self.state:
            self.since = now
        self.state = state
        self.activity = activity
        if notice is not None:
            self.notice = notice

    def label(self):
        return self.pane_name or self.name or self.title or self.project or self.sid[:8]

    def app(self):
        """Where a tap brings it to the front: "rex", "herdr", or "" for nowhere. Rex comes first when it has both: hc
        gives its terminals a clean environment, so no Rex id is left over in one, but a Rex pane can inherit
        HC_TERMINAL_ID from the terminal that started Rex."""
        return "rex" if self.rex_block else "herdr" if self.hc_terminal else ""

    def pane_id(self):
        """The id of its Rex pane or Herdr terminal, whichever app() picks, or ""."""
        return {"rex": self.rex_block, "herdr": self.hc_terminal}.get(self.app(), "")

    def take_pane(self, ids):
        """Takes the pane that these ids name (a hook event, a window, or pane_env in sources.py). They all come from
        one process's environment, so they replace both the Rex pane and the Herdr terminal it had: a session resumed
        in Herdr leaves its old Rex pane. Ids that name no pane, such as a shared Codex process's, change nothing."""
        if ids.get("rex_block") or ids.get("hc_terminal"):
            self.rex_block = ids.get("rex_block") or ""
            self.rex_session = (ids.get("rex_session") or "") if self.rex_block else ""
            self.hc_terminal = ids.get("hc_terminal") or ""

    def public(self, now, where=""):
        return {
            "id": self.id,
            "name": short(self.label(), 40),
            "proj": short(self.project, 40),
            "kind": "b" if self.kind == "background" else "i",
            "st": self.state,
            "act": short(self.activity, 48),
            "note": short(self.notice, 64),
            "age": int(now - self.since),
            "up": int(now - self.started),
            "pane": 1 if self.app() else 0,
            "where": short(where, 60),
            "subs": [{"id": short_id(k), "type": short(v["type"], 24), "act": short(v["activity"], 48)}
                     for k, v in list(self.subs.items())[:MAX_SUBS]],
            **({"agent": self.agent} if self.agent != "claude" else {}),
        }

    def snapshot(self):
        """What a row shows, apart from `where`, `age` and `up`, as the zoo compares it to see that something changed."""
        return (self.label(), self.project, self.kind, self.state, self.activity, self.notice, self.rex_block,
                self.hc_terminal, self.agent,
                sorted((str(k), v["type"], v["activity"]) for k, v in self.subs.items()) if self.subs else None)


class Zoo:
    """All agents, merged from hook events and the agent list. Thread-safe."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.agents = {}
        self.excluded = set()       # sessions from the Claude desktop app
        self.codex_alias = {}       # a Codex session id -> the key of its window's agent
        self.grok_alias = {}        # a Grok session id -> the key of its window's agent
        self.ended_sessions = {}    # a Codex or Grok session that ended -> when (see _is_late)
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.version = 0
        self._snapshots = {}        # sid -> Agent.snapshot(), as of the last _bump

    # -- hook events
    def on_hook(self, ev):
        ev = clean_event(ev)
        if ev is None:
            return
        sid = ev["session_id"]
        now = self.clock()
        with self.lock:
            kind = ev.get("agent") or "claude"
            event = ev.get("event") or ""
            if kind in ("codex", "grok"):
                if self._is_late(sid, event):
                    return
                if event == "SessionEnd":
                    self.ended_sessions[sid] = now
            if kind == "codex":  # a Codex session belongs to its window's agent, once known
                sid = self.codex_alias.get(sid) or self._alias_window(sid, ev.get("project") or "") or sid
            elif kind == "grok":
                sid = self._grok_key(sid, ev)
            if is_desktop(ev.get("entrypoint")):
                self.excluded.add(sid)
                self.agents.pop(sid, None)
                self._bump(sid)
                return
            if sid in self.excluded:
                return
            a = self.agents.get(sid)
            if a is None:
                if event == "SessionEnd":
                    return
                self._make_room()
                a = self.agents[sid] = Agent(sid, now)
            a.last_hook = a.last_seen = now
            if ev.get("project"):
                a.project = ev["project"]
            if kind in ("codex", "grok"):  # no agent list, so a name from the folder, and its process
                a.agent = kind
                if not a.name and a.project:
                    a.name = f"{a.project}-{a.id[:2]}"
                if ev.get("pid"):
                    a.pid = int(ev["pid"])
                    a.window = a.window or kind == "grok"  # a Grok hook names its window (the agent's key)
            a.take_pane(ev)
            agent_id = ev.get("agent_id")
            if agent_id and event not in ("SubagentStart", "SubagentStop"):
                sub = a.subs.setdefault(agent_id, {"type": ev.get("agent_type") or "subagent", "activity": "Working"})
                if ev.get("activity"):
                    sub["activity"] = ev["activity"]
                if a.state != "needs" and event != "PermissionRequest":
                    self._bump(sid)
                    return
            if event == "SessionStart":
                if ev.get("title"):
                    a.title = ev["title"]
                if ev.get("source") in ("startup", "clear", "resume", "new", "load"):  # Grok says new and load
                    a.subs.clear()
                    a.set("idle", "Waiting for a prompt" if a.window else "Idle", now, "")
            elif event == "UserPromptSubmit":
                a.set("working", "Thinking…", now, "")
            elif event == "PreToolUse":
                a.set("working", ev.get("activity") or "Working", now)
            elif event in ("PostToolUse", "PostToolUseFailure"):
                if a.state != "working":  # the tool ran, so any permission prompt was answered
                    a.set("working", ev.get("activity") or "Working", now, "")
            elif event == "PermissionRequest":
                tool = (ev.get("activity") or "").replace("Needs permission: ", "")
                who = {"codex": "Codex", "grok": "Grok"}.get(a.agent, "Claude")
                a.set("needs", ev.get("activity") or "Needs permission", now,
                      f"{who} needs your permission to use {tool}" if tool else "")
            elif event in ("Interrupt", "StopCancelled"):  # Codex, Grok: you stopped the turn
                a.set("idle", "Interrupted", now)
            elif event == "Notification":
                if ev.get("needs"):
                    note = ev.get("notice") or ""
                    act = "Waiting for your answer" if ev.get("notification_type") != "permission_prompt" else (
                        a.activity if a.state == "needs" else "Needs your permission")
                    a.set("needs", act, now, note or a.notice)
                elif ev.get("notification_type") == "idle_prompt" and a.state != "done":
                    a.set("done", "Done", now)
            elif event == "Stop":
                a.set("done", "Done", now)
            elif event == "StopFailure":
                a.set("failed", "Stopped with an error", now)
            elif event == "SubagentStart" and agent_id:
                if len(a.subs) < MAX_SUBS or agent_id in a.subs:
                    a.subs[agent_id] = {"type": ev.get("agent_type") or "subagent", "activity": "Starting"}
            elif event == "SubagentStop" and agent_id:
                a.subs.pop(agent_id, None)
            elif event == "SessionEnd":
                if a.window:  # the window still runs (or quits in a moment: tick): back to waiting for a prompt
                    for aliases in (self.codex_alias, self.grok_alias):
                        for k in [k for k, v in aliases.items() if v == sid]:
                            del aliases[k]
                    a.subs.clear()
                    a.set("idle", "Waiting for a prompt", now, "")
                else:
                    a.ended = True
                    del self.agents[sid]
            self._bump(sid)

    def _make_room(self):
        """Keeps the zoo under MAX_STORED: the agent heard from longest ago goes, a finished one before a busy one."""
        if len(self.agents) < MAX_STORED:
            return
        idle = [a for a in self.agents.values() if a.state not in ("working", "needs")]
        oldest = min(idle or self.agents.values(), key=lambda a: a.last_seen)
        del self.agents[oldest.sid]
        self._snapshots.pop(oldest.sid, None)

    def _is_late(self, session, event):
        """Has this Codex or Grok session ended already? A window's hooks run as it dies: Stop and SessionEnd at the
        same moment, in either order, and without the window's pid once its process is gone. Such an event must not
        make a new agent, which nothing would remove for 30 minutes, or reach another window in the same folder. A
        SessionStart does count: the session is resumed."""
        if session not in self.ended_sessions:
            return False
        if event == "SessionStart":
            del self.ended_sessions[session]
            return False
        return True

    def _alias_window(self, session, project):
        """A new Codex session: the newest window in its folder that has no session yet takes it."""
        busy = set(self.codex_alias.values())
        free = [a for a in self.agents.values() if a.window and a.sid not in busy and a.project == project]
        if not free:
            return None
        w = max(free, key=lambda a: a.started)
        self.codex_alias[session] = w.sid
        return w.sid

    def _grok_key(self, session, ev):
        """A Grok session belongs to its window's agent, keyed by the window's process, so the character keeps its
        hat through /new. The hook names the window when it runs in one; else ~/.grok/active_sessions.json does
        (sync_grok_windows). A session with neither (a headless run) is its own agent."""
        pid = int(ev.get("pid") or 0)
        if pid:
            self.grok_alias[session] = f"grok-{pid}"
        return self.grok_alias.get(session) or session

    def sync_grok_windows(self, windows):
        """The Grok windows that run now (grok_windows): each gets its own agent at once, waiting for a prompt,
        and its session joins it. An agent that a hook made from the session alone moves into its window.
        An agent leaves when its window's process ends (tick)."""
        now = self.clock()
        with self.lock:
            for w in windows:
                key = f"grok-{w['pid']}"
                a = self.agents.get(key)
                if a is None:
                    a = self.agents[key] = Agent(key, now)
                    a.agent, a.window, a.pid, a.project = "grok", True, w["pid"], w["folder"]
                    a.name = f"{w['folder'] or 'grok'}-{a.id[:2]}"
                    a.started = a.since = w["started"] or now
                    a.last_hook = now
                    a.set("idle", "Waiting for a prompt", now, "")
                session = w["session"]
                if session:
                    old = self.agents.get(session)
                    if old is not None and session != key and old.agent == "grok" and not old.window:
                        a.state, a.activity, a.notice, a.since, a.subs = old.state, old.activity, old.notice, old.since, old.subs
                        a.last_hook = max(a.last_hook, old.last_hook)
                        del self.agents[session]
                    self.grok_alias[session] = key
                a.take_pane(w)
            self._bump()

    def sync_codex_windows(self, windows):
        """The Codex windows that run now: a session agent that has no window yet takes one in its folder;
        any other window gets its own agent at once, waiting for its first prompt (Codex starts a session
        only then). An agent leaves when its window's process ends (tick)."""
        now = self.clock()
        with self.lock:
            used = {a.pid for a in self.agents.values() if a.agent == "codex" and a.pid}
            free = [w for w in windows if w["pid"] not in used]
            sessions = [{"sid": a.sid, "project": a.project, "started": a.started, "pid": a.pid}
                        for a in self.agents.values() if a.agent == "codex" and not a.window]
            for sid, w in match_codex(sessions, free).items():
                a = self.agents[sid]
                a.pid = w["pid"]
                a.take_pane(w)
                used.add(w["pid"])
            for w in windows:
                if w["pid"] in used:
                    continue
                key = f"codex-window-{w['pid']}"
                a = self.agents[key] = Agent(key, now)
                a.agent, a.window, a.pid, a.project = "codex", True, w["pid"], w["folder"]
                a.name = f"{w['folder'] or 'codex'}-{a.id[:2]}"
                a.started = a.since = w["started"] or now
                a.last_hook = now
                a.take_pane(w)
                a.set("idle", "Waiting for a prompt", now, "")
            self._bump()

    # -- the agent list (claude agents --json, or the session files)
    def on_poll(self, rows, complete=True):
        now = self.clock()
        with self.lock:
            seen = set()
            for r in rows:
                sid = str(r.get("sessionId") or r.get("id") or "")
                if not sid or sid in self.excluded:
                    continue
                if is_desktop(r.get("entrypoint")):
                    self.excluded.add(sid)
                    self.agents.pop(sid, None)
                    continue
                seen.add(sid)
                a = self.agents.get(sid)
                if a is None:
                    a = self.agents[sid] = Agent(sid, now)
                    started = r.get("startedAt")
                    if isinstance(started, (int, float)) and started > 0:
                        a.started = started / 1000 if started > 1e11 else started
                    changed = r.get("statusUpdatedAt")
                    if isinstance(changed, (int, float)) and changed > 0:
                        a.since = min(now, changed / 1000 if changed > 1e11 else changed)
                a.last_seen = now
                if r.get("name"):
                    a.name = str(r["name"])
                if r.get("cwd") and not a.project:
                    a.project = os.path.basename(str(r["cwd"]).rstrip("/"))
                a.kind = "background" if r.get("kind") == "background" else "interactive"
                a.pid = _count(r.get("pid"))
                fresh = now - a.last_hook < HOOK_TRUST_S
                status, state = r.get("status"), r.get("state")
                if a.kind == "background" and state:
                    mapping = {"working": "working", "blocked": "needs", "done": "done", "failed": "failed"}
                    if state == "stopped":
                        del self.agents[sid]
                        continue
                    target = mapping.get(state)
                    if target and target != a.state and not fresh:
                        a.set(target, {"needs": "Waiting for you", "done": "Done", "failed": "Stopped with an error"}.get(
                            target, a.activity if a.state == "working" else "Working"), now)
                elif status == "waiting" and a.state != "needs":
                    a.set("needs", "Waiting for you", now)
                elif status == "busy" and a.state in ("idle", "done", "needs") and not fresh:
                    a.set("working", "Working", now)
                elif status == "idle" and a.state in ("working", "needs") and not fresh:
                    a.set("done", "Done", now)
            if complete:  # Codex and Grok agents aren't in this list; tick() checks their process instead
                for sid in [s for s, a in self.agents.items()
                            if s not in seen and a.agent == "claude" and now - a.last_hook > GONE_AFTER_S]:
                    del self.agents[sid]
            self._housekeep(now)
            self._bump()

    def missing_panes(self):
        """Claude sessions with a process but no Rex pane or Herdr terminal yet: (sid, pid) for each."""
        with self.lock:
            return [(a.sid, a.pid) for a in self.agents.values()
                    if a.pid and not a.app() and a.kind != "background" and a.agent == "claude"]

    def set_pane(self, sid, ids):
        """The pane a session's process runs in (pane_env in sources.py), for a session that had none."""
        with self.lock:
            a = self.agents.get(sid)
            if a and not a.app() and (ids.get("rex_block") or ids.get("hc_terminal")):
                a.take_pane(ids)
                self._bump(sid)

    def rex_sessions(self):
        with self.lock:
            return {a.rex_session for a in self.agents.values() if a.rex_session and a.app() == "rex"}

    def hc_terminals(self):
        with self.lock:
            return {a.hc_terminal for a in self.agents.values() if a.app() == "herdr"}

    def set_pane_names(self, names):
        """The names of the Rex panes and Herdr terminals (Rex.pane_names, Herdr.pane_names), by id. One that isn't
        listed leaves its agent's name as it is."""
        with self.lock:
            for a in self.agents.values():
                key = a.pane_id()
                if key and key in names:
                    a.pane_name = names[key]
            self._bump()

    def tick(self):
        with self.lock:
            now = self.clock()
            # A killed Claude session sends no SessionEnd: it leaves as soon as its process is gone.
            for sid in [s for s, a in self.agents.items() if a.agent == "claude" and a.pid and not pid_alive(a.pid)]:
                del self.agents[sid]
            for sid in [s for s, a in self.agents.items() if a.agent in ("codex", "grok") and not window_alive(a, now)]:
                del self.agents[sid]
                self.ended_sessions[sid] = now
                for aliases in (self.codex_alias, self.grok_alias):
                    for k in [k for k, v in aliases.items() if v == sid]:
                        del aliases[k]
                        self.ended_sessions[k] = now
            self._housekeep(now)
            self._bump()

    def _housekeep(self, now):
        for session in [s for s, ended in self.ended_sessions.items() if now - ended >= LATE_EVENTS_S]:
            del self.ended_sessions[session]
        for a in self.agents.values():
            if a.state == "done" and now - a.since > DONE_TO_IDLE_S:
                a.set("idle", "Idle", now)

    def _bump(self, sid=None):
        """Moves the version if something shown changed. With a sid, it looks only at that agent: hook events come
        often, and this runs under the lock."""
        if sid is None:
            snapshots = {s: a.snapshot() for s, a in self.agents.items()}
            changed = snapshots != self._snapshots
            self._snapshots = snapshots
        else:
            a = self.agents.get(sid)
            snapshot = a.snapshot() if a else None
            changed = self._snapshots.get(sid) != snapshot
            if a:
                self._snapshots[sid] = snapshot
            else:
                self._snapshots.pop(sid, None)
        if changed:
            self.version += 1
            self.changed.notify_all()

    # -- outputs
    def _listed(self):
        return sorted(self.agents.values(), key=lambda x: x.started)[:MAX_AGENTS]

    def public(self, where_of=lambda a: ""):
        """Every agent with all its fields, for local HTTP. A board gets rows() instead."""
        now = self.clock()
        with self.lock:
            agents = [a.public(now, where_of(a)) for a in self._listed()]
            return {"t": "state", "v": VERSION, "seq": self.version, "agents": agents}

    def rows(self, where_of=lambda a: ""):
        """A board's view, oldest first: (row, age, up) for each agent. The row leaves out what's empty, and what moves
        by itself is apart from it, so that two rows can be compared as they are."""
        now = self.clock()
        with self.lock:
            return [({k: v for k, v in a.public(now, where_of(a)).items() if v and k not in ("age", "up")},
                     max(0, int(now - a.since)), max(0, int(now - a.started))) for a in self._listed()]

    def count(self):
        with self.lock:
            return min(len(self.agents), MAX_AGENTS)

    def find(self, short):
        with self.lock:
            for a in self.agents.values():
                if a.id == short:
                    return a
        return None
