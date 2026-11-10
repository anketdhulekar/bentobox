"""The `agents` message: the whole list in pages, then only what changes, at a pace. And `state`, which no longer carries
agents, and agent tracking at 500 agents. The tests move the clock themselves, so the pace takes no real time."""
import time
import unittest
from unittest import mock

import helpers
from helpers import Clock, FakeLink, HomeCase, connect, disconnect, make_backend

from agents import rex as rex_mod
from agents.herdr import Herdr
from agents import sources
from agents.sync import CHANGES_BYTES, LOOK_EVERY_S, OVERDUE_S, AgentSync
from agents.zoo import MAX_AGENTS, Agent, Zoo, short_id
from fake_board import FakeBoard
from link.base import KEEPALIVE_S, Link
from link.framing import AGENTS_BYTES_PER_S, AGENTS_LINE_MAX, encode

LOOP_S = 0.05  # how often a link's loop asks


def uid_of(i):
    return short_id(f"s{i}")


class Case(HomeCase):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.backend = make_backend(clock=self.clock)
        self.zoo = self.backend.zoo

    def agent(self, i, activity="Working", **kw):
        self.zoo.on_hook(dict({"event": "PreToolUse", "session_id": f"s{i}", "project": "p", "activity": activity}, **kw))

    def agents(self, n, **kw):
        for i in range(n):
            self.agent(i, **kw)
            self.clock.advance(1)  # each older than the next

    def needs(self, i):
        self.zoo.on_hook({"event": "PermissionRequest", "session_id": f"s{i}", "activity": "Needs permission: Bash"})

    def done(self, i):
        self.zoo.on_hook({"event": "Stop", "session_id": f"s{i}"})

    def end(self, i):
        self.zoo.on_hook({"event": "SessionEnd", "session_id": f"s{i}"})

    def run_for(self, seconds, each=None):
        """A link's loop, for this long: what it wrote, as (when, message)."""
        out, end = [], self.clock() + seconds
        while self.clock() < end:
            if each:
                each()
            msg = self.backend.agents_due()
            if msg:
                out.append((self.clock(), msg))
            self.clock.advance(LOOP_S)
        return out

    def messages(self, seconds=2):
        return [m for _, m in self.run_for(seconds)]

    def whole_list(self):
        """Connects and lets the whole list go, and returns its pages."""
        self.link = connect(self.backend)
        return self.until_listed()

    def next_line(self):
        """The loop's next line, as (when, message), however long the pace makes it wait."""
        for _ in range(100000):
            msg = self.backend.agents_due()
            if msg:
                return self.clock(), msg
            self.clock.advance(LOOP_S)
        self.fail("no line came")

    def until_listed(self):
        """The rest of the whole list: its pages. The lines that went between them are in self.between."""
        pages, self.between = [], []
        while not pages or pages[-1]["page"] < pages[-1]["pages"] - 1:
            msg = self.next_line()[1]
            (pages if "page" in msg else self.between).append(msg)
        return pages

    def assert_paced(self, sent):
        """However you look at it, these lines keep to the pace, give or take the line that's going out."""
        times = [t for t, _ in sent]
        sizes = [len(encode(m)) for _, m in sent]
        for i in range(len(sent)):
            total = 0
            for j in range(i, len(sent)):
                total += sizes[j]
                self.assertLessEqual(total, AGENTS_BYTES_PER_S * (times[j] - times[i]) + max(sizes) + 1)

    def choke(self):
        """Uses up the pace, as a big line would, so that what happens next has to wait."""
        self.backend._board().agents.free_at = self.clock() + 10


def ids(messages):
    return [r["id"] for m in messages for r in m["rows"]]


