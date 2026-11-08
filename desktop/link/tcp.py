"""The protocol over TCP: the app connects to the simulator (or fake_board.py), which listens.

One TcpLink is one board. Several simulators listen on several ports, and the app runs a TcpLink for each
(`display_port` can be a list), as it keeps several Bluetooth boards in step.

TCP carries everything in clear text, with no pairing, so it stays on this Mac: a `display_host` that isn't a loopback
address is refused unless the config also says `"allow_remote_display": true`.
"""
import socket

from link.base import Link
from link.framing import LineBuffer
from util import log


def ports(cfg):
    """`display_port`: one port, a list of them, or "7777,7778"."""
    value = cfg.get("display_port", 7777)
    if isinstance(value, (list, tuple)):
        items = value
    elif isinstance(value, str):
        items = [v for v in value.replace(" ", "").split(",") if v]
    else:
        items = [value]
    out = []
    for item in items:
        try:
            port = int(item)
        except (TypeError, ValueError):
            continue
        if 0 < port < 65536 and port not in out:
            out.append(port)
    return out or [7777]


def host_allowed(cfg):
    host = str(cfg.get("display_host") or "")
    return host in ("localhost", "::1") or host.startswith("127.") or cfg.get("allow_remote_display") is True


class TcpLink(Link):
    trusted_link = True  # a simulator or fake board on this Mac, by its own config

    def __init__(self, cfg, backend, port=None):
        super().__init__(cfg, backend)
        self.port = int(port if port is not None else ports(cfg)[0])

    def run(self, stop):
        if not host_allowed(self.cfg):
            log(f"board: {self.cfg.get('display_host')!s:.40} isn't this Mac, and TCP isn't encrypted: not connecting. "
                "Set \"allow_remote_display\": true in the config to allow it.")
            return
        backoff, said_missing = self.reconnect_min_s, False
        while not stop.is_set():
            was_connected = False
            try:
                was_connected = self._session(stop)
            except OSError:
                was_connected = self.connected
            except Exception as e:
                was_connected = self.connected
                log(f"board: {type(e).__name__} on the link")
            finally:
                self._end_session()
            if was_connected:
                backoff, said_missing = self.reconnect_min_s, False
            elif not said_missing:
                log(f"board: nothing listens on {self.cfg['display_host']}:{self.port} yet")
                said_missing = True
            if self.busy:
                log(f"board: busy ({self.busy}); trying again in {self.busy_wait_s} s")
                self.busy = None
                stop.wait(self.busy_wait_s)
                continue
            stop.wait(backoff)
            backoff = min(self.reconnect_max_s, backoff * 2)

    def _session(self, stop):
        host, port = self.cfg["display_host"], self.port
        sock = socket.create_connection((host, port), timeout=5)
        try:
            sock.settimeout(5)
            sock.sendall(self._line(self.backend.hello()))
            self._start_session()
            log(f"board: connected to {host}:{port} (TCP)")
            lines = LineBuffer()
            while not stop.is_set() and not self.busy:
                agents = self.backend.agents_due(self)
                if agents:
                    self._write(sock, agents)
                state = self._state_due()
                if state:
                    self._write(sock, state)
                for msg, _reliable, on_sent in self._take_queue():
                    self._write(sock, msg)
                    self._sent(on_sent)
                sock.settimeout(0.02)
                try:
                    data = sock.recv(65536)
                    if not data:
                        raise ConnectionResetError("closed by the board")
                    for line in lines.feed(data):
                        self.handle(line)
                except socket.timeout:
                    pass
                sock.settimeout(5)
            return True
        finally:
            sock.close()

    def _write(self, sock, msg):
        line = self._line(msg)
        if line:
            sock.settimeout(5)
            sock.sendall(line)
