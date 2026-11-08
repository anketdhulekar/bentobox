"""Keys in the Keychain (with a fake `security` tool, never the real one), and the lock code flow."""
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import helpers
from helpers import KEY, LOCK_CODE, SPEECH_KEY, T0, FakeSpeech, FakeToggl, HomeCase, audio_messages, connect, disconnect, drain, make_backend, speech_pcm

import bentobox
import config
import http_call
import lockcode
import secrets_store
from secrets_store import KeychainStore, MemoryStore, SecretStoreError, clean_key
from speech.stt import SpeechClient
from toggl.client import TogglClient

FAKE_SECURITY = r"""#!/usr/bin/python3
# Stands in for /usr/bin/security: it keeps "keys" in a file beside it, and logs every argv and all of stdin.
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
db_path = os.path.join(here, "db.json")
db = json.load(open(db_path)) if os.path.exists(db_path) else {}
args = sys.argv[1:]
stdin = sys.stdin.read() if args == ["-i"] else ""
with open(os.path.join(here, "argv.log"), "a") as f:
    f.write(json.dumps(sys.argv) + "\n")
with open(os.path.join(here, "stdin.log"), "a") as f:
    f.write(stdin)
if os.path.exists(os.path.join(here, "broken")):
    sys.stderr.write("security: something went wrong\n")
    sys.exit(1)
def opt(a, flag):
    return a[a.index(flag) + 1] if flag in a else None
if args == ["-i"]:
    for line in stdin.splitlines():
        a = line.split()
        if a and a[0] == "add-generic-password":
            if os.path.exists(os.path.join(here, "refuse")):
                print("security: SecKeychainItemCreateFromContent: refused")
                continue
            db[opt(a, "-s") + "/" + opt(a, "-a")] = bytes.fromhex(opt(a, "-X")).decode()
    json.dump(db, open(db_path, "w"))
    sys.exit(0)
if args[0] == "find-generic-password":
    item = opt(args, "-s") + "/" + opt(args, "-a")
    if item not in db:
        sys.stderr.write("security: The specified item could not be found in the keychain.\n")
        sys.exit(44)
    print(db[item] if "-w" in args else 'keychain: "x"\nattributes: ...')
    sys.exit(0)
if args[0] == "delete-generic-password":
    item = opt(args, "-s") + "/" + opt(args, "-a")
    if item not in db:
        sys.exit(44)
    del db[item]
    json.dump(db, open(db_path, "w"))
    sys.exit(0)
sys.exit(2)
"""


def make_fake_security(folder):
    path = os.path.join(folder, "security")
    with open(path, "w") as f:
        f.write(FAKE_SECURITY)
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


KNOB, SQUARE = "331d7d73", "64056f77"


class CleanKey(unittest.TestCase):
    def test_stray_characters_go_and_are_only_counted(self):
        self.assertEqual(clean_key(" toggl_sk_abc​\n"), ("toggl_sk_abc", 3))
        self.assertEqual(clean_key(" abc "), ("abc", 2))
        self.assertEqual(clean_key(""), ("", 0))


class Memory(unittest.TestCase):
    def test_the_memory_store(self):
        store = MemoryStore(toggl="a")
        self.assertTrue(store.has("toggl"))
        self.assertEqual(store.get("toggl"), "a")
        self.assertFalse(store.has("speech"))
        self.assertIsNone(store.get("speech"))
        store.set("speech", "b")
        self.assertEqual(store.get("speech"), "b")
        with self.assertRaises(SecretStoreError):
            store.get("other")


