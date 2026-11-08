"""Which boards this Mac trusts: an untrusted board gets only the time, a board trusted while connected gets everything at
once, and the list starts with the boards the Mac already knows. Also the main loop, which carries on after an error."""
import json
import threading
import unittest

import helpers
from helpers import T0, Clock, FakeLink, HomeCase, connect, disconnect, make_backend
from test_clock_sync import board_clock
from test_secrets_and_lock import run_cli

import bentobox
import config
from link.base import Link
from link.tcp import TcpLink
from trust import TrustStore

KNOB, SQUARE, STRANGER = "331d7d73", "64056f77", "0badcafe"


def toggl_start(rid=1, msg_id=7):
    return {"t": "toggl", "id": msg_id, "op": "start", "rid": rid, "at": T0, "desc": "x", "task_id": 0, "project": 0}


class Untrusted(HomeCase):
    def setUp(self):
        super().setUp()
        self.backend = make_backend(clock=Clock())
        self.backend.lock_store.set_code(helpers.LOCK_CODE)
        self.knob = connect(self.backend, FakeLink(), uid=KNOB)
        self.backend.on_message(board_clock(seq=1), self.knob)  # a latest clock to hand out
        self.stranger = connect(self.backend, FakeLink(), uid=STRANGER, trusted=False)

    def test_it_gets_only_the_time(self):
        kinds = {m["t"] for m in self.stranger.sent}
        self.assertNotIn("lock_code", kinds)
        self.assertNotIn("clock", kinds)
        self.assertIsNone(self.backend.agents_due(self.stranger))
        self.assertEqual(self.backend.http_status()["boards"][-1], {"uid": STRANGER, "name": helpers.MODEL, "trusted": False})

    def test_nothing_it_sends_is_taken(self):
        self.backend.on_message(toggl_start(), self.stranger)
        self.backend.on_message(board_clock(seq=2, at=60_000, now=60_000), self.stranger)
        self.backend.on_message({"t": "sync", "req": 3, "rid": 0}, self.stranger)
        self.backend.on_message({"t": "voice_start", "vid": 1, "rate": 16000}, self.stranger)
        self.assertEqual(self.stranger.of_kind("ack"), [])  # the board keeps the message, for when it's trusted
        self.assertEqual(self.backend.runs.queued(), 0)
        self.assertEqual(self.backend.clocks.message()["from"], KNOB)
        self.assertEqual(self.knob.of_kind("clock")[-1]["from"], KNOB)  # nothing new reached the trusted board
        self.assertEqual(self.backend.voice.clips, {})

    def test_it_is_noted_for_the_cli(self):
        self.assertIn(STRANGER, self.backend.trust.seen())
        self.assertNotIn(STRANGER, self.backend.trust.trusted())

    def test_trusted_while_connected_it_gets_everything_at_once(self):
        self.backend.trust.trust(STRANGER)
        self.backend.reload()
        self.assertEqual(self.stranger.of_kind("hello")[-1]["t"], "hello")  # so it sends `board` and its saved messages
        connect(self.backend, self.stranger, uid=STRANGER, trusted=False)  # its answer, on a new session here
        kinds = {m["t"] for m in self.stranger.sent}
        self.assertIn("lock_code", kinds)
        self.assertIn("clock", kinds)
        self.backend.on_message(toggl_start(), self.stranger)
        self.assertEqual(self.stranger.of_kind("ack"), [{"t": "ack", "id": 7}])

    def test_forgotten_it_goes_back_to_only_the_time(self):
        self.backend.trust.forget(KNOB)
        self.backend.reload()
        self.backend.on_message(toggl_start(msg_id=9), self.knob)
        self.assertEqual(self.knob.of_kind("ack"), [])
        self.assertIsNone(self.backend.agents_due(self.knob))

    def test_a_simulator_on_this_mac_needs_no_list(self):
        self.assertTrue(TcpLink.trusted_link)
        self.assertFalse(Link.trusted_link)


