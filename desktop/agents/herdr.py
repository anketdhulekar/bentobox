"""Herdr: focus an agent's terminal, and read the names of its terminals. Arguments never pass through a shell.

hc (herdr-core) runs the terminals and keeps them alive, and the Herdr app shows them. An agent in one has the terminal's
address in HC_TERMINAL_ID ("t7"). Of `hc ls`, only the address, the name and the `where` label are kept: never the
folder, the command or anything on the screen.
"""
import json
import os
import re
import subprocess
import time

# hc's address for a terminal on this Mac, as HC_TERMINAL_ID holds it. A name, another machine's terminal ("vps/t7") or an
# option such as "--json" never reaches `herdr-app` as an argument.
TERMINAL_ID = re.compile(r"t[0-9][0-9a-z]{0,15}")
WINDOW_WAIT_S = 8  # how long Herdr, just opened, has to show its window


def app_bundle(app_bin):
    """The Herdr.app that herdr-app is in, or "" if it isn't in one. ~/.local/bin/herdr-app links to
    Herdr.app/Contents/MacOS/Herdr."""
    macos = os.path.dirname(os.path.realpath(app_bin))
    bundle = os.path.dirname(os.path.dirname(macos))
    return bundle if os.path.basename(macos) == "MacOS" and bundle.endswith(".app") else ""


def place(where):
    """A terminal's `where` label ("bentobox / 1"), for the details card. A label that's a path gives only its last part."""
    where = " ".join(str(where or "").split())
    return os.path.basename(where.rstrip("/")) if where.startswith(("/", "~")) else where


class Herdr:
    def __init__(self, hc_bin, app_bin, clock=time.monotonic, sleep=time.sleep):
        self.hc, self.app = hc_bin, app_bin
        self.places = {}  # terminal -> its place (place()), as of the last `hc ls`
        self.clock, self.sleep = clock, sleep

    def _run(self, *args, timeout=4):
        res = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            why = (res.stderr.strip() or res.stdout.strip())[:200]
            raise RuntimeError(why or f"{os.path.basename(args[0])} {args[1]} failed")
        return res.stdout

    def _window(self):
        """Is a Herdr window open? `herdr-app status` fails when none is."""
        try:
            self._run(self.app, "status")
            return True
        except (OSError, subprocess.SubprocessError, RuntimeError):
            return False

    def _show_app(self):
        """Opens Herdr, or brings it to the front. `open` asks macOS to, so Herdr never runs as this app's child."""
        bundle = app_bundle(self.app)
        subprocess.run(["open", bundle] if bundle else ["open", "-a", "Herdr"], capture_output=True, timeout=4)

    def focus(self, terminal):
        """Shows the agent's terminal in the Herdr window, and brings the window to the front. With no window open, it
        opens Herdr first and waits for its window: a tap on the board means you want to see the agent."""
        if not (isinstance(terminal, str) and TERMINAL_ID.fullmatch(terminal)):
            raise RuntimeError("not a Herdr terminal")
        if not self._window():
            self._show_app()
            deadline = self.clock() + WINDOW_WAIT_S
            while not self._window():
                if self.clock() > deadline:
                    raise RuntimeError("Herdr opened, but its window didn't come up")
                self.sleep(0.25)
        self._run(self.app, "do", "agent.focus", "--terminal", terminal)
        self._show_app()

    def pane_names(self, terminals):
        """Terminal -> the name someone gave it, or "" for none, for these terminals, from one `hc ls`. Their places
        come with it, for where(). A listing that fails leaves names and places as they were. With no agent in hc, no hc
        command: it could start hc's server."""
        terminals = {t for t in terminals if isinstance(t, str) and TERMINAL_ID.fullmatch(t)}
        if not terminals:
            return {}
        try:
            rows = json.loads(self._run(self.hc, "ls", "--json")).get("terminals") or []
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError, RuntimeError):
            return {}
        names, places = {}, {}
        for r in rows if isinstance(rows, list) else []:
            if isinstance(r, dict) and r.get("address") in terminals:
                names[r["address"]] = " ".join(str(r.get("name") or "").split())
                places[r["address"]] = place(r.get("where"))
        self.places = places
        return names

    def where(self, terminal):
        """The terminal's place, for the details card. It runs no process: it's called under the zoo's lock."""
        label = self.places.get(terminal)
        return f"Herdr “{label}”" if label else "Herdr"
