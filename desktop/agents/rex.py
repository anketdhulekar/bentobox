"""Rex: focus an agent's pane, and read the names of its panes. Arguments never pass through a shell."""
import json
import re
import subprocess
import time

LISTINGS_PER_CALL = 20  # `rex block ls` is a process per session, and pane_names runs every few seconds
# Rex's ids, as hooks report them. Anything else (an option such as "-C", say) never reaches `rex` as an argument.
SESSION_ID = re.compile(r"session:[0-9A-Za-z-]{1,64}")
BLOCK_ID = re.compile(r"block:[0-9A-Za-z-]{1,64}")


def applescript_string(text):
    """text as an AppleScript string literal's inside: backslashes and quotes escaped, line breaks dropped."""
    return str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def given_name(block):
    """The name someone gave a Rex pane (a row of `rex block ls`), or "" for Rex's own labels: the pane's type
    ("shell"), or "Block" and the end of its id."""
    label = str(block.get("label") or "").strip()
    pane_type = str(block.get("flavor") or "").rsplit(".", 1)[-1]
    if label.lower() == pane_type.lower() or label == f"Block {str(block.get('block_id') or '')[-8:]}":
        return ""
    return label


class Rex:
    def __init__(self, rex_bin):
        self.bin = rex_bin
        self.cache = {}
        self.cache_at = 0.0
        self.next_listing = 0

    def _run(self, *args, timeout=4):
        res = subprocess.run([self.bin, *args], capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip() or f"rex {args[0]} failed")
        return res.stdout

    def _json(self, *args):
        return json.loads(self._run(*args, "--json"))

    def app_client(self):
        for c in self._json("client", "ls").get("clients", []):
            info = c.get("info") or {}
            if info.get("kind") == "app" and c.get("connection_state", "connected") == "connected":
                return c["client_id"], info.get("name") or ""
        raise RuntimeError("the Rex app isn't connected")

    def focus(self, session, block):
        """Show the session in the Rex app, then focus the pane. Arguments never pass through a shell.

        Showing the session needs Remote Control in the app's settings. Without it, the pane still gets the
        focus inside its session, and this raises afterwards to say why the app didn't switch sessions."""
        if not (isinstance(session, str) and SESSION_ID.fullmatch(session) and isinstance(block, str) and BLOCK_ID.fullmatch(block)):
            raise RuntimeError("not a Rex pane")
        # -s: without it, rex looks only in its own session (REX_SESSION, or none under launchd).
        window = (self._json("block", "inspect", block, "-s", session).get("block") or {}).get("window_id")
        app_name, problem = "", None
        try:
            client, app_name = self.app_client()
            args = ["-C", client, "do", "session.select", f"session_id={session}"]
            if window:
                args.append(f"window_id={window}")
            self._run(*args)
        except Exception as e:
            problem = e
        self._run("focus", block, "-s", session)
        if app_name:
            subprocess.run(["osascript", "-e", f'tell application "{applescript_string(app_name)}" to activate'],
                           capture_output=True, timeout=4)
        if problem:
            raise RuntimeError(f"focused the pane, but the app didn't switch sessions: {problem}")

    def pane_names(self, sessions):
        """Pane id -> the name someone gave it, or "" for none, in these Rex sessions. Only the id and the label
        are kept. A session that can't be listed is left out, so its agents keep their names. This runs every few
        seconds, so it never starts a Rex server, and with many sessions it lists some on each call, in turn."""
        names = {}
        sessions = sorted(sessions)
        start = self.next_listing % len(sessions) if sessions else 0
        self.next_listing = start + LISTINGS_PER_CALL
        for session in (sessions[start:] + sessions[:start])[:LISTINGS_PER_CALL]:
            if not SESSION_ID.fullmatch(str(session)):
                continue
            try:
                blocks = self._json("block", "ls", "-s", session, "--autostart=false").get("blocks", [])
            except Exception:
                continue
            for b in blocks:
                if b.get("block_id"):
                    names[b["block_id"]] = given_name(b)
        return names

    def refresh_where(self):
        """Reads the Rex sessions' labels, once a minute at most. The poller does this: where() must not run a process,
        because it's called under the zoo's lock, on the link's thread."""
        now = time.time()
        if now - self.cache_at > 60:
            try:
                self.cache = {s["session_id"]: s.get("label") or "" for s in self._json("ls").get("sessions", [])}
            except Exception:
                self.cache = {}
            self.cache_at = now

    def where(self, session):
        """The Rex session's label, for the details card."""
        label = self.cache.get(session)
        return f"Rex session “{label}”" if label else ("Rex" if session else "")