class WholeList(Case):
    def test_nothing_goes_until_the_board_has_answered(self):
        self.agents(3)
        connect(self.backend, board=False)
        self.assertEqual(self.messages(), [])
        self.backend.on_message({"t": "board", "uid": helpers.UID})
        self.assertEqual(self.backend.agents_due()["page"], 0)

    def test_500_agents_with_long_non_ascii_names_fit_in_pages_oldest_first(self):
        for i in range(500):
            self.zoo.on_hook({"event": "PreToolUse", "session_id": f"s{i}", "project": "プ" * 40, "activity": "編" * 48,
                              "title": "名" * 40, "rex_session": "session:1", "rex_block": "block:1"})
            self.zoo.agents[f"s{i}"].name = "名前" * 30
            for j in range(6):
                self.zoo.on_hook({"event": "SubagentStart", "session_id": f"s{i}", "agent_id": f"g{j}", "agent_type": "型" * 24})
            self.clock.advance(1)
        pages = self.whole_list()
        self.assertGreater(len(pages), 1)
        self.assertEqual([p["page"] for p in pages], list(range(len(pages))))
        self.assertEqual({p["pages"] for p in pages}, {len(pages)})
        for page in pages:
            self.assertLessEqual(len(encode(page)), AGENTS_LINE_MAX)
            self.assertEqual(set(page), {"t", "page", "pages", "rows"})
        self.assertEqual(ids(pages), [uid_of(i) for i in range(500)])
        self.assertEqual(pages[0]["rows"][0]["name"], "名前" * 19 + "名…")
        self.assertEqual(len(pages[0]["rows"][0]["subs"]), 6)

    def test_plain_rows_fill_a_page_with_about_40_agents(self):
        for i in range(500):
            self.agent(i, activity="Editing main.c", project="api-server", rex_session="session:1", rex_block="block:1")
        self.backend.where_of = lambda a: "Rex session “work”"
        pages = self.whole_list()
        self.assertGreaterEqual(len(pages[0]["rows"]), 40)
        self.assertLessEqual(len(encode(pages[0])), AGENTS_LINE_MAX)

    def test_a_row_at_every_cap_fits_a_line_several_times(self):
        """Each field has its own cap, so no row can outgrow a line. Worst of all are control characters: JSON writes each
        in 6 bytes."""
        a = Agent("s", 0)
        worst = "\x01" * 100
        a.name = a.project = a.activity = a.notice = worst
        a.rex_block = "block:1"
        a.agent = "codex"
        for i in range(10):
            a.subs[f"g{i}"] = {"type": worst, "activity": worst}
        row = a.public(100000, worst)
        self.assertEqual((len(row["name"]), len(row["act"]), len(row["subs"])), (40, 48, 6))
        self.assertLess(len(encode(row)), AGENTS_LINE_MAX / 2)
        self.assertLess(len(encode(dict(row, name="😀" * 40, act="😀" * 48))), AGENTS_LINE_MAX / 2)

    def test_no_agents_is_still_one_page(self):
        self.assertEqual(self.whole_list(), [{"t": "agents", "page": 0, "pages": 1, "rows": []}])

    def test_a_row_leaves_out_what_is_empty(self):
        self.agent(0)
        row = self.whole_list()[0]["rows"][0]
        self.assertEqual(set(row), {"id", "name", "proj", "kind", "st", "act"})  # no note, where, pane, subs, age or up
        self.clock.advance(5)
        self.backend.on_message({"t": "resync"})
        row = self.backend.agents_due()["rows"][0]
        self.assertEqual((row["age"], row["up"]), (5, 5))

    def test_a_row_says_which_agent_it_is_only_when_it_isnt_claude(self):
        self.agent(0, agent="codex")
        self.agent(1)
        rows = self.whole_list()[0]["rows"]
        self.assertEqual(rows[0]["agent"], "codex")
        self.assertNotIn("agent", rows[1])

    def test_age_and_up_are_as_of_the_sending(self):
        for i in range(200):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.clock.advance(100)
        pages = self.whole_list()
        self.assertGreater(len(pages), 2)
        self.assertEqual(pages[0]["rows"][0]["up"], 100)
        self.assertGreater(pages[-1]["rows"][0]["up"], 101)

    def test_a_change_during_the_list_goes_in_its_page_if_that_has_yet_to_go(self):
        for i in range(200):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        first = self.backend.agents_due()
        self.assertGreater(first["pages"], 1)
        self.agent(199, activity="Changed")
        self.needs(198)
        pages = [first] + self.until_listed()
        rows = {r["id"]: r for p in pages for r in p["rows"]}
        self.assertEqual(rows[uid_of(199)]["act"], "Changed")
        self.assertEqual(rows[uid_of(198)]["st"], "needs")
        self.assertEqual(self.messages(), [])  # nothing more to say about them

    def test_a_change_to_an_agent_already_sent_waits_for_the_list_to_end(self):
        for i in range(200):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        first = self.backend.agents_due()
        self.agent(0, activity="Changed")
        self.end(1)
        pages = self.until_listed()
        self.assertTrue(all("page" in p and "gone" not in p for p in pages))
        self.assertEqual(ids([first] + pages).count(uid_of(0)), 1)
        after = self.messages()
        self.assertEqual(after[0]["gone"], [uid_of(1)])
        self.assertEqual([(r["id"], r["act"]) for r in after[1]["rows"]], [(uid_of(0), "Changed")])
        self.assertTrue(all("page" not in m for m in after))

    def test_an_agent_that_ends_before_its_page_is_never_named_and_a_new_one_joins(self):
        for i in range(200):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        first = self.backend.agents_due()
        self.end(150)
        self.agent(200)
        pages = [first] + self.until_listed()
        named = ids(pages)
        self.assertNotIn(uid_of(150), named)
        self.assertEqual(named[-1], uid_of(200))
        self.assertEqual(len(named), len(set(named)))
        self.assertEqual({p["pages"] for p in pages}, {first["pages"]})
        self.assertEqual(self.messages(), [])  # the list has said it all

    def test_what_outgrows_the_last_page_follows_as_changes_in_order(self):
        for i in range(60):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        first = self.backend.agents_due()
        for i in range(60, 100):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        pages = [first] + self.until_listed()
        self.assertEqual(len(pages), first["pages"])
        self.assertEqual(ids(pages), [uid_of(i) for i in range(60)])
        self.assertEqual(ids(pages + self.messages(30)), [uid_of(i) for i in range(100)])

    def test_a_list_that_takes_long_still_names_everyone_though_age_and_up_grow(self):
        """Their digits take more room as it goes, and no agent may be pushed past the last page because of that."""
        for i in range(500):
            self.agent(i, project="プ" * 40, activity="編" * 48)  # all with no `age` or `up` yet
        pages = self.whole_list()
        self.assertEqual(ids(pages), [uid_of(i) for i in range(500)])
        self.assertTrue(all(p["rows"] for p in pages))
        self.assertEqual(self.messages(), [])

    def test_pages_stay_in_their_count_when_agents_end(self):
        for i in range(200):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        first = self.backend.agents_due()
        for i in range(30, 200):
            self.end(i)
        pages = self.until_listed()
        self.assertEqual([p["page"] for p in pages], list(range(1, first["pages"])))
        self.assertEqual(pages[-1]["rows"], [])  # so the last page still comes, and ends the list


