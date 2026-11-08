"""The protocol over Bluetooth LE, with bleak: finds every board by its service, connects to each and stays connected.

The first connection from a new Mac pairs: the board shows a 6-digit code on its screen only, and macOS asks for it.
Later connections encrypt with the saved keys. A board connected to a Mac doesn't advertise, so a scan finds only the
boards that are free. Each board found gets a BoardLink of its own, with its own session, queue and backoff: one board
going away, or sending this Mac away, doesn't touch the others.
"""
import asyncio
import time

from link.base import RECONNECT_MIN_S, SCAN_S, Link
from link.framing import RX_UUID, SERVICE_UUID, TX_UUID, LineBuffer, chunks
from util import log, short

PAIRING_WAIT_S = 120
SCAN_CONNECTED_S = 5   # with a board connected, look for others in shorter scans, with a rest between
REST_S = 5


def is_board(service_uuids):
    return SERVICE_UUID in [u.lower() for u in (service_uuids or [])]


class BoardLink(Link):
    """One board over Bluetooth, by its address (macOS gives each board a stable one)."""

    def __init__(self, cfg, backend, address):
        super().__init__(cfg, backend)
        self.address = address
        self.task = None
        self.next_try = 0.0
        self.backoff = self.reconnect_min_s
        self.said_pair = False

    async def run_once(self, device, stop):
        """One connection, from connecting until it ends. Never raises."""
        from bleak import BleakClient
        sessions = self.session
        try:
            async with BleakClient(device, timeout=20.0) as client:
                await self._session(client, stop)
            self.backoff = self.reconnect_min_s
        except Exception as e:  # bleak raises several kinds; keep trying
            had_session = self.session != sessions
            log(f"board: {'lost the link' if had_session else 'no connection'} ({type(e).__name__}: {short(str(e), 100)})")
            if not had_session and not self.said_pair:
                log("board: if this Mac isn't paired yet, choose Settings > Pair a new Mac on the board")
                self.said_pair = True
            if had_session:
                self.backoff = self.reconnect_min_s
        finally:
            self._end_session()
        if self.busy:
            log(f"board: busy ({self.busy}); trying again in {self.busy_wait_s} s")
            self.busy = None
            self.next_try = time.monotonic() + self.busy_wait_s
        else:
            self.next_try = time.monotonic() + self.backoff
            self.backoff = min(self.reconnect_max_s, self.backoff * 2)

    async def _session(self, client, stop):
        lines = LineBuffer()
        early = []  # the board can answer before the hello's write returns: those lines wait for the session to start

        def on_notify(_char, data):
            for line in lines.feed(bytes(data)):
                if early is None:
                    self.handle(line)
                else:
                    early.append(line)

        # Both need a paired, encrypted link. The first time, macOS pairs here (the board shows the code): give that up
        # to two minutes. The write has a response, so it's what triggers pairing.
        deadline = time.time() + PAIRING_WAIT_S
        while True:
            try:
                await client.start_notify(TX_UUID, on_notify)
                await self._write(client, self.backend.hello(), response=True)
                break
            except Exception:
                if time.time() > deadline or not client.is_connected:
                    raise
                await asyncio.sleep(RECONNECT_MIN_S)
        self._start_session()
        log("board: connected over Bluetooth")
        try:
            for line in early:
                self.handle(line)
            early = None
            while client.is_connected and not stop.is_set() and not self.busy:
                agents = self.backend.agents_due(self)
                if agents:
                    await self._write(client, agents)
                state = self._state_due()
                if state:
                    await self._write(client, state)
                for msg, reliable, on_sent in self._take_queue():
                    await self._write(client, msg, response=reliable)
                    self._sent(on_sent)
                await asyncio.sleep(0.05)
        finally:
            self._end_session()

    async def _write(self, client, msg, response=False):
        line = self._line(msg)
        if not line:
            return
        char = client.services.get_characteristic(RX_UUID)
        size = max(20, char.max_write_without_response_size)
        for part in chunks(line, size):
            await client.write_gatt_char(char, part, response=response)


class BleLink:
    """Looks for boards and keeps a BoardLink running for each one it finds."""

    def __init__(self, cfg, backend):
        self.cfg, self.backend = cfg, backend
        self.links = {}  # address -> BoardLink

    def run(self, stop):
        try:
            import bleak  # noqa: F401
        except ImportError:
            log("board: Bluetooth needs bleak; run Bentobox with desktop/.venv/bin/python (see desktop/README.md)")
            return
        asyncio.run(self._main(stop))

    def _busy_links(self):
        return [link for link in self.links.values() if link.task and not link.task.done()]

    async def _main(self, stop):
        from bleak import BleakScanner
        said_missing = False
        while not stop.is_set():
            running = self._busy_links()
            connected = any(link.connected for link in running)
            try:
                found = await BleakScanner.discover(timeout=SCAN_CONNECTED_S if connected else SCAN_S, return_adv=True)
            except Exception as e:  # Bluetooth off, or no permission: say so once a while, and keep trying
                log(f"board: can't scan ({type(e).__name__}: {short(str(e), 80)})")
                await self._sleep(stop, SCAN_S)
                continue
            boards = {addr: dev for addr, (dev, adv) in found.items() if is_board(adv.service_uuids)}
            now = time.monotonic()
            for address, device in boards.items():
                link = self.links.get(address)
                if link is None:
                    link = self.links[address] = BoardLink(self.cfg, self.backend, address)
                if (link.task and not link.task.done()) or now < link.next_try:
                    continue
                link.task = asyncio.ensure_future(link.run_once(device, stop))
            if boards or self._busy_links():
                said_missing = False
            elif not said_missing:
                log("board: no Bento board found yet (is it on, near, and free of another Mac?)")
                said_missing = True
            if any(link.connected for link in self._busy_links()):
                await self._sleep(stop, REST_S)
        for link in self._busy_links():
            try:
                await asyncio.wait_for(link.task, timeout=5)
            except Exception:
                pass

    @staticmethod
    async def _sleep(stop, seconds):
        end = time.monotonic() + seconds
        while not stop.is_set() and time.monotonic() < end:
            await asyncio.sleep(min(0.25, seconds))
