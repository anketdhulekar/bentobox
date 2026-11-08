"""What one connection's board holds of the agents, and what it's owed next. See `agents` in PROTOCOL.md.

There's one AgentSync per connection, made when the board answers `hello`, so a new connection starts clean. The link
asks due() for a line each time round its loop, on its own thread, which is the only one that uses this. Rows are made
when they're sent, not when they change, so what waits is only an id: a later change has nothing to replace, and `age`
and `up` are always as of the sending.
"""
import time

from link.framing import AGENTS_BYTES_PER_S, AGENTS_LINE_MAX, encode, split_rows

PAGE_OVERHEAD = len(encode({"t": "agents", "page": 99999, "pages": 99999, "rows": []}))
CHANGES_BYTES = 2000     # a line of changes stops here, a quarter of a second's worth, so `needs` soon has its turn
GONE_PER_LINE = CHANGES_BYTES // 11  # an id takes 11 bytes, with its quotes and comma
LOOK_EVERY_S = 1         # `where` can change with no change in the zoo's version
OVERDUE_S = 5            # a change that has waited this long leads the next line, and a page this long late goes next
LIST_S_AT_MOST = 1000    # longer than any whole list takes: what a page's `age` and `up` can have grown by when it goes


def stamped(row, age, up):
    """A row as it's sent. Zero means the same as missing, as for its other fields."""
    return {**row, **({"age": age} if age else {}), **({"up": up} if up else {})}


class AgentSync:
    def __init__(self, zoo, where_of, clock=time.monotonic):
        self.zoo, self.where_of, self.clock = zoo, where_of, clock
        self.free_at = 0.0  # no `agents` line before this. It outlasts restart(), so a `resync` can't beat the pace.
        self.restart()

    def restart(self):
        """The whole list again, from page 0: after `board` and after `resync`. Whatever was waiting is in it."""
        self.held = {}          # id -> the row the board last got. Nothing goes out for an agent that still matches it.
        self.named = set()      # the ids this whole list's pages have named
        self.waiting = {}       # id -> when its change began waiting. A later change doesn't reset this.
        self.gone = []          # ids to tell the board about
        self.page = 0           # the next page of the whole list, or None once the last has gone
        self.page_at = None     # when the last page went
        self.plan = None        # how many rows each of its pages takes; fixed by its first
        self.looked_version = self.looked_at = None

    def due(self):
        """The next `agents` message, if one is owed and the pace allows it."""
        now = self.clock()
        if now < self.free_at:
            return None
        msg = self._during_list(now) if self.page is not None else self._next_changes(now)
        if msg:
            self.free_at = now + len(encode(msg)) / AGENTS_BYTES_PER_S
        return msg

    def _during_list(self, now):
        rows = self.zoo.rows(self.where_of)
        return self._needs_before_page(rows, now) or self._next_page(rows, now)

    def _needs_before_page(self, rows, now):
        """An agent that needs you doesn't wait for the whole list: its row goes ahead of the next page, as a change. The
        board counts it as named by the list, and its page names it again later. Only this goes between pages: `gone` and
        other changes wait for the last one.

        This one case breaks the board's oldest-first order until the next whole list: an agent new to the board goes
        straight to the end of its list, ahead of older agents not sent yet.

        Page 0 goes first, because it begins the list. And a page that's OVERDUE_S late goes next, so that agents going in
        and out of `needs` can't hold the list up for ever."""
        if self.page == 0 or now - self.page_at >= OVERDUE_S:
            return None
        needing = []
        for item in rows:
            row = item[0]
            if row["st"] == "needs" and self.held.get(row["id"]) != row:
                self.waiting.setdefault(row["id"], now)
                needing.append(item)
            else:
                self.waiting.pop(row["id"], None)
        needing.sort(key=lambda item: self.waiting[item[0]["id"]])
        return self._changes_line(needing) if needing else None

    def _next_page(self, rows, now):
        """Each page takes the first agents not named yet, as they are now. So an agent that ended leaves room for the
        next, and anything that doesn't fit by the last page goes as a change after it, still in order."""
        rows = [item for item in rows if item[0]["id"] not in self.named]
        if self.plan is None:
            grown = [stamped(row, age + LIST_S_AT_MOST, up + LIST_S_AT_MOST) for row, age, up in rows]
            self.plan = [len(p) for p in split_rows(grown, PAGE_OVERHEAD, AGENTS_LINE_MAX)]
        rows = rows[:self.plan[self.page]]
        sent = split_rows([stamped(*item) for item in rows], PAGE_OVERHEAD, AGENTS_LINE_MAX)[0]
        for row, _, _ in rows[:len(sent)]:
            self.held[row["id"]] = row
            self.named.add(row["id"])
        msg = {"t": "agents", "page": self.page, "pages": len(self.plan), "rows": sent}
        self.page = self.page + 1 if self.page + 1 < len(self.plan) else None
        self.page_at = now
        return msg

    def _next_changes(self, now):
        version = self.zoo.version
        idle = not self.waiting and not self.gone
        if idle and version == self.looked_version and now - self.looked_at < LOOK_EVERY_S:
            return None
        self.looked_version, self.looked_at = version, now
        rows = self.zoo.rows(self.where_of)
        self._compare(rows, now)
        if self.gone:  # on a line of their own, so that a full board has room before any new row arrives
            ids, self.gone = self.gone[:GONE_PER_LINE], self.gone[GONE_PER_LINE:]
            return {"t": "agents", "rows": [], "gone": ids}
        return self._changes_line(self._in_order(rows, now)) if self.waiting else None

    def _changes_line(self, order):
        line, size = [], PAGE_OVERHEAD
        for row, age, up in order:
            sent = stamped(row, age, up)
            n = len(encode(sent))
            if line and size + n > CHANGES_BYTES:
                break
            line.append(sent)
            size += n
            self.held[row["id"]] = row
            self.waiting.pop(row["id"], None)
        return {"t": "agents", "rows": line}

    def _compare(self, rows, now):
        ids = set()
        for row, _, _ in rows:
            ids.add(row["id"])
            if self.held.get(row["id"]) == row:
                self.waiting.pop(row["id"], None)  # it changed back before it could go
            else:
                self.waiting.setdefault(row["id"], now)
        for i in [i for i in self.held if i not in ids]:
            del self.held[i]
            self.gone.append(i)
        for i in [i for i in self.waiting if i not in ids]:
            del self.waiting[i]

    def _in_order(self, rows, now):
        """What's waiting after the whole list, in the order it should go: agents that need you, then changes of `st`,
        then the rest, and in each the longest wait first. A new agent counts as a change of `st`. It goes to the end of
        the board's list, so new agents go oldest first, and one that needs you takes the older new ones along."""
        new, needs, st, rest = [], [], [], []
        for item in rows:
            row = item[0]
            if row["id"] not in self.waiting:
                continue
            held = self.held.get(row["id"])
            if held is None:
                new.append(item)
            elif row["st"] == "needs":
                needs.append(item)
            elif row["st"] != held["st"]:
                st.append(item)
            else:
                rest.append(item)
        for group in (needs, st, rest):
            group.sort(key=lambda item: self.waiting[item[0]["id"]])
        pulled = max((i + 1 for i, item in enumerate(new) if item[0]["st"] == "needs"), default=0)
        order = new[:pulled] + needs + new[pulled:] + st + rest
        oldest = min(order, key=lambda item: self.waiting[item[0]["id"]])
        if now - self.waiting[oldest[0]["id"]] >= OVERDUE_S:
            lead = new[0] if oldest in new else oldest
            order.remove(lead)
            order.insert(0, lead)
        return order