class TheList(HomeCase):
    def path(self):
        return config.state_path("boards.json")

    def test_it_starts_with_the_boards_this_mac_knows(self):
        store = TrustStore(self.path())
        store.start({KNOB, SQUARE, "not-a-uid"})
        self.assertEqual(sorted(store.trusted()), sorted([KNOB, SQUARE]))
        store.start({STRANGER})  # only the first time
        self.assertNotIn(STRANGER, store.trusted())

    def test_the_backend_starts_it_from_toggl_and_the_lock_code(self):
        backend = make_backend(clock=Clock())
        link = connect(backend, FakeLink(), uid=KNOB)
        backend.on_message(toggl_start(), link)
        nonce = "n1"
        with open(config.state_path("lock.json"), "w") as f:
            json.dump({"salt": "00", "iter": 1, "hash": "ab", "nonce": nonce, "sent": [SQUARE]}, f)
        import os
        os.unlink(self.path())  # as on a Mac from before the list
        fresh = make_backend(clock=Clock())
        self.assertEqual(sorted(fresh.trust.trusted()), sorted([KNOB, SQUARE]))

    def test_a_broken_file_trusts_nobody(self):
        import os
        os.makedirs(os.path.dirname(self.path()), exist_ok=True)
        with open(self.path(), "w") as f:
            f.write("{nope")
        self.assertFalse(TrustStore(self.path()).is_trusted(KNOB))

    def test_the_cli(self):
        store = TrustStore(self.path())
        store.start(set())
        store.note_seen(STRANGER, "Bento Knob")
        code, out, _ = run_cli("boards")
        self.assertEqual(code, 0)
        self.assertIn(f"Not trusted: {STRANGER}  Bento Knob", out)
        code, out, _ = run_cli("boards", "trust")  # the one board waiting
        self.assertEqual(code, 0, out)
        self.assertTrue(store.is_trusted(STRANGER))
        code, out, _ = run_cli("boards", "forget", STRANGER)
        self.assertEqual(code, 0)
        self.assertFalse(store.is_trusted(STRANGER))
        code, _, err = run_cli("boards", "trust", "zz")
        self.assertNotEqual(code, 0)
        self.assertIn("isn't a board's uid", err)


class MainLoop(unittest.TestCase):
    def test_an_error_in_one_pass_doesnt_stop_it(self):
        stop, passes = threading.Event(), []

        class Zoo:
            @staticmethod
            def tick():
                passes.append(1)
                if len(passes) == 1:
                    raise ValueError("odd agent")
                if len(passes) >= 3:
                    stop.set()

        mark = helpers.LOGS.tell()
        bentobox.main_loop(stop, Zoo(), None, every=0.001)
        helpers.LOGS.seek(mark)
        logged = helpers.LOGS.read()
        helpers.LOGS.seek(0, 2)
        self.assertGreaterEqual(len(passes), 3)
        self.assertIn("ValueError in the main loop", logged)


if __name__ == "__main__":
    unittest.main()


class ConfigFolder(HomeCase):
    def test_the_folder_from_before_the_rename_moves_over_whole(self):
        import os
        old = os.path.join(self.home, ".config", "bento")
        os.makedirs(old)
        with open(os.path.join(old, "toggl.json"), "w") as f:
            f.write("{}")
        self.assertEqual(config.config_dir(), os.path.join(self.home, ".config", "bentobox"))
        self.assertTrue(os.path.exists(config.state_path("toggl.json")))
        self.assertFalse(os.path.exists(old))

    def test_an_existing_new_folder_wins(self):
        import os
        for name in ("bento", "bentobox"):
            os.makedirs(os.path.join(self.home, ".config", name))
        self.assertEqual(config.config_dir(), os.path.join(self.home, ".config", "bentobox"))
        self.assertTrue(os.path.isdir(os.path.join(self.home, ".config", "bento")))  # left alone
