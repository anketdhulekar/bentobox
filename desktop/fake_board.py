#!/usr/bin/env python3
"""A stand-in for a board, for testing the desktop app without one. It speaks the protocol over TCP: it listens, as the
simulator does, and the app connects to it. It answers `hello` with `board`, and records what it gets.

  python3 desktop/fake_board.py [--port 7777]
  then, in ~/.config/bentobox/config.json, set "link": "tcp"

Type a line of JSON and press Enter to send it to the app, such as {"t":"resync"}, or `tap <start of an agent's name>`.
It never prints a lock code's hash. The tests use the FakeBoard class directly.
"""
import argparse
import json
import os
import socket
import sys
import threading
import time

KEEPS_AGENTS = 512


class FakeBoard:
    def __init__(self, port=0, name="knob-lcd-1.8", uid=None, old_build=False, answer=True, busy=None, lock=False,
                 on_message=None):
        """Like a real board, it makes up its uid unless told one. old_build: its `board` has no uid at all."""
        self.name, self.answer, self.busy, self.has_lock = name, answer, busy, lock
        self.uid = uid or os.urandom(4).hex()
        self.old_build = old_build
        self.on_message = on_message
        self.messages = []          # everything since the last clear(), in order
        self.agents = {}            # id -> row, in the board's order, from the `agents` messages
        self.named = None           # the ids the whole list in progress has named so far, or None with no list going
        self.hellos = 0
        self.changed = threading.Condition()
        self.conn = None
        self.write_lock = threading.Lock()
        self.stopping = False
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", port))
        self.srv.listen(1)
        self.srv.settimeout(0.1)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True, name="fake-board")
        self.thread.start()

    def _serve(self):
        while not self.stopping:
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.conn = conn
            self._talk(conn)
            self.conn = None
            conn.close()

    def _talk(self, conn):
        conn.settimeout(0.1)
        buf = b""
        while not self.stopping and self.conn is conn:
            try:
                data = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._got(line)

    def _got(self, line):
        msg = json.loads(line)
        msg["_len"] = len(line) + 1
        with self.changed:
            self.messages.append(msg)
            if msg.get("t") == "agents":
                self._take_agents(msg)
            self.changed.notify_all()
        if msg.get("t") == "hello":
            self.hellos += 1
            if self.busy:
                self.send({"t": "busy", "reason": self.busy})
            elif self.answer:
                who = {} if self.old_build else {"uid": self.uid}
                self.send({"t": "board", "v": 4, **who, "name": self.name, "fw": "0.1.0", "w": 360, "h": 360,
                           "has": ["knob", "haptics", "mic"], "lock": self.has_lock})
        if self.on_message:
            self.on_message(msg)

    def _take_agents(self, msg):
        """As a board does: a row replaces what it held, in its place, or goes at the end, and it keeps the first 512.
        While a whole list is going out, any row counts as named by it, with a page or without. A new row on a full board
        pushes out those not named yet. Once the last page has arrived, what the list didn't name is gone."""
        if msg.get("page") == 0:
            self.named = set()
        for row in msg.get("rows", []):
            if row["id"] not in self.agents and len(self.agents) >= KEEPS_AGENTS and self.named is not None:
                self._drop_unnamed()
            if row["id"] in self.agents or len(self.agents) < KEEPS_AGENTS:
                self.agents[row["id"]] = row
                if self.named is not None:
                    self.named.add(row["id"])
        for agent_id in msg.get("gone", []):
            self.agents.pop(agent_id, None)
        if "page" in msg and msg["page"] == msg["pages"] - 1:
            self._drop_unnamed()
            self.named = None

    def _drop_unnamed(self):
        self.agents = {i: row for i, row in self.agents.items() if i in self.named}

    def send(self, msg):
        data = json.dumps(msg, separators=(",", ":")).encode() + b"\n"
        self.send_raw(data)

    def send_raw(self, data):
        with self.write_lock:
            self.conn.sendall(data)

    def of_kind(self, kind):
        with self.changed:
            return [m for m in self.messages if m.get("t") == kind]

    def wait_for(self, kind, where=lambda m: True, timeout=5.0):
        """The first message of this kind that matches, waiting for it. Raises TimeoutError if it doesn't come."""
        end = time.monotonic() + timeout
        with self.changed:
            while True:
                for m in self.messages:
                    if m.get("t") == kind and where(m):
                        return m
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"no {kind} message came")
                self.changed.wait(left)

    def wait_until(self, check, timeout=5.0):
        """Waits for check() to be true, as messages come in. Raises TimeoutError if it isn't in time."""
        end = time.monotonic() + timeout
        with self.changed:
            while not check():
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError("it didn't happen in time")
                self.changed.wait(left)

    def wait_connected(self, timeout=5.0):
        return self.wait_for("hello", timeout=timeout)

    def clear(self):
        with self.changed:
            self.messages.clear()

    def drop(self):
        """Ends the connection, as a board going out of range does. The app reconnects."""
        conn, self.conn = self.conn, None
        if conn:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def stop(self):
        self.stopping = True
        self.drop()
        self.srv.close()
        self.thread.join(timeout=2)


def summary(msg):
    if msg.get("t") == "lock_code":
        return "lock_code (removed)" if msg.get("clear") else "lock_code (a new code's hash)"
    if msg.get("t") == "state":
        return f"state: toggl {msg.get('toggl')}, voice {msg.get('voice')}, {msg.get('n')} agents"
    if msg.get("t") == "agents":
        rows = [x.get("name") or x["id"] for x in msg.get("rows", [])]
        if "page" in msg:
            return f"agents: page {msg['page'] + 1} of {msg['pages']}, {len(rows)} rows"
        return f"agents: changed {rows}, gone {msg.get('gone', [])}"
    return json.dumps({k: v for k, v in msg.items() if k != "_len"}, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser(description="A stand-in for a board, over TCP.")
    ap.add_argument("--port", type=int, default=7777)
    ap.add_argument("--uid", help="8 hex digits; random if you don't say")
    ap.add_argument("--old-build", action="store_true", help="answer with no uid, as a build from before uids would")
    a = ap.parse_args()

    def show(msg):
        print(summary(msg), flush=True)
        if msg.get("t") == "state":
            print(f"  it holds {len(board.agents)} agents", flush=True)

    board = FakeBoard(a.port, uid=a.uid, old_build=a.old_build)
    board.on_message = show
    print(f"fake board on 127.0.0.1:{board.port}, uid {'none' if a.old_build else board.uid}", flush=True)
    for line in sys.stdin:
        line = line.strip()
        try:
            if line.startswith("tap "):
                start = line[4:].lower()
                hit = next((i for i, x in list(board.agents.items()) if x.get("name", "").lower().startswith(start)), None)
                if hit:
                    board.send({"t": "tap", "id": hit})
                else:
                    print("no such agent")
            elif line:
                board.send(json.loads(line))
        except (ValueError, AttributeError, OSError) as e:
            print(f"not sent: {type(e).__name__}")


if __name__ == "__main__":
    main()