class NeedsDuringTheList(Case):
    """An agent that needs you doesn't wait for the whole list. Nothing else does: see WholeList."""

    def long_list(self, n=200):
        for i in range(n):
            self.agent(i, project="プ" * 40, activity="編" * 48)
            self.clock.advance(1)
        self.link = connect(self.backend)

    def test_a_needs_raised_during_a_list_of_500_goes_before_the_next_page_and_within_the_pace(self):
        self.long_list(500)
        sent = [self.next_line(), self.next_line()]
        self.assertEqual([m["page"] for _, m in sent], [0, 1])
        pages = sent[0][1]["pages"]
        self.assertGreater(pages, 5)
        self.clock.advance(LOOP_S)
        self.needs(0)    # its page has gone
        self.needs(499)  # its page is far off
        sent.append(self.next_line())
        when, line = sent[-1]
        self.assertEqual(set(line), {"t", "rows"})
        self.assertEqual({(r["id"], r["st"]) for r in line["rows"]}, {(uid_of(0), "needs"), (uid_of(499), "needs")})
        self.assertEqual(self.zoo.agents["s499"].state, "needs")
        page_1_ends = sent[1][0] + len(encode(sent[1][1])) / AGENTS_BYTES_PER_S
        self.assertGreaterEqual(when, page_1_ends)       # within the pace: it waited for the line before it
        self.assertLess(when, page_1_ends + 2 * LOOP_S)  # and for nothing else
        while sent[-1][1].get("page") != pages - 1:
            sent.append(self.next_line())
        self.assertEqual([m.get("page") for _, m in sent[3:]], list(range(2, pages)))
        self.assert_paced(sent)
        self.assertEqual(ids(m for _, m in sent if "page" in m), [uid_of(i) for i in range(500)])  # its page names it too

    def test_its_page_still_names_it_and_then_nothing_more_is_said(self):
        self.long_list()
        self.next_line()
        self.needs(199)
        self.assertEqual(ids([self.next_line()[1]]), [uid_of(199)])
        pages = self.until_listed()
        self.assertEqual(self.between, [])
        rows = {r["id"]: r for p in pages for r in p["rows"]}
        self.assertEqual(rows[uid_of(199)]["st"], "needs")
        self.assertEqual(self.messages(), [])

    def test_one_new_to_the_board_goes_at_once_with_no_older_ones_and_the_board_keeps_it(self):
        board = FakeBoard()
        self.addCleanup(board.stop)
        self.long_list()
        first = self.next_line()[1]
        board._take_agents(first)
        self.needs(150)                  # older agents not sent yet, and this one not sent either
        self.agent(900)                  # and one with no page to name it, as the pages are all planned
        self.needs(900)
        line = self.next_line()[1]
        self.assertEqual(ids([line]), [uid_of(150), uid_of(900)])
        board._take_agents(line)
        for msg in self.until_listed() + self.messages():
            board._take_agents(msg)
        self.assertEqual(set(board.agents), {a["id"] for a in self.zoo.public()["agents"]})
        order = list(board.agents)
        self.assertLess(order.index(uid_of(150)), order.index(uid_of(149)))  # out of order: the price of this

    def test_gone_and_other_changes_still_wait_for_the_last_page(self):
        self.long_list()
        self.next_line()
        self.end(0)
        self.agent(1, activity="Only the activity")
        self.done(2)
        self.needs(3)
        self.assertEqual(ids([self.next_line()[1]]), [uid_of(3)])
        self.until_listed()
        self.assertEqual(self.between, [])
        first, second = self.messages()
        self.assertEqual(first["gone"], [uid_of(0)])
        self.assertEqual(ids([second]), [uid_of(2), uid_of(1)])

    def test_those_already_waiting_when_the_list_begins_go_right_after_page_0(self):
        for i in range(200):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.needs(199)
        self.needs(100)
        self.link = connect(self.backend)
        self.assertEqual(self.next_line()[1]["page"], 0)
        self.assertEqual(sorted(ids([self.next_line()[1]])), sorted([uid_of(199), uid_of(100)]))
        self.assertEqual(self.next_line()[1]["page"], 1)

    def test_all_of_them_go_ahead_of_the_next_page_however_many_lines_that_takes(self):
        self.long_list()
        self.next_line()
        for i in range(100, 130):
            self.needs(i)
        lines = []
        while "page" not in (msg := self.next_line()[1]):
            lines.append(msg)
        self.assertGreater(len(lines), 1)
        self.assertEqual(sorted(ids(lines)), sorted(uid_of(i) for i in range(100, 130)))
        self.assertEqual(msg["page"], 1)

    def test_a_row_goes_once_until_it_changes_again(self):
        self.long_list()
        self.next_line()
        self.needs(150)
        self.assertEqual(ids([self.next_line()[1]]), [uid_of(150)])
        self.assertEqual(self.next_line()[1]["page"], 1)
        self.zoo.on_hook({"event": "Notification", "session_id": "s150", "needs": True, "notice": "Still here"})
        line = self.next_line()[1]
        self.assertEqual([(r["id"], r["note"]) for r in line["rows"]], [(uid_of(150), "Still here")])
        self.assertEqual(self.next_line()[1]["page"], 2)

    def test_one_that_stops_needing_you_before_it_can_go_goes_with_its_page(self):
        self.long_list()
        self.next_line()
        self.needs(150)
        self.zoo.on_hook({"event": "UserPromptSubmit", "session_id": "s150"})
        pages = self.until_listed()
        self.assertEqual(self.between, [])
        self.assertEqual(ids(pages).count(uid_of(150)), 1)

    def test_one_that_went_and_then_ended_before_its_page_is_gone_after_the_list(self):
        """The board counts its row as named, so the end of the list wouldn't remove it."""
        self.long_list()
        self.next_line()
        self.needs(150)
        self.assertEqual(ids([self.next_line()[1]]), [uid_of(150)])
        self.end(150)
        pages = self.until_listed()
        self.assertNotIn(uid_of(150), ids(pages))
        self.assertEqual(self.messages(), [{"t": "agents", "rows": [], "gone": [uid_of(150)]}])

    def test_one_that_went_and_stopped_needing_you_after_its_page_is_a_change_after_the_list(self):
        self.long_list()
        self.next_line()
        self.needs(0)
        self.assertEqual(ids([self.next_line()[1]]), [uid_of(0)])
        self.done(0)
        self.until_listed()
        self.assertEqual(self.between, [])
        self.assertEqual([(r["id"], r["st"]) for m in self.messages() for r in m["rows"]], [(uid_of(0), "done")])

    def test_agents_going_in_and_out_of_needs_cant_hold_the_list_up(self):
        self.long_list()
        n = [0]

        def flood():
            n[0] += 1
            for i in range(100):
                self.zoo.on_hook({"event": "PermissionRequest", "session_id": f"s{i}", "activity": f"Needs permission: {n[0]}"})
        sent = []
        while not sent or sent[-1][1].get("page") != sent[0][1]["pages"] - 1:
            sent += self.run_for(LOOP_S, flood)
        self.assert_paced(sent)
        pages = [(t, m) for t, m in sent if "page" in m]
        self.assertEqual([m["page"] for _, m in pages], list(range(sent[0][1]["pages"])))
        self.assertGreater(len(sent), 2 * len(pages))  # the agents had their lines too
        for (before, page), (after, _) in zip(pages, pages[1:]):
            self.assertLess(after - before, OVERDUE_S + 1.5)

    def test_a_resync_starts_the_named_ones_over(self):
        self.long_list()
        self.next_line()
        self.needs(150)
        self.next_line()
        self.backend.on_message({"t": "resync"})
        self.assertEqual(self.next_line()[1]["page"], 0)
        self.assertEqual(ids([self.next_line()[1]]), [uid_of(150)])  # the board's to hear again, from a new start
        self.assertEqual(ids(self.until_listed()).count(uid_of(150)), 1)