class Keychain(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="bentobox-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = KeychainStore(make_fake_security(self.dir))

    def read(self, name):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()

    def test_the_default_tool_and_service(self):
        self.assertEqual((KeychainStore().service, KeychainStore().legacy), ("bentobox", "bento"))
        with open(os.path.join(helpers.DESKTOP, "config.py")) as f:
            self.assertIn('"security_bin": "/usr/bin/security"', f.read())

    def test_a_write_goes_through_stdin_only(self):
        self.store.set("toggl", KEY)
        self.assertEqual(self.store.get("toggl"), KEY)
        argv = self.read("argv.log")
        self.assertNotIn(KEY, argv)
        self.assertNotIn(KEY.encode().hex(), argv)
        self.assertIn('"-i"', argv)
        self.assertNotIn(KEY, self.read("stdin.log"))  # hexadecimal, so no quoting can go wrong
        self.assertIn(f"add-generic-password -U -s bentobox -a toggl -X {KEY.encode().hex()}", self.read("stdin.log"))

    def db(self):
        return json.loads(self.read("db.json") or "{}")

    def test_a_key_saved_under_the_old_name_moves_over(self):
        with open(os.path.join(self.dir, "db.json"), "w") as f:
            json.dump({"bento/toggl": KEY}, f)
        self.assertTrue(self.store.has("toggl"))
        self.assertEqual(self.db(), {"bentobox/toggl": KEY})  # one copy, under the new name
        self.assertEqual(self.store.get("toggl"), KEY)
        self.assertFalse(self.store.has("speech"))

    def test_reading_moves_it_too(self):
        with open(os.path.join(self.dir, "db.json"), "w") as f:
            json.dump({"bento/speech": SPEECH_KEY}, f)
        self.assertEqual(self.store.get("speech"), SPEECH_KEY)
        self.assertEqual(self.db(), {"bentobox/speech": SPEECH_KEY})

    def test_a_new_key_replaces_an_old_copy(self):
        with open(os.path.join(self.dir, "db.json"), "w") as f:
            json.dump({"bento/toggl": "old"}, f)
        self.store.set("toggl", KEY)
        self.assertEqual(self.db(), {"bentobox/toggl": KEY})

    def test_any_key_character_survives(self):
        for value in ('a"b', "a'b", "a\\b", "a b", "a$b`c", "ünï"):
            self.store.set("speech", value)
            self.assertEqual(self.store.get("speech"), value)

    def test_has_doesnt_read_the_key(self):
        self.assertFalse(self.store.has("toggl"))
        self.store.set("toggl", KEY)
        with open(os.path.join(self.dir, "argv.log"), "w"):
            pass
        self.assertTrue(self.store.has("toggl"))
        self.assertNotIn('"-w"', self.read("argv.log"))

    def test_a_missing_key_is_none(self):
        self.assertIsNone(self.store.get("speech"))

    def test_a_write_the_keychain_refused_is_an_error_without_the_key(self):
        open(os.path.join(self.dir, "refuse"), "w").close()
        with self.assertRaises(SecretStoreError) as e:
            self.store.set("toggl", KEY)
        self.assertNotIn(KEY, str(e.exception))
        self.assertNotIn(KEY.encode().hex(), str(e.exception))

    def test_a_tool_that_cant_run_is_an_error_with_no_original_exception(self):
        store = KeychainStore("/nonexistent/security")
        with self.assertRaises(SecretStoreError) as e:
            store.set("toggl", KEY)
        self.assertIsNone(e.exception.__context__)
        self.assertNotIn(KEY, str(e.exception))

    def test_a_timeout_carries_no_output(self):
        secret_output = subprocess.TimeoutExpired(["security"], 20, output=KEY)
        with mock.patch.object(secrets_store.subprocess, "run", side_effect=secret_output):
            with self.assertRaises(SecretStoreError) as e:
                self.store.get("toggl")
        self.assertIsNone(e.exception.__context__)
        self.assertIsNone(e.exception.__cause__)
        self.assertNotIn(KEY, str(e.exception))

    def test_only_known_names(self):
        for call in (lambda: self.store.get("../etc"), lambda: self.store.has("x y"), lambda: self.store.set("-w", "v")):
            with self.assertRaises(SecretStoreError):
                call()

    def test_empty_and_huge_keys_are_refused(self):
        for bad in ("", "x" * 5000):
            with self.assertRaises(SecretStoreError):
                self.store.set("toggl", bad)


class NoLeaks(HomeCase):
    """A distinctive key goes through every failure, and must not show in any log, file or error."""

    def setUp(self):
        super().setUp()
        self.mark = helpers.LOGS.tell()

    def new_logs(self):
        helpers.LOGS.seek(self.mark)
        text = helpers.LOGS.read()
        helpers.LOGS.seek(0, 2)
        return text

    def test_keys_stay_out_of_the_logs_files_and_state(self):
        toggl, speech = FakeToggl(), FakeSpeech()
        self.addCleanup(toggl.close)
        self.addCleanup(speech.close)
        backend = make_backend(toggl, speech)
        link = connect(backend)
        backend.runs.run_jobs()
        # Every kind of failure from Toggl.
        for status in (500, 429, 400, 403, 404):
            backend.client.wid, backend.client.oid = helpers.WID, helpers.ORG
            backend.on_message({"t": "toggl", "id": status, "op": "start", "rid": status, "at": T0 + status, "desc": "x", "task_id": 0, "project": 0})
            toggl.fail_next(status, body={"error": "echoing " + KEY})
            backend.runs.client.next_try = backend.runs.client.rate_until = 0
            drain(backend)
        toggl.down = True
        backend.runs.client.next_try = backend.runs.client.rate_until = 0
        drain(backend)
        toggl.down = False
        toggl.key = "other"  # a 401
        backend.runs.client.next_try = 0
        backend.on_message({"t": "toggl", "id": 999, "op": "start", "rid": 999, "at": T0 + 9999, "desc": "x", "task_id": 0, "project": 0})
        drain(backend)
        self.assertTrue(backend.client.key_rejected)
        self.assertTrue(any(r.auth == f"Bearer {KEY}" for r in toggl.log))  # the key really was in use
        # Every kind of failure from speech-to-text.
        pcm = speech_pcm(1.0)
        for status in (400, 500, None):
            speech.script.append(status)
            speech.script.append(status)
            packets = audio_messages(pcm)
            backend.on_message({"t": "voice_start", "vid": 3, "rate": 16000})
            for p in packets:
                backend.on_message(p)
            backend.on_message({"t": "voice_end", "vid": 3, "packets": len(packets)})
        self.assertTrue(any(r["auth"] == f"Bearer {SPEECH_KEY}" for r in speech.requests))
        everything = [self.new_logs(), json.dumps(backend.state()), json.dumps(backend.http_status()), json.dumps(link.sent)]
        for folder, _, files in os.walk(self.home):
            for name in files:
                with open(os.path.join(folder, name)) as f:
                    everything.append(f.read())
        for text in everything:
            for secret in (KEY, SPEECH_KEY):
                self.assertNotIn(secret, text)

    def test_a_redirect_doesnt_carry_the_key_to_another_host(self):
        toggl, other = FakeToggl(), FakeToggl()
        self.addCleanup(toggl.close)
        self.addCleanup(other.close)
        toggl.fail_next(302, {"Location": other.url + "/users/me/settings"})
        client = TogglClient(toggl.url, lambda: KEY, lambda: helpers.ORG, clock=helpers.Clock())
        self.assertFalse(client.ensure())
        self.assertEqual(other.count(), 0)

    def test_plain_http_is_only_for_this_mac(self):
        self.assertTrue(http_call.is_safe_url("https://focus.toggl.com/api"))
        self.assertTrue(http_call.is_safe_url("http://127.0.0.1:9/x"))
        self.assertTrue(http_call.is_safe_url("http://localhost:9/x"))
        self.assertFalse(http_call.is_safe_url("http://focus.toggl.com/api"))
        self.assertFalse(http_call.is_safe_url("ftp://x/y"))
        self.assertEqual(http_call.call("GET", "http://example.com/", {"Authorization": "x"}, None, 1)[0], -1)

    def test_exceptions_from_the_network_never_escape_with_a_url_or_key(self):
        client = SpeechClient("http://127.0.0.1:9/nothing", lambda: SPEECH_KEY)
        self.assertIsNone(client.transcribe(b"wav"))
        self.assertNotIn(SPEECH_KEY, self.new_logs())


def run_cli(*argv, store=None, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    code = 0
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch("sys.stdin", io.StringIO(stdin)):
        try:
            bentobox.main(list(argv), store=store)
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
            if not isinstance(e.code, int) and e.code:
                print(e.code, file=err)
    return code, out.getvalue(), err.getvalue()


class Cli(HomeCase):
    def test_set_key_stores_it_and_never_shows_it(self):
        store = MemoryStore()
        code, out, err = run_cli("set-key", "toggl", store=store, stdin=f" {KEY}​\n")
        self.assertEqual(code, 0)
        self.assertEqual(store.get("toggl"), KEY)
        self.assertNotIn(KEY, out + err)
        self.assertIn("Ignored 2 stray characters", out)

    def test_an_empty_key_changes_nothing(self):
        store = MemoryStore(toggl="old")
        code, _, err = run_cli("set-key", "toggl", store=store, stdin="\n")
        self.assertEqual((code, store.get("toggl")), (1, "old"))
        self.assertIn("nothing changed", err)

    def test_key_status_says_only_whether(self):
        store = MemoryStore(toggl=KEY)
        code, out, _ = run_cli("key-status", store=store)
        self.assertEqual(code, 0)
        self.assertIn("Toggl key: set", out)
        self.assertIn("speech-to-text key: not set", out)
        self.assertNotIn(KEY, out)

    def test_set_org_goes_in_the_config_and_keys_dont(self):
        code, out, _ = run_cli("set-org", "4242")
        self.assertEqual(code, 0)
        self.assertIn("4242", out)
        self.assertEqual(config.load_config()["toggl_org"], 4242)
        for bad in ("abc", "0", "-5", "٣"):
            self.assertEqual(run_cli("set-org", bad)[0], 1)
        self.assertEqual(config.load_config()["toggl_org"], 4242)
        run_cli("set-key", "toggl", store=MemoryStore(), stdin=KEY + "\n")
        with open(config.config_path()) as f:
            self.assertNotIn(KEY, f.read())
        self.assertEqual(os.stat(config.config_path()).st_mode & 0o777, 0o600)

    def test_a_store_error_is_a_plain_sentence(self):
        code, _, err = run_cli("set-key", "toggl", store=KeychainStore("/nonexistent/security"), stdin=KEY + "\n")
        self.assertEqual(code, 1)
        self.assertIn("Couldn't save the key", err)
        self.assertNotIn(KEY, err)

    def test_the_real_command_line_with_a_fake_tool(self):
        """bentobox.py as a process: set-key, key-status. Its config points at a fake `security`."""
        tool_dir = os.path.join(self.home, "tool")
        os.makedirs(tool_dir)
        security = make_fake_security(tool_dir)
        config.save_config(dict(config.load_config(), security_bin=security, http_port=1))
        env = dict(os.environ, HOME=self.home)
        bento_py = os.path.join(helpers.DESKTOP, "bentobox.py")
        res = subprocess.run([sys.executable, bento_py, "set-key", "speech"], input=SPEECH_KEY + "\n", text=True,
                             capture_output=True, env=env, timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn(SPEECH_KEY, res.stdout + res.stderr)
        res = subprocess.run([sys.executable, bento_py, "key-status"], capture_output=True, text=True, env=env, timeout=20)
        self.assertIn("speech-to-text key: set", res.stdout)
        self.assertIn("Toggl key: not set", res.stdout)
        with open(os.path.join(tool_dir, "argv.log")) as f:
            self.assertNotIn(SPEECH_KEY, f.read())


class LockStoreFile(HomeCase):
    def setUp(self):
        super().setUp()
        self.path = config.state_path("lock.json")
        self.store = lockcode.LockStore(self.path)

    def test_only_digits_exactly_four(self):
        for bad in ("123", "12345", "12a4", "", None, "١٢٣٤", "12 4"):
            self.assertFalse(lockcode.is_valid(bad))
            with self.assertRaises(ValueError):
                self.store.set_code(bad)
        self.assertTrue(lockcode.is_valid("0000"))
        self.assertFalse(os.path.exists(self.path))

    def test_it_stores_salt_and_hash_never_the_code(self):
        self.store.set_code(LOCK_CODE)
        with open(self.path) as f:
            text = f.read()
        data = json.loads(text)
        self.assertNotIn(LOCK_CODE, json.dumps({k: v for k, v in data.items() if k not in ("salt", "hash", "nonce")}))
        self.assertEqual(len(bytes.fromhex(data["salt"])), 16)
        self.assertEqual(data["iter"], 2000)
        self.assertEqual(data["hash"], lockcode.hash_code(LOCK_CODE, bytes.fromhex(data["salt"]), 2000))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_the_salt_is_random(self):
        def saved():
            with open(self.path) as f:
                return json.load(f)
        self.store.set_code(LOCK_CODE)
        first = saved()
        self.store.set_code(LOCK_CODE)
        second = saved()
        self.assertNotEqual(first["salt"], second["salt"])
        self.assertNotEqual(first["hash"], second["hash"])

    def test_the_message(self):
        self.store.set_code(LOCK_CODE)
        msg, _ = self.store.pending_for(KNOB)
        self.assertEqual(set(msg), {"t", "salt", "iter", "hash"})
        self.assertEqual(msg["t"], "lock_code")
        self.store.remove()
        self.assertEqual(self.store.pending_for(KNOB)[0], {"t": "lock_code", "clear": True})

    def test_each_board_gets_it_once(self):
        self.store.set_code(LOCK_CODE)
        self.assertTrue(self.store.is_pending())
        _, nonce = self.store.pending_for(KNOB)
        self.store.mark_sent(nonce, KNOB)
        self.assertIsNone(self.store.pending_for(KNOB))
        self.assertFalse(self.store.is_pending())  # a board has it
        self.assertIsNotNone(self.store.pending_for(SQUARE))  # set once on the Mac: the other board gets it too
        self.store.mark_sent(nonce, SQUARE)
        self.assertIsNone(self.store.pending_for(SQUARE))
        self.assertEqual(sorted(self.store.sent_to()), sorted([KNOB, SQUARE]))
        self.assertTrue(self.store.is_set())

    def test_a_new_code_goes_to_every_board_again(self):
        self.store.set_code("1111")
        _, nonce = self.store.pending_for(KNOB)
        self.store.mark_sent(nonce, KNOB)
        self.store.set_code("2222")
        self.assertIsNotNone(self.store.pending_for(KNOB))
        self.assertEqual(self.store.sent_to(), [])

    def test_a_code_set_while_the_old_one_was_being_sent_isnt_lost(self):
        self.store.set_code("1111")
        _, nonce = self.store.pending_for(KNOB)
        self.store.set_code("2222")
        self.store.mark_sent(nonce, KNOB)
        self.assertIsNotNone(self.store.pending_for(KNOB))

    def test_a_removal_reaches_every_board_and_forgets_the_hash(self):
        self.store.set_code(LOCK_CODE)
        self.store.remove()
        self.assertFalse(self.store.is_set())
        _, nonce = self.store.pending_for(KNOB)
        self.store.mark_sent(nonce, KNOB)
        self.assertIsNone(self.store.pending_for(KNOB))
        self.assertEqual(self.store.pending_for(SQUARE)[0], {"t": "lock_code", "clear": True})
        with open(self.path) as f:
            self.assertNotIn("hash", json.load(f))

    def test_a_code_from_before_goes_once_to_each_board(self):
        """A file from when one board had the code: no nonce left. The same salt and hash go to each board once."""
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"salt": "00" * 16, "iter": 2000, "hash": "ab" * 32, "pending": False}, f)
        msg, nonce = self.store.pending_for(SQUARE)
        self.assertEqual((msg["salt"], msg["hash"]), ("00" * 16, "ab" * 32))
        self.store.mark_sent(nonce, SQUARE)
        self.assertIsNone(self.store.pending_for(SQUARE))
        self.assertIsNotNone(self.store.pending_for(KNOB))

    def test_a_file_with_parts_missing_means_nothing_to_send(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"pending": True, "nonce": "ab"}, f)
        self.assertIsNone(self.store.pending_for(KNOB))

    def test_a_broken_file_means_nothing_to_send(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as f:
            f.write("not json")
        self.assertIsNone(self.store.pending_for(KNOB))
        self.assertFalse(self.store.is_set())


class LockFlow(HomeCase):
    def setUp(self):
        super().setUp()
        self.backend = make_backend()
        self.mark = helpers.LOGS.tell()

    def cli(self, *argv, stdin=""):
        return run_cli("lock-code", *argv, stdin=stdin)

    def test_set_with_no_board_goes_at_the_next_connection_once(self):
        code, out, _ = self.cli("set", stdin=LOCK_CODE + "\n")
        self.assertEqual(code, 0)
        self.assertNotIn(LOCK_CODE, out)
        self.assertIn("next connects", out)
        link = connect(self.backend)
        sent = link.of_kind("lock_code")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["iter"], 2000)
        self.assertEqual(sent[0]["hash"], lockcode.hash_code(LOCK_CODE, bytes.fromhex(sent[0]["salt"])))
        self.assertIn(sent[0], link.reliable)
        disconnect(self.backend, link)
        link = connect(self.backend, link)
        self.assertEqual(len(link.of_kind("lock_code")), 1)  # not sent again

    def test_set_while_connected_goes_at_once(self):
        link = connect(self.backend)
        self.assertEqual(link.of_kind("lock_code"), [])
        self.cli("set", stdin=LOCK_CODE + "\n")
        self.backend.reload()  # the nudge from the CLI
        self.assertEqual(len(link.of_kind("lock_code")), 1)
        self.backend.reload()
        self.assertEqual(len(link.of_kind("lock_code")), 1)

    def test_it_waits_for_the_boards_answer(self):
        self.cli("set", stdin=LOCK_CODE + "\n")
        link = connect(self.backend, board=False)
        self.backend.reload()
        self.assertEqual(link.of_kind("lock_code"), [])  # it might still say busy
        self.backend.on_message({"t": "board", "v": 4, "uid": helpers.UID, "name": "b"})
        self.assertEqual(len(link.of_kind("lock_code")), 1)

    def test_a_connection_lost_before_the_write_sends_it_again(self):
        self.cli("set", stdin=LOCK_CODE + "\n")
        link = helpers.FakeLink()
        link.fail_writes = True
        connect(self.backend, link)
        self.assertEqual(len(link.of_kind("lock_code")), 1)
        self.assertTrue(self.backend.lock_store.is_pending())
        disconnect(self.backend, link)
        link.fail_writes = False
        connect(self.backend, link)
        self.assertEqual(len(link.of_kind("lock_code")), 2)
        self.assertFalse(self.backend.lock_store.is_pending())

    def test_a_board_with_no_uid_gets_the_code_too(self):
        self.cli("set", stdin=LOCK_CODE + "\n")
        link = connect(self.backend, uid=None)
        self.assertEqual(len(link.of_kind("lock_code")), 1)

    def test_remove(self):
        self.cli("set", stdin=LOCK_CODE + "\n")
        link = connect(self.backend)
        code, out, _ = self.cli("remove")
        self.assertEqual(code, 0)
        self.backend.reload()
        self.assertEqual(link.of_kind("lock_code")[-1], {"t": "lock_code", "clear": True})
        self.assertFalse(self.backend.lock_store.is_set())

    def test_the_latest_change_wins_when_no_board_was_there(self):
        self.cli("set", stdin=LOCK_CODE + "\n")
        self.cli("remove")
        link = connect(self.backend)
        self.assertEqual(link.of_kind("lock_code"), [{"t": "lock_code", "clear": True}])

    def test_bad_input_changes_nothing_and_isnt_shown(self):
        for bad in ("123", "12345", "abcd", ""):
            code, out, err = self.cli("set", stdin=bad + "\n")
            self.assertEqual(code, 1)
            if bad:
                self.assertNotIn(bad, out + err)
        self.assertFalse(os.path.exists(config.state_path("lock.json")))

    def test_the_code_and_hash_stay_out_of_the_logs(self):
        self.cli("set", stdin=LOCK_CODE + "\n")
        link = connect(self.backend)
        msg = link.of_kind("lock_code")[0]
        helpers.LOGS.seek(self.mark)
        logs = helpers.LOGS.read()
        helpers.LOGS.seek(0, 2)
        self.assertIn("lock code sent", logs)
        for secret in (LOCK_CODE, msg["hash"], msg["salt"]):
            self.assertNotIn(secret, logs)

    def test_the_status_says_only_set_or_not(self):
        self.cli("set", stdin=LOCK_CODE + "\n")
        _, out, _ = run_cli("key-status", store=MemoryStore())
        self.assertIn("Lock code: set, waiting for a board", out)
        msg, nonce = self.backend.lock_store.pending_for(KNOB)
        self.backend.lock_store.mark_sent(nonce, KNOB)
        _, out, _ = run_cli("key-status", store=MemoryStore())
        self.assertIn("Lock code: set, 1 board has it\n", out)
        self.assertNotIn(msg["hash"], out)


if __name__ == "__main__":
    unittest.main()
