#!/usr/bin/env python3
"""Bentobox, the desktop app: the backend behind the board. It tracks your coding agents, talks to Toggl and speech-to-text,
keeps the API keys in the Keychain, and sets the board's lock code.

  bentobox.py init                  create the config
  bentobox.py run [--no-display]    run in the foreground (launchd runs this)
  bentobox.py status                what the running app sees
  bentobox.py set-key toggl|speech  save an API key in the Keychain (it asks for it, and doesn't show it)
  bentobox.py set-org [ID]          the Toggl organization id
  bentobox.py key-status            which keys are set, never what they are
  bentobox.py lock-code set|remove  set or remove the board's lock code
"""
import sys

if sys.version_info < (3, 10):
    sys.exit("Bentobox needs Python 3.10 or newer (for example: brew install python@3.13).")

import argparse
import getpass
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request

import config
import lockcode
from secrets_store import KeychainStore, SecretStoreError, clean_key
from util import log, short

KEY_NAMES = {"toggl": "Toggl", "speech": "speech-to-text"}
_no_proxy = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # these calls stay on this Mac


def find_bin(name, configured):
    if configured and os.path.exists(configured):
        return configured
    found = shutil.which(name)
    if found:
        return found
    for guess in (f"~/.local/bin/{name}", f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}"):
        p = os.path.expanduser(guess)
        if os.path.exists(p):
            return p
    return name


def focus_pane(a, rex, herdr):
    """Brings the agent's terminal pane to the front, in Rex or in Herdr, whichever Agent.app picks. Raises if it can't."""
    app = a.app()
    if app == "rex":
        rex.focus(a.rex_session, a.rex_block)
    elif app == "herdr":
        herdr.focus(a.hc_terminal)
    else:
        raise RuntimeError("no terminal pane known")


def pane_place(a, rex, herdr):
    """Where the agent runs, for its details card. It runs no process: it's called under the zoo's lock."""
    app = a.app()
    if app == "rex":
        return rex.where(a.rex_session)
    if app == "herdr":
        return herdr.where(a.hc_terminal)
    return "No terminal known"


def find_tools(cfg):
    for key, name in (("claude_bin", "claude"), ("rex_bin", "rex"), ("hc_bin", "hc"), ("herdr_app_bin", "herdr-app")):
        cfg[key] = find_bin(name, cfg[key])


def host_name():
    """This Mac's name; the board shows it in Settings."""
    try:
        out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=2).stdout
        if out.strip():
            return short(out, 40)
    except (OSError, subprocess.SubprocessError):
        pass
    return short(socket.gethostname().split(".")[0], 40)


def make_store(cfg):
    return KeychainStore(cfg.get("security_bin") or None)


def read_hidden(prompt):
    """A line from the keyboard with no echo, or from a pipe. getpass would go to /dev/tty even with a pipe."""
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\r\n")


def nudge(cfg):
    """Tells a running app to read its keys, organization and lock code again. False if none is running."""
    req = urllib.request.Request(f"http://127.0.0.1:{cfg['http_port']}/reload", data=b"", method="POST")
    try:
        with _no_proxy.open(req, timeout=2):
            return True
    except (OSError, ValueError, http.client.HTTPException):
        return False


# ---------------------------------------------------------------- commands

def cmd_init(_args, _store=None):
    cfg = config.load_config()
    find_tools(cfg)
    config.save_config(cfg)
    print(f"Config: {config.config_path()} (readable only by you)")
    print("Next: bentobox.py set-key toggl, bentobox.py set-key speech, bentobox.py set-org <id>")


def cmd_set_key(args, store=None):
    cfg = config.load_config()
    store = store or make_store(cfg)
    raw = read_hidden(f"Paste your {KEY_NAMES[args.name]} key (it won't show): ")
    key, dropped = clean_key(raw)
    del raw
    if not key:
        sys.exit("No key given; nothing changed.")
    try:
        store.set(args.name, key)
    except SecretStoreError as e:
        sys.exit(f"Couldn't save the key: {e}")
    if dropped:
        print(f"Ignored {dropped} stray character{'s' if dropped != 1 else ''} around the key.")
    print(f"The {KEY_NAMES[args.name]} key is in the Keychain.")
    nudge(cfg)


def cmd_set_org(args, _store=None):
    cfg = config.load_config()
    text = args.id if args.id is not None else input("Toggl organization id (the first number in "
                                                     "focus.toggl.com/<organization>/workspaces/...): ")
    text = str(text).strip()
    if not text.isascii() or not text.isdigit() or int(text) <= 0:
        sys.exit("The organization id is a positive whole number.")
    cfg["toggl_org"] = int(text)
    config.save_config(cfg)
    print(f"Toggl organization: {cfg['toggl_org']}")
    nudge(cfg)