class Changes(Case):
    def setUp(self):
        super().setUp()
        self.agents(5)
        self.whole_list()

    def test_nothing_goes_when_nothing_changed_as_age_and_up_move_by_themselves(self):
        self.assertEqual(self.messages(60), [])

    def test_a_changed_agent_goes_whole_and_alone(self):
        self.clock.advance(30)
        self.agent(2, activity="Reading a.c")
        msg = self.messages()[0]
        self.assertEqual(set(msg), {"t", "rows"})
        row = msg["rows"][0]
        self.assertEqual(len(msg["rows"]), 1)
        self.assertEqual((row["id"], row["name"], row["proj"], row["kind"], row["st"], row["act"]),
                         (uid_of(2), "p", "p", "i", "working", "Reading a.c"))
        self.assertGreater(row["up"], 30)
        self.assertEqual(self.messages(), [])  # once

    def test_a_field_that_empties_goes_missing_from_the_row(self):
        self.needs(1)
        self.assertEqual(self.messages()[0]["rows"][0]["note"], "Claude needs your permission to use Bash")
        self.zoo.on_hook({"event": "UserPromptSubmit", "session_id": "s1"})
        self.assertNotIn("note", self.messages()[0]["rows"][0])

    def test_every_field_but_age_and_up_counts(self):
        a = self.zoo.agents["s1"]
        for change in (lambda: setattr(a, "pane_name", "otter"), lambda: setattr(a, "project", "other"),
                       lambda: setattr(a, "kind", "background"), lambda: setattr(a, "notice", "Look"),
                       lambda: setattr(a, "rex_block", "block:1"), lambda: setattr(a, "agent", "grok"),
                       lambda: a.subs.update(g={"type": "Plan", "activity": "Going"})):
            with self.subTest():
                change()
                self.clock.advance(LOOK_EVERY_S)
                self.assertEqual(ids(self.messages()), [uid_of(1)])

    def test_a_changed_where_is_noticed_with_no_change_in_the_zoo(self):
        version = self.zoo.version
        self.backend._board().agents.where_of = lambda a: "Rex session “work”" if a.sid == "s3" else ""
        self.assertEqual(self.zoo.version, version)
        msg = self.messages(LOOK_EVERY_S * 2)[0]
        self.assertEqual((msg["rows"][0]["id"], msg["rows"][0]["where"]), (uid_of(3), "Rex session “work”"))

    def test_an_ended_agent_is_gone_once_and_a_new_one_goes_at_the_end(self):
        self.end(0)
        self.agent(9)
        first, second = self.messages()
        self.assertEqual(first, {"t": "agents", "rows": [], "gone": [uid_of(0)]})
        self.assertEqual(ids([second]), [uid_of(9)])
        self.assertNotIn("gone", second)

    def test_an_agent_that_came_and_went_while_waiting_is_never_mentioned(self):
        self.choke()
        self.agent(9)
        self.backend._board().agents.due()  # still choked
        self.end(9)
        self.backend._board().agents.free_at = 0
        self.assertEqual(self.messages(), [])

    def test_new_agents_go_oldest_first_and_a_needy_one_takes_older_ones_along(self):
        self.choke()
        self.zoo.on_poll([{"sessionId": "late", "status": "busy", "startedAt": self.clock() - 500}], complete=False)
        self.agent(9)
        self.needs(9)
        self.needs(2)
        self.backend._board().agents.free_at = 0
        order = ids(self.messages())
        self.assertEqual(order, [short_id("late"), uid_of(9), uid_of(2)])

    def test_an_agent_that_ended_and_came_back_before_the_pace_allowed_is_a_change(self):
        self.choke()
        self.end(2)
        self.agent(2, activity="Back")
        self.backend._board().agents.free_at = 0
        msgs = self.messages()
        self.assertEqual([(r["id"], r["act"]) for r in msgs[0]["rows"]], [(uid_of(2), "Back")])
        self.assertNotIn("gone", msgs[0])


class Coalescing(Case):
    def setUp(self):
        super().setUp()
        self.agents(5)
        self.whole_list()
        self.choke()

    def test_only_the_newest_of_several_changes_goes(self):
        for act in ("One", "Two", "Three"):
            self.agent(1, activity=act)
            self.backend.agents_due()
        self.backend._board().agents.free_at = 0
        msgs = self.messages()
        self.assertEqual([r["act"] for m in msgs for r in m["rows"]], ["Three"])

    def test_it_replaces_a_change_already_in_the_queue(self):
        """One row a line: 1 goes and 2 stays in the queue, and changes there."""
        self.backend._board().agents.free_at = 0
        self.agent(1, activity="Goes first")
        self.agent(2, activity="Replaced")
        with mock.patch("agents.sync.CHANGES_BYTES", 1):
            self.assertEqual(ids([self.backend.agents_due()]), [uid_of(1)])
            self.assertIn(uid_of(2), self.backend._board().agents.waiting)
            self.agent(2, activity="Newest")
            rows = [r for m in self.messages() for r in m["rows"]]
        self.assertEqual([(r["id"], r["act"]) for r in rows], [(uid_of(2), "Newest")])

    def test_a_change_undone_while_waiting_doesnt_go(self):
        sync = self.backend._board().agents
        self.agent(1, activity="Other")
        sync._compare(self.zoo.rows(), self.clock())
        self.assertIn(uid_of(1), sync.waiting)
        self.agent(1, activity="Working")
        sync.free_at = 0
        self.assertEqual(self.messages(), [])
        self.assertEqual(sync.waiting, {})

    def test_a_later_change_doesnt_lose_an_agents_place_in_the_queue(self):
        sync = self.backend._board().agents
        self.agent(1, activity="First")
        sync.free_at = 0
        sync._compare(self.zoo.rows(), self.clock())
        when = sync.waiting[uid_of(1)]
        self.clock.advance(3)
        self.agent(1, activity="Second")
        sync._compare(self.zoo.rows(), self.clock())
        self.assertEqual(sync.waiting[uid_of(1)], when)


