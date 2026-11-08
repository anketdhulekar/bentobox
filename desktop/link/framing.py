"""The wire format: one JSON object per line, at most 16 KB, and 12 KB for `agents` lines."""
import json
import re

VERSION = 4
LINE_MAX = 16384              # bytes, with the newline
PAGE_BYTES = 16000            # list pages stay a little under the limit
AGENTS_LINE_MAX = 12000       # bytes, with the newline. It holds whichever way someone counts "12 KB".
AGENTS_BYTES_PER_S = 8000     # the pace of `agents` lines
SERVICE_UUID = "62656e74-6f00-0000-0000-000000000000"  # "bento" in ASCII
RX_UUID = "62656e74-6f00-0000-0000-000000000001"       # the desktop app writes here
TX_UUID = "62656e74-6f00-0000-0000-000000000002"       # the board notifies here


UNKNOWN_BOARD = "unknown"     # for a `board` with no valid uid; not 8 hex digits, so no real uid can equal it


def parse_uid(value):
    """A `board` message's uid, always in the same form, or None if it isn't 8 hex digits."""
    if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8}", value):
        return value.lower()
    return None


def encode(msg):
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "replace") + b"\n"


def chunks(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)]


class LineBuffer:
    """Splits a byte stream into lines. A line over the limit is dropped, whole."""

    def __init__(self, limit=LINE_MAX):
        self.buf, self.limit, self.skip = bytearray(), limit, False

    def feed(self, data):
        self.buf += data
        lines = []
        while True:
            end = self.buf.find(b"\n")
            if end < 0:
                break
            line = bytes(self.buf[:end])
            del self.buf[:end + 1]
            if self.skip:
                self.skip = False
            elif len(line) + 1 <= self.limit:
                lines.append(line)
        if len(self.buf) > self.limit:
            self.buf.clear()
            self.skip = True
        return lines


def split_rows(rows, overhead, limit):
    """Rows into pages whose lines stay under the limit, given what a line takes besides its rows. An empty list is
    still one page, because a receiver acts when the last page arrives."""
    pages, page, size = [], [], overhead
    for row in rows:
        n = len(encode(row))  # its newline stands in for the comma
        if page and size + n > limit:
            pages.append(page)
            page, size = [], overhead
        page.append(row)
        size += n
    pages.append(page)
    return pages


def paginate(kind, req, rows, limit=PAGE_BYTES):
    """One `projects` or `tasks` message per page."""
    overhead = len(encode({"t": kind, "req": req, "page": 99999, "pages": 99999, "rows": []}))
    pages = split_rows(rows, overhead, limit)
    return [{"t": kind, "req": req, "page": i, "pages": len(pages), "rows": rows} for i, rows in enumerate(pages)]