def cmd_key_status(_args, store=None):
    cfg = config.load_config()
    store = store or make_store(cfg)
    try:
        for name, label in KEY_NAMES.items():
            print(f"{label} key: {'set' if store.has(name) else 'not set'}")
    except SecretStoreError as e:
        sys.exit(f"Couldn't ask the Keychain: {e}")
    print(f"Toggl organization: {cfg['toggl_org'] or 'not set (Bentobox looks it up in your Toggl settings)'}")
    lock = lockcode.LockStore(config.state_path("lock.json"))
    sent = len(lock.sent_to())
    has = f", {sent} board{'s have' if sent != 1 else ' has'} it" if lock.is_set() and sent else ''
    print(f"Lock code: {'set' if lock.is_set() else 'not set'}{', waiting for a board' if lock.is_pending() else has}")


def _read_code():
    if not sys.stdin.isatty():
        return read_hidden("")
    code = getpass.getpass("New 4-digit lock code (it won't show): ")
    if lockcode.is_valid(code) and getpass.getpass("Type it again: ") != code:
        sys.exit("Those weren't the same; nothing changed.")
    return code


def cmd_lock_code(args, _store=None):
    cfg = config.load_config()
    lock = lockcode.LockStore(config.state_path("lock.json"))
    if args.action == "set":
        code = _read_code()
        if not lockcode.is_valid(code):
            sys.exit("The lock code must be exactly 4 digits; nothing changed.")
        lock.set_code(code)
        del code
        what = "Lock code saved."
    else:
        lock.remove()
        what = "Lock code removed."
    running = nudge(cfg)
    deadline = time.monotonic() + 2
    while running and lock.is_pending() and time.monotonic() < deadline:
        time.sleep(0.1)
    if lock.is_pending():
        print(f"{what} Each board gets it when it next connects.")
    else:
        print(f"{what} The boards connected now have it, and any other board gets it when it next connects.")


def cmd_status(_args, _store=None):
    cfg = config.load_config()
    try:
        with _no_proxy.open(f"http://127.0.0.1:{cfg['http_port']}/state", timeout=2) as r:
            state = json.load(r)
    except (OSError, ValueError, http.client.HTTPException):
        sys.exit("Bentobox isn't running.")
    if not isinstance(state, dict) or not isinstance(state.get("agents"), list):
        sys.exit("Something else answers on Bentobox's port; Bentobox isn't running there.")
    link = state.get("link")
    boards = state.get("boards") if isinstance(state.get("boards"), list) else []
    if link == "off":
        print("Board: off (--no-display)")
    elif boards:
        for b in boards:
            who = ", ".join(str(x) for x in (b.get("name"), b.get("uid")) if x) or "waiting for its answer"
            untrusted = "" if b.get("trusted", True) else ", not trusted yet: see bentobox.py boards"
            print(f"Board: connected ({who}, {link}{untrusted})")
    else:
        print(f"Board: not connected ({link})")
    if "toggl" in state:
        print(f"Toggl: {state['toggl']}")
        print(f"Voice: {'on' if state.get('voice') else 'off (no speech-to-text key)'}")
    for a in state["agents"]:
        subs = f", {len(a['subs'])} subagents" if a["subs"] else ""
        print(f"{a['name']:<28} {a['st']:<8} {a['act']}{subs}")
    if not state["agents"]:
        print("No agents.")


def cmd_run(args, store=None):
    # Imported here so that the other commands start fast and need nothing from these.
    from agents.herdr import Herdr
    from agents.rex import Rex
    from agents.sources import poller
    from agents.zoo import Zoo
    from backend import Backend
    from http_call import is_safe_url
    from link.ble import BleLink
    from link.tcp import TcpLink, ports
    from install import LOG
    from localhttp import make_http
    from util import rotate_log

    rotate_log(LOG)  # (launchd writes it; a run in a terminal leaves it alone unless it's grown)
    cfg = config.load_config()
    find_tools(cfg)
    for url in (cfg["toggl_api"], cfg["stt_url"]):
        if not is_safe_url(url):
            sys.exit(f"{url} isn't HTTPS, and API keys don't go over plain HTTP. Fix the config.")
    zoo, stop = Zoo(), threading.Event()
    rex, herdr = Rex(cfg["rex_bin"]), Herdr(cfg["hc_bin"], cfg["herdr_app_bin"])

    def on_tap(agent_id, source):
        a = zoo.find(agent_id)
        if not a:
            return False
        if not a.app():
            log(f"tap ({source}): {a.label()} has no terminal pane")
            return False
        try:
            focus_pane(a, rex, herdr)
            log(f"tap ({source}): focused {a.label()}")
            return True
        except Exception as e:
            log(f"tap ({source}): couldn't focus {a.label()}: {e}")
            return False

    where_of = lambda a: pane_place(a, rex, herdr)  # noqa: E731
    backend = None
    if args.no_display:
        # Only the agents and local HTTP: no Keychain, no Toggl, no Bluetooth. Saved work waits for a normal run.
        status = lambda: {"now": int(time.time()), "utc": time.localtime().tm_gmtoff,  # noqa: E731
                          "link": "off", "boards": []}
    else:
        backend = Backend(cfg, zoo, store or make_store(cfg), on_tap=on_tap, where_of=where_of, host=host_name())
        status = backend.http_status
    try:
        httpd = make_http(zoo, int(cfg["http_port"]), on_tap, where_of, status=status,
                          reload=backend.reload if backend else None)
    except OSError:
        sys.exit(f"Port {cfg['http_port']} is in use: is Bentobox already running? (bentobox.py status)")
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    threading.Thread(target=poller, args=(zoo, cfg, stop, rex, herdr), daemon=True, name="agents").start()
    tcp = cfg["link"] == "tcp"
    if backend:
        backend.start()
        # One link per board: each simulator's port over TCP, and every board Bluetooth finds.
        links = [TcpLink(cfg, backend, port) for port in ports(cfg)] if tcp else [BleLink(cfg, backend)]
        for i, link in enumerate(links):
            threading.Thread(target=link.run, args=(stop,), daemon=True, name=f"link{i}").start()
    tcp_where = ", ".join(f"{cfg['display_host']}:{p}" for p in ports(cfg))
    where = "off" if args.no_display else (f"TCP {tcp_where}" if tcp else "Bluetooth")
    log(f"Bentobox: hooks and the preview on 127.0.0.1:{cfg['http_port']}, board: {where}")

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        main_loop(stop, zoo, backend)
    except KeyboardInterrupt:
        stop.set()
    if backend:
        backend.stop()
    httpd.shutdown()