class Priority(Case):
    def setUp(self):
        super().setUp()
        self.agents(10)
        self.whole_list()
        self.choke()

    def wave(self):
        self.agent(1, activity="Only the activity")  # first, and still last on the line
        self.done(2)
        self.needs(3)
        self.end(4)
        self.agent(5, activity="Only the activity")
        self.agent(20)
        self.needs(6)

    def test_gone_then_needs_then_changes_of_st_then_the_rest(self):
        self.wave()
        self.backend._board().agents.free_at = 0
        msgs = self.messages()
        self.assertEqual(msgs[0]["gone"], [uid_of(4)])
        self.assertEqual(msgs[0]["rows"], [])
        self.assertEqual(ids(msgs[1:]), [uid_of(3), uid_of(6), uid_of(20), uid_of(2), uid_of(1), uid_of(5)])

    def test_when_each_row_needs_its_own_line_the_order_holds_too(self):
        self.wave()
        self.backend._board().agents.free_at = 0
        with mock.patch("agents.sync.CHANGES_BYTES", 1):
            msgs = self.messages(5)
        self.assertEqual([m["gone"] if "gone" in m else [r["id"] for r in m["rows"]] for m in msgs],
                         [[uid_of(4)], [uid_of(3)], [uid_of(6)], [uid_of(20)], [uid_of(2)], [uid_of(1)], [uid_of(5)]])

    def test_a_row_that_needs_you_counts_as_needs_for_any_change(self):
        self.needs(7)
        self.backend._board().agents.free_at = 0
        self.messages()
        self.choke()
        self.agent(1, activity="Only the activity")
        self.zoo.on_hook({"event": "Notification", "session_id": "s7", "needs": True, "notice": "Still here"})
        self.backend._board().agents.free_at = 0
        self.assertEqual(ids(self.messages()), [uid_of(7), uid_of(1)])


class Pace(Case):
    def test_the_whole_list_of_500(self):
        for i in range(500):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        started = self.clock()
        sent = []
        while not sent or sent[-1][1]["page"] < sent[-1][1]["pages"] - 1:
            sent += self.run_for(LOOP_S)
        self.assert_paced(sent)
        total = sum(len(encode(m)) for _, m in sent)
        self.assertGreater(total, 100000)
        self.assertGreaterEqual(sent[-1][0] - started, (total - len(encode(sent[-1][1]))) / AGENTS_BYTES_PER_S)
        self.assertLess(sent[-1][0] - started, total / AGENTS_BYTES_PER_S * 1.3)  # and it doesn't dawdle

    def test_a_flood_of_changes(self):
        self.agents(300)
        self.whole_list()
        n = [0]

        def flood():  # every agent, again and again, far more than the pace can carry
            n[0] += 1
            for i in range(300):
                self.agent(i, activity=f"Step {n[0]} " + "x" * 30)
        sent = self.run_for(20, flood)
        self.assert_paced(sent)
        self.assertLess(max(len(encode(m)) for _, m in sent), CHANGES_BYTES + 200)
        self.assertLessEqual(sum(len(encode(m)) for _, m in sent), AGENTS_BYTES_PER_S * 21)

    def test_the_pace_doesnt_save_up_while_idle(self):
        self.agents(5)
        self.whole_list()
        self.clock.advance(600)
        sent = self.run_for(10, lambda: self.agent(1, activity=f"Step {self.clock()} " + "x" * 200))
        self.assert_paced(sent)

    def test_a_small_change_after_a_big_page_waits_only_for_that_page(self):
        for i in range(20):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        pages = self.whole_list()
        self.assertEqual(len(pages), 1)
        started, wait = self.clock(), len(encode(pages[-1])) / AGENTS_BYTES_PER_S
        self.assertGreater(wait, 1)
        self.agent(1, activity="Now")
        when, msg = self.run_for(wait + 1)[0]
        self.assertEqual(ids([msg]), [uid_of(1)])
        self.assertGreaterEqual(when - started, wait)
        self.assertLess(when - started, wait + 2 * LOOP_S)


