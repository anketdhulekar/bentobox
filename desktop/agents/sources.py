"""Where the agent list comes from, apart from the hooks: processes, session files and `claude agents --json`.

Privacy: the session files give only SAFE_SESSION_FIELDS, never the .key files or the transcripts. A process's
environment gives only REX_SESSION, REX_BLOCK and HC_TERMINAL_ID. Of a working folder, only the last part is kept. Nothing
here is logged.
"""
import glob
import json
import os
import re
import subprocess
import time

from agents.herdr import TERMINAL_ID
from agents.rex import BLOCK_ID, SESSION_ID
from util import log

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
GROK_SESSIONS = os.path.expanduser("~/.grok/active_sessions.json")
WINDOWLESS_SILENT_S = 1800    # a Codex or Grok agent with no known process: gone after 30 minutes of silence
SAFE_SESSION_FIELDS = ("pid", "sessionId", "name", "cwd", "kind", "entrypoint", "status", "startedAt", "statusUpdatedAt")


def window_pids(rows):
    """The pid of each Codex window, from `ps -axo pid=,ppid=,tty=,comm=` rows: a `codex` process with a
    terminal whose parent is not a `codex` process on the same terminal. One Codex can be a chain of
    `codex` processes (a launcher that starts the real one); the outermost lives as long as the window.
    hook.py's codex_proc() reports the same process."""
    procs = {}
    for row in rows.splitlines():
        parts = row.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit():
            procs[int(parts[0])] = (int(parts[1]), parts[2], os.path.basename(parts[3]) == "codex")
    return [pid for pid, (ppid, tty, codex) in procs.items()
            if codex and tty not in ("??", "-")
            and not (ppid in procs and procs[ppid][2] and procs[ppid][1] == tty)]


_codex_known = {}  # pid -> what codex_windows found about it, kept for as long as it's a window


def codex_windows():
    """The Codex windows on this Mac (window_pids). For each: its pid, the name of its working folder (only
    the last part), when it started, and its Rex pane and Herdr terminal (pane_env). This runs every few seconds, so
    a window is looked into once, when it's first seen, and not with three processes each time."""
    out = []
    try:
        rows = subprocess.run(["ps", "-axo", "pid=,ppid=,tty=,comm="], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    pids = window_pids(rows)
    for pid in [p for p in _codex_known if p not in pids]:
        del _codex_known[pid]
    for pid in pids:
        if pid in _codex_known:
            out.append(_codex_known[pid])
            continue
        try:
            cwd = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=3).stdout
            start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
            started = time.mktime(time.strptime(start, "%a %b %d %H:%M:%S %Y")) if start else 0
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        folder = next((os.path.basename(line[1:].rstrip("/")) for line in cwd.splitlines() if line.startswith("n")), "")
        window = {"pid": pid, "folder": folder, "started": started, **pane_env(pid)}
        if folder and started:  # else one of those lookups came back empty: try again next time
            _codex_known[pid] = window
        out.append(window)
    return out


def match_codex(agents, windows):
    """Pairs Codex agents that have no window yet with Codex windows in the same folder: sid -> window.
    With several candidates, the window that started closest to the agent's first event wins."""
    taken = {a["pid"] for a in agents if a["pid"]}
    free = {}
    for w in windows:
        if w["pid"] not in taken:
            free.setdefault(w["folder"], []).append(w)
    placed = {}
    for a in sorted((a for a in agents if not a["pid"]), key=lambda x: x["started"]):
        cands = free.get(a["project"])
        if not cands:
            continue
        w = min(cands, key=lambda w: abs(w["started"] - a["started"]))
        cands.remove(w)
        placed[a["sid"]] = w
    return placed