def main_loop(stop, zoo, backend, every=1.0):
    """Once a second: the agents and the backend's housekeeping. Whatever goes wrong in one pass is logged, and the next
    pass runs anyway: one odd agent or file mustn't stop the app."""
    while not stop.is_set():
        stop.wait(every)
        for name, tick in (("agents", zoo.tick), ("backend", backend.tick if backend else None)):
            if tick is None:
                continue
            try:
                tick()
            except Exception as e:
                log(f"{name}: {type(e).__name__} in the main loop; carrying on")


def _trust_store():
    from trust import TrustStore
    return TrustStore(config.state_path("boards.json"))


def cmd_boards(args, _store=None):
    """The boards this Mac trusts, and those that connected but aren't trusted yet."""
    trust, cfg = _trust_store(), config.load_config()
    if args.action == "list":
        trusted, seen = trust.trusted(), trust.seen()
        for uid, info in trusted.items():
            print(f"Trusted: {uid}  {info.get('name') or ''}".rstrip())
        for uid, info in seen.items():
            print(f"Not trusted: {uid}  {info.get('name') or ''}  (bentobox.py boards trust {uid})")
        if not trusted and not seen:
            print("No boards yet.")
        return
    uid = args.uid
    if args.action == "trust" and not uid:
        seen = list(trust.seen())
        if len(seen) != 1:
            sys.exit("Name the board: bentobox.py boards trust UID (bentobox.py boards lists them).")
        uid = seen[0]
    if not uid:
        sys.exit("Name the board: bentobox.py boards forget UID")
    if args.action == "trust":
        if not trust.trust(uid):
            sys.exit(f"{uid!s:.20} isn't a board's uid: 8 hex digits, as bentobox.py boards shows.")
        print(f"Trusted {uid}." + (" The board has everything now." if nudge(cfg) else ""))
    elif trust.forget(uid):
        nudge(cfg)
        print(f"Forgot {uid}: it gets only the time from now on.")
    else:
        sys.exit(f"{uid!s:.20} wasn't trusted.")


def main(argv=None, store=None):
    ap = argparse.ArgumentParser(description="Bentobox, the desktop app")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the config").set_defaults(fn=cmd_init)
    run = sub.add_parser("run", help="run the app")
    run.add_argument("--no-display", action="store_true", help="only the agents and local HTTP: no board, Toggl or Keychain")
    run.set_defaults(fn=cmd_run)
    sub.add_parser("status", help="what the running app sees").set_defaults(fn=cmd_status)
    key = sub.add_parser("set-key", help="save an API key in the Keychain")
    key.add_argument("name", choices=sorted(KEY_NAMES))
    key.set_defaults(fn=cmd_set_key)
    org = sub.add_parser("set-org", help="the Toggl organization id")
    org.add_argument("id", nargs="?")
    org.set_defaults(fn=cmd_set_org)
    sub.add_parser("key-status", help="which keys are set").set_defaults(fn=cmd_key_status)
    lock = sub.add_parser("lock-code", help="set or remove the board's lock code")
    lock.add_argument("action", choices=["set", "remove"])
    lock.set_defaults(fn=cmd_lock_code)
    boards = sub.add_parser("boards", help="the boards this Mac trusts: list, trust UID, forget UID")
    boards.add_argument("action", nargs="?", choices=["list", "trust", "forget"], default="list")
    boards.add_argument("uid", nargs="?")
    boards.set_defaults(fn=cmd_boards)
    args = ap.parse_args(argv)
    args.fn(args, store)


if __name__ == "__main__":
    main()