class NoStarving(Case):
    def setUp(self):
        super().setUp()
        self.agents(300)
        self.whole_list()

    def flood_while_this_waits(self, flood):
        """It changes one quiet agent, floods the rest and sees that agent's row go soon after it's overdue."""
        self.agent(299, activity="Quiet, and a long time waiting")
        sent = self.run_for(OVERDUE_S + 5, flood)
        self.assertGreater(len(ids(m for _, m in sent)), 100)  # there was a flood
        self.assertIn(uid_of(299), ids(m for _, m in sent), "it starved")

    def test_a_flood_of_changes_of_st_doesnt_starve_a_small_change(self):
        n = [0]

        def flood():
            n[0] += 1
            for i in range(250):
                self.agent(i, activity="x" * 40) if n[0] % 2 else self.done(i)
        self.flood_while_this_waits(flood)

    def test_even_a_flood_of_agents_that_need_you_doesnt_starve_a_small_change(self):
        n = [0]

        def flood():
            n[0] += 1
            for i in range(250):
                self.zoo.on_hook({"event": "PermissionRequest", "session_id": f"s{i}", "activity": f"Needs permission: {n[0]}"})
        self.flood_while_this_waits(flood)

    def test_in_such_a_flood_a_new_needs_is_still_at_the_front_of_the_line(self):
        n = [0]

        def flood():
            n[0] += 1
            for i in range(250):
                self.agent(i, activity=f"Step {n[0]} " + "x" * 40)
        self.run_for(15, flood)
        self.needs(280)
        started = self.clock()
        for t, msg in self.run_for(2, flood):
            if uid_of(280) in ids([msg]):
                self.assertLess(t - started, 0.5)
                self.assertLessEqual(ids([msg]).index(uid_of(280)), 1)  # at most behind one row that has waited too long
                return
        self.fail("it took too long")

    def test_every_agent_in_a_flood_gets_its_turn(self):
        seen = set()
        n = [0]

        def flood():
            n[0] += 1
            for i in range(300):
                self.agent(i, activity=f"Step {n[0]}")
        for _, msg in self.run_for(60, flood):
            seen.update(ids([msg]))
        self.assertEqual(len(seen), 300)


class Resync(Case):
    def test_it_sends_the_whole_list_again(self):
        self.agents(5)
        self.whole_list()
        self.backend.on_message({"t": "resync"})
        msgs = self.messages()
        self.assertEqual((msgs[0]["page"], ids(msgs)), (0, [uid_of(i) for i in range(5)]))

    def test_it_begins_again_in_the_middle_of_a_list(self):
        for i in range(200):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        self.assertEqual(self.backend.agents_due()["page"], 0)
        self.clock.advance(5)
        self.assertEqual(self.backend.agents_due()["page"], 1)
        self.clock.advance(5)
        self.backend.on_message({"t": "resync"})
        pages = self.until_listed()
        self.assertEqual([p["page"] for p in pages], list(range(len(pages))))
        self.assertEqual(ids(pages), [uid_of(i) for i in range(200)])

    def test_what_was_waiting_is_in_the_list_and_doesnt_follow_it(self):
        self.agents(5)
        self.whole_list()
        self.choke()
        self.end(0)
        self.agent(1, activity="Changed")
        self.backend.agents_due()
        self.backend.on_message({"t": "resync"})
        self.backend._board().agents.free_at = 0
        msgs = self.messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(ids(msgs), [uid_of(i) for i in range(1, 5)])
        self.assertEqual(msgs[0]["rows"][0]["act"], "Changed")

    def test_it_cant_beat_the_pace(self):
        for i in range(100):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        self.assertIsNotNone(self.backend.agents_due())
        self.backend.on_message({"t": "resync"})
        self.assertIsNone(self.backend.agents_due())

    def test_before_the_boards_answer_it_does_nothing(self):
        self.agents(2)
        connect(self.backend, board=False)
        self.backend.on_message({"t": "resync"})
        self.assertIsNone(self.backend.agents_due())

    def test_with_no_board_connected_it_does_nothing(self):
        self.backend.on_message({"t": "resync"})
        self.assertIsNone(self.backend.agents_due())


class NewConnection(Case):
    def test_it_starts_over_with_nothing_left_of_the_last(self):
        self.agents(5)
        self.whole_list()
        self.agent(1, activity="Changed, but the board went away")
        self.end(0)
        disconnect(self.backend, self.link)
        self.assertIsNone(self.backend.agents_due())
        connect(self.backend, self.link, board=False)
        self.assertEqual(self.messages(), [])  # until it answers
        self.backend.on_message({"t": "board", "uid": helpers.UID})
        msgs = self.messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["page"], 0)
        self.assertEqual(ids(msgs), [uid_of(i) for i in range(1, 5)])
        self.assertEqual(msgs[0]["rows"][0]["act"], "Changed, but the board went away")

    def test_each_connection_has_its_own_sync_state(self):
        self.whole_list()
        first = self.backend._board().agents
        disconnect(self.backend, self.link)
        connect(self.backend, self.link)
        self.assertIsNot(self.backend._board().agents, first)
        self.assertEqual(self.backend._board().agents.held, {})

    def test_a_list_cut_short_is_sent_whole_next_time(self):
        for i in range(200):
            self.agent(i, project="プ" * 40, activity="編" * 48)
        self.link = connect(self.backend)
        self.assertEqual(self.backend.agents_due()["page"], 0)
        disconnect(self.backend, self.link)
        connect(self.backend, self.link)
        self.clock.advance(5)
        pages = self.until_listed()
        self.assertEqual(ids(pages), [uid_of(i) for i in range(200)])