def pid_alive(pid):
    """Whether a process runs. Never raises: a pid that can't be one is a process that isn't running."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 2 ** 31:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False


def window_alive(a, now):
    """A Codex or Grok agent lives while its window's process does; without a known window, until 30 minutes of
    silence."""
    return pid_alive(a.pid) if a.pid else now - a.last_hook < WINDOWLESS_SILENT_S


def grok_pids(rows=None):
    """The pids of the Grok apps in a terminal, from `ps -axo pid=,tty=,comm=` rows. A `grok` without a terminal
    (`grok host-server`) is a shared process, not a window."""
    if rows is None:
        try:
            rows = subprocess.run(["ps", "-axo", "pid=,tty=,comm="], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            return set()
    out = set()
    for row in rows.splitlines():
        parts = row.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1] not in ("??", "-") and os.path.basename(parts[2]) == "grok":
            out.add(int(parts[0]))
    return out


_grok_panes = {}  # (pid, start) -> pane_env(pid): a process's environment doesn't change


def grok_windows(path=GROK_SESSIONS, pids=None, pane_of=None):
    """The Grok windows on this Mac, from ~/.grok/active_sessions.json, which Grok keeps current. For each: its
    pid, its session id, the name of its working folder (only the last part), when it started, and its Rex pane and
    Herdr terminal (pane_env). An entry counts only while its pid is a Grok app in a terminal."""
    try:
        with open(path) as f:
            rows = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    pids = grok_pids() if pids is None else pids
    out = []
    for r in rows:
        try:
            pid = int(r.get("pid") or 0)
            start = float(r.get("pid_start") or 0)  # microseconds since 1970
        except (AttributeError, TypeError, ValueError):
            continue
        if pid not in pids:
            continue
        started = start / 1e6 if start > 1e14 else start / 1e3 if start > 1e11 else start
        if pane_of is not None:
            pane = pane_of(pid)
        else:
            pane = _grok_panes.get((pid, start)) or _grok_panes.setdefault((pid, start), pane_env(pid))
        out.append({"pid": pid, "session": str(r.get("session_id") or ""), "started": started,
                    "folder": os.path.basename(str(r.get("cwd") or "").rstrip("/")), **pane})
    return out


# ---------------------------------------------------------------- polling

def read_session_files(directory=SESSIONS_DIR):
    """The fallback list: only the safe fields of ~/.claude/sessions/<pid>.json (never the .key files)."""
    rows = []
    for path in glob.glob(os.path.join(directory, "*.json")):
        try:
            with open(path) as f:
                d = json.load(f)
            row = {k: d.get(k) for k in SAFE_SESSION_FIELDS}
            pid = int(row.get("pid") or 0)
            if pid:
                os.kill(pid, 0)  # raises if the process is gone
            rows.append(row)
        except (OSError, ValueError):
            continue
    return rows


def session_extras(directory=SESSIONS_DIR):
    """pid -> {entrypoint, statusUpdatedAt}, from the session files: the entry point drops desktop-app
    sessions, and the status time gives agents that were already running a real "time in state"."""
    out = {}
    for row in read_session_files(directory):
        if row.get("pid"):
            out[int(row["pid"])] = {"entrypoint": row.get("entrypoint") or "", "statusUpdatedAt": row.get("statusUpdatedAt")}
    return out


def poll_agents(claude_bin):
    """Rows from `claude agents --json`; falls back to the session files. Returns (rows, complete)."""
    try:
        res = subprocess.run([claude_bin, "agents", "--json"], capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout)
        rows = data if isinstance(data, list) else data.get("agents") or data.get("sessions") or []
        extras = session_extras()
        for r in rows:
            if r.get("pid") and int(r["pid"]) in extras:
                for k, v in extras[int(r["pid"])].items():
                    r.setdefault(k, v)
        return rows, True
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log(f"claude agents failed ({e.__class__.__name__}); using the session files")
        return read_session_files(), True


PANE_ENV = re.compile(r"(?:^|\s)(REX_SESSION|REX_BLOCK|HC_TERMINAL_ID)=(\S+)")
PANE_IDS = {"REX_SESSION": ("rex_session", SESSION_ID), "REX_BLOCK": ("rex_block", BLOCK_ID),
            "HC_TERMINAL_ID": ("hc_terminal", TERMINAL_ID)}


def pane_ids(text):
    """The Rex pane and the Herdr terminal that an environment listing names: {"rex_session", "rex_block",
    "hc_terminal"}, with "" for any that's missing or isn't a well-formed id. Nothing else in it is read."""
    out = dict.fromkeys(("rex_session", "rex_block", "hc_terminal"), "")
    for name, value in PANE_ENV.findall(text):
        key, form = PANE_IDS[name]
        if form.fullmatch(value):
            out[key] = value
    return out


def pane_env(pid):
    """The Rex pane and the Herdr terminal a process runs in, from its environment (`ps eww`): see pane_ids.

    The output holds every environment variable, which can include secrets: only the three ids are kept, and nothing
    else is stored or logged."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, ValueError, subprocess.SubprocessError):
        return pane_ids("")
    found = pane_ids(out)
    del out
    return found


def poller(zoo, cfg, stop, rex, herdr):
    looked_up = {}
    while not stop.is_set():
        try:
            rows, complete = poll_agents(cfg["claude_bin"])
            zoo.on_poll(rows, complete)
            for sid, pid in zoo.missing_panes():  # sessions that started before the hooks were installed
                if looked_up.get(sid) != pid:
                    looked_up[sid] = pid
                    zoo.set_pane(sid, pane_env(pid))
            if zoo.rex_sessions():  # with no agent in Rex, no rex command: it could start a Rex server
                rex.refresh_where()
            zoo.sync_codex_windows(codex_windows())
            zoo.sync_grok_windows(grok_windows())
            names = rex.pane_names(zoo.rex_sessions())
            names.update(herdr.pane_names(zoo.hc_terminals()))  # with no agent in hc, no hc command either
            zoo.set_pane_names(names)
        except Exception as e:  # odd data from a tool mustn't end agent tracking
            log(f"agents: {type(e).__name__} while looking for agents")
        stop.wait(max(2, float(cfg["poll_seconds"])))