class State(Case):
    def setUp(self):
        super().setUp()
        self.real_link = Link({}, self.backend)
        self.real_link._start_session()

    def sent_after_settling(self):
        self.assertIsNotNone(self.real_link._state_due())  # the first one, always
        self.assertIsNone(self.real_link._state_due())

    def test_it_carries_n_and_no_agents(self):
        self.agents(3)
        msg = self.backend.state()
        self.assertEqual(msg["n"], 3)
        self.assertNotIn("agents", msg)
        self.assertEqual(set(msg), {"t", "v", "seq", "now", "utc", "toggl", "voice", "n"})

    def test_a_change_in_an_agent_doesnt_trigger_it(self):
        self.agents(3)
        self.sent_after_settling()
        seq = self.backend.state()["seq"]
        self.agent(1, activity="Reading a.c")
        self.needs(2)
        self.done(0)
        self.zoo.on_hook({"event": "SubagentStart", "session_id": "s0", "agent_id": "g", "agent_type": "Plan"})
        self.zoo.tick()
        self.assertIsNone(self.real_link._state_due())
        self.assertEqual(self.backend.state()["seq"], seq)

    def test_a_change_in_n_does(self):
        self.agents(3)
        self.sent_after_settling()
        self.agent(9)
        self.assertEqual(self.real_link._state_due()["n"], 4)
        self.assertIsNone(self.real_link._state_due())
        self.end(9)
        self.assertEqual(self.real_link._state_due()["n"], 3)

    def test_one_agent_ending_as_another_starts_doesnt_trigger_it(self):
        self.agents(3)
        self.sent_after_settling()
        self.end(0)
        self.agent(9)
        self.assertIsNone(self.real_link._state_due())

    def test_its_own_fields_do(self):
        self.sent_after_settling()
        self.backend.client.key_rejected = True
        self.assertEqual(self.real_link._state_due()["toggl"], "no_key")
        self.backend.keys.store = helpers.secrets_store.MemoryStore()
        self.backend.keys.refresh()
        self.assertFalse(self.real_link._state_due()["voice"])
        self.assertIsNone(self.real_link._state_due())

    def test_a_new_utc_offset_does(self):
        self.sent_after_settling()
        with mock.patch("backend.time.localtime", return_value=mock.Mock(tm_gmtoff=3600)):
            self.assertEqual(self.real_link._state_due()["utc"], 3600)

    def test_time_passing_alone_only_keeps_it_alive(self):
        self.sent_after_settling()
        self.clock.advance(5)
        self.assertIsNone(self.real_link._state_due())
        self.real_link._last_state = time.monotonic() - KEEPALIVE_S - 1
        self.assertIsNotNone(self.real_link._state_due())


class TheCap(Case):
    def test_the_board_gets_the_first_512_and_n_says_so(self):
        self.assertEqual(MAX_AGENTS, 512)
        self.agents(600)
        self.assertEqual(self.backend.state()["n"], 512)
        self.assertEqual(ids(self.whole_list()), [uid_of(i) for i in range(512)])
        self.assertEqual(len(self.zoo.public()["agents"]), 512)

    def test_when_one_ends_the_next_takes_its_place_at_the_end(self):
        self.agents(514)
        self.whole_list()
        self.end(3)
        first, second = self.messages()
        self.assertEqual(first["gone"], [uid_of(3)])
        self.assertEqual(ids([second]), [uid_of(512)])
        self.assertEqual(self.backend.state()["n"], 512)

    def test_an_older_agent_found_late_pushes_out_the_newest(self):
        self.agents(512)
        self.whole_list()
        self.zoo.on_poll([{"sessionId": "late", "status": "busy", "startedAt": self.clock() - 100000}], complete=False)
        first, second = self.messages()
        self.assertEqual(first["gone"], [uid_of(511)])
        self.assertEqual(ids([second]), [short_id("late")])


class AtScale(unittest.TestCase):
    """Hundreds of agents, each sending hook events, on threads that hold the zoo's lock."""

    def test_a_hook_event_looks_only_at_its_own_agent(self):
        zoo = Zoo()
        for i in range(50):
            zoo.on_hook({"event": "PreToolUse", "session_id": f"s{i}", "project": "p", "activity": "x"})
        with mock.patch.object(Agent, "snapshot", autospec=True, side_effect=Agent.snapshot) as snapshot:
            zoo.on_hook({"event": "PreToolUse", "session_id": "s7", "project": "p", "activity": "y"})
        self.assertEqual(snapshot.call_count, 1)

    def test_the_version_still_moves_for_what_only_a_full_look_finds(self):
        zoo = Zoo()
        for i in range(3):
            zoo.on_hook({"event": "PreToolUse", "session_id": f"s{i}", "project": "p", "activity": "x"})
        before = zoo.version
        zoo.agents["s1"].activity = "Changed behind its back"
        zoo.tick()
        self.assertEqual(zoo.version, before + 1)
        zoo.tick()
        self.assertEqual(zoo.version, before + 1)

    def test_an_agent_that_ends_moves_the_version(self):
        zoo = Zoo()
        zoo.on_hook({"event": "PreToolUse", "session_id": "s", "project": "p", "activity": "x"})
        before = zoo.version
        zoo.on_hook({"event": "SessionEnd", "session_id": "s"})
        self.assertEqual(zoo.version, before + 1)
        self.assertEqual(zoo._snapshots, {})

    def test_one_pass_over_500_agents_takes_a_few_milliseconds(self):
        zoo = Zoo()
        for i in range(500):
            zoo.on_hook({"event": "PreToolUse", "session_id": f"s{i}", "project": "プ" * 40, "activity": "編" * 48,
                         "rex_session": "session:1", "rex_block": f"block:{i}"})
            for j in range(6):
                zoo.on_hook({"event": "SubagentStart", "session_id": f"s{i}", "agent_id": f"g{j}", "agent_type": "型" * 24})
        sync = AgentSync(zoo, lambda a: "Rex session “work”", clock=Clock())
        sync.held = {row["id"]: row for row, _, _ in zoo.rows(sync.where_of)}
        best = 1.0
        for _ in range(5):
            started = time.perf_counter()
            sync._compare(zoo.rows(sync.where_of), 0)
            best = min(best, time.perf_counter() - started)
        self.assertEqual(sync.waiting, {})
        self.assertLess(best, 0.02)  # about 3.5 ms when this was written; much more room than that, for a busy machine


class Subprocess:
    """Answers subprocess.run for `ps` and `lsof`, and counts what's run."""

    ROWS = ["101 1 ttys001 /usr/local/bin/codex", "102 1 ttys002 codex", "103 1 ttys003 codex"]

    def __init__(self):
        self.rows, self.calls = list(self.ROWS), []

    def __call__(self, argv, **_kw):
        self.calls.append(argv)
        if argv[:2] == ["ps", "-axo"]:
            return mock.Mock(stdout="\n".join(self.rows))
        if argv[0] == "lsof":
            return mock.Mock(stdout="p1\nfcwd\nn/Users/me/shop\n")
        if "lstart=" in argv:
            return mock.Mock(stdout="Fri Nov  6 19:00:00 2026\n")
        return mock.Mock(stdout="REX_SESSION=session:1 REX_BLOCK=block:1")


class CodexWindowsLookup(unittest.TestCase):
    def setUp(self):
        sources._codex_known.clear()
        self.addCleanup(sources._codex_known.clear)
        self.run_ = Subprocess()
        patcher = mock.patch.object(sources.subprocess, "run", self.run_)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_window_is_looked_into_once_not_on_every_poll(self):
        first = sources.codex_windows()
        self.assertEqual(sorted(w["pid"] for w in first), [101, 102, 103])
        self.assertEqual(first[0]["folder"], "shop")
        self.assertEqual(first[0]["rex_block"], "block:1")
        self.assertEqual(len(self.run_.calls), 1 + 3 * 3)
        self.run_.calls.clear()
        self.assertEqual(sources.codex_windows(), first)
        self.assertEqual(len(self.run_.calls), 1)  # only the one `ps` that lists them

    def test_a_new_window_costs_only_its_own_lookup_and_one_that_closed_is_forgotten(self):
        sources.codex_windows()
        self.run_.calls.clear()
        self.run_.rows = self.run_.ROWS[1:] + ["104 1 ttys004 codex"]
        self.assertEqual(sorted(w["pid"] for w in sources.codex_windows()), [102, 103, 104])
        self.assertEqual(len(self.run_.calls), 1 + 3)
        self.assertNotIn(101, sources._codex_known)

    def test_a_lookup_that_came_back_empty_is_tried_again(self):
        real = self.run_.__call__

        def lsof_fails(argv, **kw):
            return mock.Mock(stdout="") if argv[0] == "lsof" else real(argv, **kw)
        with mock.patch.object(sources.subprocess, "run", lsof_fails):
            self.assertEqual(sources.codex_windows()[0]["folder"], "")
        self.assertEqual(sources.codex_windows()[0]["folder"], "shop")

    def test_match_codex_still_pairs_by_folder_and_the_closest_start(self):
        windows = [{"pid": 1, "folder": "a", "started": 100}, {"pid": 2, "folder": "a", "started": 200},
                   {"pid": 3, "folder": "b", "started": 150}, {"pid": 4, "folder": "a", "started": 300}]
        agents = [{"sid": "x", "project": "a", "started": 190, "pid": 0}, {"sid": "y", "project": "a", "started": 195, "pid": 0},
                  {"sid": "z", "project": "c", "started": 150, "pid": 0}, {"sid": "w", "project": "a", "started": 1, "pid": 4}]
        placed = sources.match_codex(agents, windows)
        self.assertEqual({s: w["pid"] for s, w in placed.items()}, {"x": 2, "y": 1})


class RexAtScale(unittest.TestCase):
    class Counting(rex_mod.Rex):
        def __init__(self):
            super().__init__("rex")
            self.calls = []

        def _run(self, *args, timeout=4):
            self.calls.append(args)
            if args[0] == "ls":
                return '{"sessions":[{"session_id":"session:1","label":"work"}]}'
            return '{"blocks":[]}'

    def test_many_sessions_are_listed_a_few_at_a_time_and_all_in_turn(self):
        rex = self.Counting()
        sessions = {f"session:{i:03d}" for i in range(50)}
        listed = set()
        for _ in range(3):
            rex.calls.clear()
            rex.pane_names(sessions)
            self.assertEqual(len(rex.calls), rex_mod.LISTINGS_PER_CALL)
            listed |= {c[c.index("-s") + 1] for c in rex.calls}
        self.assertEqual(listed, sessions)

    def test_a_few_sessions_are_all_listed_each_time(self):
        rex = self.Counting()
        for _ in range(3):
            rex.calls.clear()
            rex.pane_names({"session:1", "session:2"})
            self.assertEqual(len(rex.calls), 2)

    def test_the_poller_runs_no_rex_command_with_no_agent_in_rex(self):
        rex = self.Counting()
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        herdr, hc_calls = Herdr("hc", "herdr-app"), []
        herdr._run = lambda *args, **kw: hc_calls.append(args) or "{}"
        with mock.patch.multiple(sources, poll_agents=lambda _: ([], True), codex_windows=lambda: [], grok_windows=lambda: []):
            sources.poller(Zoo(), {"claude_bin": "claude", "poll_seconds": 10}, stop, rex, herdr)
        self.assertEqual((rex.calls, hc_calls), ([], []))  # nor an hc command: it could start hc's server

    def test_the_poller_reads_the_session_labels_with_an_agent_in_rex(self):
        rex = self.Counting()
        zoo = Zoo()
        zoo.on_hook({"event": "PreToolUse", "session_id": "s", "rex_session": "session:1", "rex_block": "block:1"})
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        with mock.patch.multiple(sources, poll_agents=lambda _: ([], False), codex_windows=lambda: [], grok_windows=lambda: []):
            sources.poller(zoo, {"claude_bin": "claude", "poll_seconds": 10}, stop, rex, Herdr("hc", "herdr-app"))
        self.assertIn(("ls", "--json"), rex.calls)
        self.assertEqual(rex.where("session:1"), "Rex session “work”")

    def test_where_runs_no_process_and_the_poller_refreshes_it_once_a_minute(self):
        rex = self.Counting()
        self.assertEqual(rex.where("session:1"), "Rex")
        self.assertEqual(rex.calls, [])
        rex.refresh_where()
        rex.refresh_where()
        self.assertEqual(rex.calls, [("ls", "--json")])
        self.assertEqual(rex.where("session:1"), "Rex session “work”")
        self.assertEqual(rex.where(""), "")
        rex.cache_at -= 61
        rex.refresh_where()
        self.assertEqual(len(rex.calls), 2)


class NotThroughTheQueue(Case):
    def test_agents_lines_dont_go_through_the_queue(self):
        """A queue can't be paced: everything in it goes at once, and what waits in it can't be replaced."""
        self.agents(3)
        link = connect(self.backend, FakeLink())
        self.backend.agents_due()
        self.assertEqual(link.of_kind("agents"), [])


if __name__ == "__main__":
    unittest.main()
