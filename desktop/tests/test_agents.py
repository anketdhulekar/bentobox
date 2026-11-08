"""Tests for agent tracking and the hook script."""
import contextlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import helpers  # noqa: F401 (first: it isolates this process)

import install
from agents import hook as zoo_hook
from agents import rex as rex_mod
from agents import sources
from agents import zoo as zoo_mod
from agents.zoo import Agent, Zoo
from localhttp import make_http

HOOK = os.path.join(helpers.DESKTOP, "agents", "hook.py")


class Clock:
    def __init__(self):
        self.t = 1_790_000_000.0  # a realistic date (2026), so milliseconds and seconds differ

    def __call__(self):
        return self.t


def ev(name, sid="s1", **kw):
    return dict({"event": name, "session_id": sid, "project": "proj", "entrypoint": "cli",
                 "rex_session": "session:1", "rex_block": "block:1"}, **kw)


class HookFilter(unittest.TestCase):
    def test_bash_uses_the_description_never_the_command(self):
        out = zoo_hook.build({"hook_event_name": "PreToolUse", "session_id": "x", "tool_name": "Bash",
                              "tool_input": {"command": "cat ~/.ssh/id_rsa", "description": "Run the tests"}}, {})
        self.assertEqual(out["activity"], "Bash: Run the tests")
        self.assertNotIn("ssh", json.dumps(out))

    def test_bash_without_a_description(self):
        out = zoo_hook.build({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls /secret"}}, {})
        self.assertEqual(out["activity"], "Running a command")

    def test_files_show_only_the_base_name(self):
        out = zoo_hook.build({"hook_event_name": "PreToolUse", "tool_name": "Edit",
                              "tool_input": {"file_path": "/Users/me/private/plan.md", "old_string": "x", "new_string": "y"}}, {})
        self.assertEqual(out["activity"], "Editing plan.md")
        self.assertNotIn("private", json.dumps(out))

    def test_web_fetch_shows_only_the_domain(self):
        out = zoo_hook.build({"hook_event_name": "PreToolUse", "tool_name": "WebFetch",
                              "tool_input": {"url": "https://docs.example.com/a/b?token=abc", "prompt": "secret"}}, {})
        self.assertEqual(out["activity"], "Fetching docs.example.com")
        self.assertNotIn("token", json.dumps(out))

    def test_private_fields_are_dropped(self):
        event = {"hook_event_name": "UserPromptSubmit", "session_id": "x", "prompt": "my password is hunter2",
                 "transcript_path": "/Users/me/.claude/projects/x.jsonl", "last_assistant_message": "secret",
                 "cwd": "/Users/me/Workspaces/proj"}
        out = json.dumps(zoo_hook.build(event, {"REX_BLOCK": "block:9", "CLAUDE_CODE_ENTRYPOINT": "cli"}))
        for word in ("hunter2", "transcript", "jsonl", "Workspaces", "secret"):
            self.assertNotIn(word, out)
        self.assertIn('"project": "proj"', out)
        self.assertIn("block:9", out)

    def test_only_template_notices_pass(self):
        ok = zoo_hook.build({"hook_event_name": "Notification", "notification_type": "permission_prompt",
                             "message": "Claude needs your permission to use Bash"}, {})
        odd = zoo_hook.build({"hook_event_name": "Notification", "notification_type": "elicitation_dialog",
                              "message": "Enter your API key for server X"}, {})
        self.assertEqual(ok["notice"], "Claude needs your permission to use Bash")
        self.assertTrue(ok["needs"])
        self.assertEqual(odd["notice"], "")

    def test_long_lines_are_cut(self):
        out = zoo_hook.build({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                              "tool_input": {"description": "x" * 200}}, {})
        self.assertLessEqual(len(out["activity"]), 48)


class States(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.zoo = Zoo(self.clock)

    def state(self, sid="s1"):
        return self.zoo.agents[sid]

    def test_a_turn(self):
        z = self.zoo
        z.on_hook(ev("SessionStart", title="Fix the login", source="startup"))
        self.assertEqual((self.state().state, self.state().title), ("idle", "Fix the login"))
        z.on_hook(ev("UserPromptSubmit"))
        self.assertEqual((self.state().state, self.state().activity), ("working", "Thinking…"))
        z.on_hook(ev("PreToolUse", activity="Editing main.c"))
        self.assertEqual(self.state().activity, "Editing main.c")
        z.on_hook(ev("PermissionRequest", activity="Needs permission: Bash"))
        self.assertEqual(self.state().state, "needs")
        self.assertEqual(self.state().notice, "Claude needs your permission to use Bash")
        z.on_hook(ev("PostToolUse", activity="Bash: Build"))
        self.assertEqual((self.state().state, self.state().activity), ("working", "Bash: Build"))
        z.on_hook(ev("Stop"))
        self.assertEqual(self.state().state, "done")
        self.clock.t += zoo_mod.DONE_TO_IDLE_S + 1
        z.tick()
        self.assertEqual(self.state().state, "idle")

    def test_waiting_for_input_and_failure(self):
        z = self.zoo
        z.on_hook(ev("Notification", notification_type="agent_needs_input", needs=True))
        self.assertEqual((self.state().state, self.state().activity), ("needs", "Waiting for your answer"))
        z.on_hook(ev("StopFailure"))
        self.assertEqual(self.state().state, "failed")

    def test_since_changes_only_with_the_state(self):
        z = self.zoo
        z.on_hook(ev("PreToolUse", activity="Reading a.c"))
        t0 = self.state().since
        self.clock.t += 30
        z.on_hook(ev("PreToolUse", activity="Reading b.c"))
        self.assertEqual(self.state().since, t0)

    def test_subagents(self):
        z = self.zoo
        z.on_hook(ev("PreToolUse", activity="Starting a subagent: Explore"))
        z.on_hook(ev("SubagentStart", agent_id="ag1", agent_type="Explore"))
        z.on_hook(ev("PreToolUse", agent_id="ag1", agent_type="Explore", activity="Reading routes.ts"))
        self.assertEqual(self.state().subs["ag1"]["activity"], "Reading routes.ts")
        self.assertEqual(self.state().activity, "Starting a subagent: Explore")  # the parent's line stays
        z.on_hook(ev("SubagentStop", agent_id="ag1", agent_type="Explore"))
        self.assertEqual(self.state().subs, {})

    def test_session_end_removes_the_agent(self):
        self.zoo.on_hook(ev("UserPromptSubmit"))
        self.zoo.on_hook(ev("SessionEnd", reason="exit"))
        self.assertNotIn("s1", self.zoo.agents)

    def test_desktop_sessions_are_dropped(self):
        self.zoo.on_hook(ev("UserPromptSubmit", entrypoint="claude-desktop"))
        self.zoo.on_hook(ev("PreToolUse", activity="x"))
        self.assertNotIn("s1", self.zoo.agents)
        self.zoo.on_poll([{"sessionId": "s2", "name": "d", "status": "busy", "entrypoint": "claude-desktop"}])
        self.assertNotIn("s2", self.zoo.agents)

    def test_version_changes_only_when_something_shown_changes(self):
        z = self.zoo
        z.on_hook(ev("PreToolUse", activity="Reading a.c"))
        v = z.version
        z.on_hook(ev("PreToolUse", activity="Reading a.c"))
        self.assertEqual(z.version, v)
        z.on_hook(ev("PreToolUse", activity="Reading b.c"))
        self.assertEqual(z.version, v + 1)


class Polling(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.zoo = Zoo(self.clock)

    def test_rows_create_agents(self):
        self.zoo.on_poll([{"sessionId": "a", "name": "pomodoro-40", "kind": "interactive", "status": "busy",
                           "cwd": "/x/pomodoro", "startedAt": 1_000_000_000_000, "pid": 5}])
        a = self.zoo.agents["a"]
        self.assertEqual((a.name, a.project, a.state, a.started), ("pomodoro-40", "pomodoro", "working", 1_000_000_000))

    def test_a_running_agent_keeps_its_status_time(self):
        self.zoo.on_poll([{"sessionId": "a", "status": "idle", "statusUpdatedAt": (self.clock.t - 600) * 1000}])
        a = self.zoo.agents["a"]
        self.assertEqual(a.state, "idle")
        self.assertAlmostEqual(a.since, self.clock.t - 600)

    def test_waiting_means_needs(self):
        self.zoo.on_poll([{"sessionId": "a", "status": "waiting"}])
        self.assertEqual(self.zoo.agents["a"].state, "needs")

    def test_poll_does_not_override_a_fresh_hook(self):
        self.zoo.on_hook(ev("Stop", sid="a"))
        self.zoo.on_poll([{"sessionId": "a", "status": "busy"}])
        self.assertEqual(self.zoo.agents["a"].state, "done")
        self.clock.t += zoo_mod.HOOK_TRUST_S + 1
        self.zoo.on_poll([{"sessionId": "a", "status": "busy"}])
        self.assertEqual(self.zoo.agents["a"].state, "working")

    def test_missing_agents_end(self):
        self.zoo.on_poll([{"sessionId": "a", "status": "idle"}])
        self.clock.t += zoo_mod.GONE_AFTER_S + 1
        self.zoo.on_poll([])
        self.assertNotIn("a", self.zoo.agents)

    def test_background_agents(self):
        self.zoo.on_poll([{"id": "bg", "kind": "background", "state": "blocked", "name": "pi.dev"}])
        self.assertEqual(self.zoo.agents["bg"].state, "needs")
        self.zoo.on_poll([{"id": "bg", "kind": "background", "state": "stopped", "name": "pi.dev"}])
        self.assertNotIn("bg", self.zoo.agents)

    def test_public_shape(self):
        self.zoo.on_hook(ev("PreToolUse", activity="Editing main.c"))
        self.zoo.on_hook(ev("SubagentStart", agent_id="g", agent_type="Plan"))
        msg = self.zoo.public()
        a = msg["agents"][0]
        self.assertEqual(msg["t"], "state")
        self.assertEqual(set(a), {"id", "name", "proj", "kind", "st", "act", "note", "age", "up", "rex", "where", "subs"})
        self.assertEqual((a["st"], a["rex"], len(a["subs"])), ("working", 1, 1))
        self.assertEqual(len(a["id"]), 8)


class LocalHttp(unittest.TestCase):
    def setUp(self):
        self.zoo = Zoo()
        self.taps = []
        self.server = make_http(self.zoo, 0, lambda i, s: self.taps.append(i) or True, lambda a: "")
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers or {})
        r = c.getresponse()
        return r.status, r.read(), r

    def test_hook_then_state(self):
        st, _, _ = self.req("POST", "/hook", ev("PreToolUse", activity="Reading a.c"), {"Content-Type": "application/json"})
        self.assertEqual(st, 204)
        st, body, _ = self.req("GET", "/state")
        self.assertEqual(json.loads(body)["agents"][0]["act"], "Reading a.c")

    def test_other_websites_are_refused(self):
        st, _, _ = self.req("GET", "/state", headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("GET", "/state", headers={"Host": "evil.example:80"})
        self.assertEqual(st, 403)

    def test_a_page_in_a_sandboxed_frame_or_a_file_is_refused(self):
        """A browser sends `Origin: null` from a sandboxed iframe or a file. No web page may read or send anything."""
        st, _, r = self.req("GET", "/state", headers={"Origin": "null"})
        self.assertEqual(st, 403)
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        st, _, _ = self.req("POST", "/hook", ev("PreToolUse", activity="from a page"),
                            {"Origin": "null", "Content-Type": "application/json"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("POST", "/focus", {"id": "x"}, {"Origin": "null", "Content-Type": "application/json"})
        self.assertEqual(st, 403)
        st, _, _ = self.req("OPTIONS", "/hook", headers={"Origin": "null"})
        self.assertNotIn(st, (200, 204))
        self.assertEqual(self.zoo.public()["agents"], [])

    def test_a_form_post_from_a_page_is_refused(self):
        # A plain HTML form sends text/plain or a urlencoded body, with no preflight: only application/json is taken.
        st, _, _ = self.req("POST", "/hook", ev("PreToolUse"), {"Content-Type": "text/plain"})
        self.assertEqual(st, 400)
        self.assertEqual(self.zoo.public()["agents"], [])

    def test_focus(self):
        self.req("POST", "/hook", ev("UserPromptSubmit"), {"Content-Type": "application/json"})
        aid = self.zoo.public()["agents"][0]["id"]
        st, body, _ = self.req("POST", "/focus", {"id": aid}, {"Content-Type": "application/json"})
        self.assertEqual((st, self.taps), (200, [aid]))

    def test_oversized_bodies_are_refused(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        c.request("POST", "/hook", body=b"{" + b" " * 9000 + b"}", headers={"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 400)

    def test_a_bad_length_is_refused_without_waiting_for_a_body(self):
        for length in ("-5", "lots"):
            c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            c.putrequest("POST", "/hook")
            c.putheader("Content-Type", "application/json")
            c.putheader("Content-Length", length)
            c.endheaders()
            self.assertEqual(c.getresponse().status, 400, length)

    def test_the_cli_nudge_needs_no_body(self):
        reloads = []
        self.server.shutdown()
        self.server.server_close()
        self.server = make_http(self.zoo, 0, lambda i, s: True, lambda a: "", reload=lambda: reloads.append(1))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        st, _, _ = self.req("POST", "/reload", headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": "0"})
        self.assertEqual((st, reloads), (204, [1]))

    def test_the_hook_script_end_to_end(self):
        event = {"hook_event_name": "PreToolUse", "session_id": "e2e", "cwd": "/a/b/demo", "tool_name": "Read",
                 "tool_input": {"file_path": "/a/b/demo/notes.txt"}}
        env = dict(os.environ, BENTOBOX_PORT=str(self.port), REX_BLOCK="block:7", REX_SESSION="session:7")
        res = subprocess.run([sys.executable, HOOK], input=json.dumps(event), text=True, env=env, timeout=10)
        self.assertEqual(res.returncode, 0)
        a = self.zoo.agents["e2e"]
        self.assertEqual((a.activity, a.project, a.rex_block), ("Reading notes.txt", "demo", "block:7"))

    def test_the_hook_script_never_fails(self):
        env = dict(os.environ, BENTO_PORT="1")  # nothing listens there
        res = subprocess.run([sys.executable, HOOK], input="not json", text=True, env=env, timeout=10)
        self.assertEqual(res.returncode, 0)


class Codex(unittest.TestCase):
    def test_the_hook_marks_codex_and_keeps_only_file_names_from_a_patch(self):
        patch = "*** Begin Patch\n*** Update File: src/app/secret_plan.py\n@@\n-password = 1\n+password = 2\n*** Add File: notes.md\n*** End Patch"
        out = zoo_hook.build({"hook_event_name": "PreToolUse", "session_id": "c1", "cwd": "/w/shop", "tool_name": "apply_patch",
                              "tool_input": {"command": patch}}, {}, "codex")
        self.assertEqual((out["agent"], out["activity"]), ("codex", "Editing secret_plan.py and 1 more"))
        self.assertNotIn("password", json.dumps(out))
        self.assertNotIn("src/app", json.dumps(out))
        bash = zoo_hook.build({"hook_event_name": "PreToolUse", "session_id": "c1", "tool_name": "Bash",
                               "tool_input": {"command": "rm -rf build"}}, {}, "codex")
        self.assertEqual(bash["activity"], "Running a command")  # never the command

    def test_the_agent_comes_from_the_arguments(self):
        self.assertEqual(zoo_hook.agent_from_args(["zoo_hook.py", "--agent", "codex"]), "codex")
        self.assertEqual(zoo_hook.agent_from_args(["zoo_hook.py"]), "claude")

    def test_a_codex_agent(self):
        clock = Clock()
        zoo = Zoo(clock)
        zoo.on_hook(ev("SessionStart", sid="cx", project="shop", agent="codex", source="startup"))
        zoo.on_hook(ev("PermissionRequest", sid="cx", agent="codex", activity="Needs permission: Bash"))
        a = zoo.public()["agents"][0]
        self.assertEqual((a["agent"], a["name"][:5], a["st"]), ("codex", "shop-", "needs"))
        self.assertEqual(a["note"], "Codex needs your permission to use Bash")
        zoo.on_hook(ev("Interrupt", sid="cx", agent="codex"))
        self.assertEqual(zoo.public()["agents"][0]["st"], "idle")
        self.assertNotIn("agent", Agent("claude-one", 0).public(0))  # Claude agents: no field

    def test_codex_agents_outlive_the_claude_list_but_not_their_process(self):
        clock = Clock()
        zoo = Zoo(clock)
        zoo.on_hook(ev("SessionStart", sid="cx", agent="codex", pid=os.getpid()))
        clock.t += zoo_mod.GONE_AFTER_S + 5
        zoo.on_poll([])  # not in `claude agents`: kept
        zoo.tick()       # its process (this test) lives: kept
        self.assertIn("cx", zoo.agents)
        zoo.agents["cx"].pid = 999999  # a process that doesn't exist
        zoo.tick()
        self.assertNotIn("cx", zoo.agents)


class CodexWindows(unittest.TestCase):
    W = {"pid": 4242, "folder": "shop", "started": 1_790_000_000.0, "rex_session": "session:9", "rex_block": "block:9"}

    def test_a_window_shows_before_its_first_prompt_and_keeps_its_character(self):
        zoo = Zoo(Clock())
        zoo.sync_codex_windows([self.W])
        a = zoo.public()["agents"][0]
        self.assertEqual((a["agent"], a["st"], a["act"], a["rex"]), ("codex", "idle", "Waiting for a prompt", 1))
        # The first prompt: the session joins the window's agent (same id, so the same hat, no poof).
        zoo.on_hook(ev("UserPromptSubmit", sid="s-1", project="shop", agent="codex", rex_session="", rex_block=""))
        b = zoo.public()["agents"]
        self.assertEqual((len(b), b[0]["id"], b[0]["st"]), (1, a["id"], "working"))
        self.assertEqual(zoo.agents[f"codex-window-4242"].rex_block, "block:9")  # the window's pane, not the hook's
        zoo.on_hook(ev("SessionEnd", sid="s-1", project="shop", agent="codex"))
        self.assertEqual(zoo.public()["agents"][0]["st"], "idle")  # the window still runs

    def test_a_session_seen_first_takes_its_window_later(self):
        zoo = Zoo(Clock())
        zoo.on_hook(ev("UserPromptSubmit", sid="s-2", project="shop", agent="codex", rex_session="", rex_block=""))
        zoo.sync_codex_windows([self.W])
        self.assertEqual(len(zoo.agents), 1)
        a = zoo.agents["s-2"]
        self.assertEqual((a.pid, a.rex_block), (4242, "block:9"))

    def test_the_window_goes_when_its_process_ends(self):
        zoo = Zoo(Clock())
        zoo.sync_codex_windows([dict(self.W, pid=999999)])
        zoo.tick()
        self.assertEqual(zoo.agents, {})

    def test_a_chain_of_codex_processes_is_one_window(self):
        rows = "\n".join([
            "19868  1000 ttys016  -zsh",
            "28644 19868 ttys016  codex",                                   # a launcher
            "28645 28644 ttys016  /Users/u/.toolbox/tools/codex/1.0/bin/codex",
            "28648 28645 ttys016  /Users/u/.toolbox/tools/codex/1.0/codex",  # the real one
            "28696 28648 ttys016  builder-mcp",
            "28700 28648 ttys016  /Users/u/.toolbox/tools/codex/1.0/codex",  # a child Codex process
            "65458     1 ??       /Users/u/.toolbox/tools/codex/1.0/bin/codex",  # the background process
            "30000  1001 ttys020  -zsh",
            "30001 30000 ttys020  /opt/homebrew/bin/codex",                 # another window
            "30002 30001 ttys021  codex",                                   # a Codex on another terminal
            "40000     1 ??       /Applications/ChatGPT for Chrome",
        ])
        self.assertEqual(sorted(sources.window_pids(rows)), [28644, 30001, 30002])

    def test_the_hook_reports_the_outermost_codex_of_its_window(self):
        procs = {os.getppid(): (28650, "ttys016", "/bin/sh"),
                 28650: (28648, "ttys016", "/bin/zsh"),
                 28648: (28645, "ttys016", "/Users/u/.toolbox/tools/codex/1.0/codex"),
                 28645: (28644, "ttys016", "/Users/u/.toolbox/tools/codex/1.0/bin/codex"),
                 28644: (19868, "ttys016", "codex"),
                 19868: (1000, "ttys016", "-zsh"),
                 1000: (1, "??", "rex")}
        self.assertEqual(zoo_hook.codex_proc(procs.get), (28644, True))
        procs[28650] = (65458, "??", "/bin/zsh")  # in the shared background process
        procs[65458] = (1, "??", "/Users/u/.toolbox/tools/codex/1.0/bin/codex")
        self.assertEqual(zoo_hook.codex_proc(procs.get), (65458, False))
        self.assertEqual(zoo_hook.codex_proc({}.get), (0, False))

    def test_a_hook_from_the_window_keeps_one_character(self):
        zoo = Zoo(Clock())
        zoo.sync_codex_windows([self.W])
        zoo.on_hook(ev("UserPromptSubmit", sid="s-3", project="shop", agent="codex", pid=4242))
        zoo.sync_codex_windows([self.W])
        self.assertEqual(list(zoo.agents), ["codex-window-4242"])


class Grok(unittest.TestCase):
    # A Grok event: Claude Code's keys and Grok's own camelCase keys (~/.grok/docs/user-guide/10-hooks.md).
    def gev(self, name, **kw):
        return dict({"hookEventName": name.lower(), "hook_event_name": name, "sessionId": "g1", "session_id": "g1",
                     "cwd": "/Users/me/Workspaces/ml-eval", "workspaceRoot": "/Users/me/Workspaces/ml-eval"}, **kw)

    def test_the_hook_knows_grok_by_its_keys_or_its_environment(self):
        self.assertTrue(zoo_hook.is_grok(self.gev("Stop"), {}))
        self.assertTrue(zoo_hook.is_grok({"hook_event_name": "Stop"}, {"GROK_HOOK_EVENT": "stop"}))
        self.assertFalse(zoo_hook.is_grok({"hook_event_name": "Stop", "session_id": "x"}, {}))

    def test_grok_tools_get_safe_activity_lines(self):
        def act(tool, inp):
            out = zoo_hook.build(self.gev("PreToolUse", toolName=tool, toolInput=inp), {}, "grok")
            self.assertEqual((out["agent"], out["session_id"], out["project"]), ("grok", "g1", "ml-eval"))
            return out["activity"]
        self.assertEqual(act("run_terminal_command", {"command": "cat ~/.ssh/id_rsa"}), "Running a command")
        self.assertEqual(act("read_file", {"path": "/Users/me/private/src/main.rs"}), "Reading main.rs")
        self.assertEqual(act("search_replace", {"file_path": "/Users/me/private/plan.md", "old_string": "a"}), "Editing plan.md")
        self.assertEqual(act("grep", {"pattern": "password"}), "Searching the codebase")
        self.assertEqual(act("web_fetch", {"url": "https://docs.x.ai/a?key=1"}), "Fetching docs.x.ai")
        self.assertEqual(act("spawn_subagent", {"prompt": "secret plan", "description": "x"}), "Starting a subagent")
        self.assertEqual(act("linear__save_issue", {"title": "secret"}), "Using linear")
        self.assertEqual(act("todo_write", {"todos": ["secret"]}), "Updating the plan")
        out = json.dumps(zoo_hook.build(self.gev("PreToolUse", toolName="read_file", toolInput={"path": "/Users/me/private/x"}), {}, "grok"))
        for word in ("private", "Workspaces", "ssh"):
            self.assertNotIn(word, out)

    def test_grok_notices_are_never_passed_on(self):
        out = zoo_hook.build(self.gev("Notification", notificationType="permission_prompt", message="Claude needs your permission to use rm -rf x"), {}, "grok")
        self.assertEqual((out["needs"], out["notice"]), (True, ""))

    def test_the_hook_reports_its_grok_window(self):
        procs = {os.getppid(): (500, "ttys003", "/bin/sh"), 500: (400, "ttys003", "grok"), 400: (1, "ttys003", "-zsh")}
        self.assertEqual(zoo_hook.grok_proc(procs.get), (500, True))
        procs[500] = (1, "??", "/Users/me/.grok/bin/grok")  # grok host-server: shared, no terminal
        self.assertEqual(zoo_hook.grok_proc(procs.get), (500, False))
        self.assertEqual(zoo_hook.grok_proc({}.get), (0, False))

    def test_a_grok_window_keeps_its_character_through_new(self):
        zoo = Zoo(Clock())
        zoo.on_hook(ev("SessionStart", sid="g1", project="ml-eval", agent="grok", pid=4343, source="new"))
        a = zoo.public()["agents"][0]
        self.assertEqual((a["agent"], a["name"][:8], a["st"], a["act"]), ("grok", "ml-eval-", "idle", "Waiting for a prompt"))
        zoo.on_hook(ev("PreToolUse", sid="g1", agent="grok", pid=4343, activity="Reading main.rs"))
        self.assertEqual(zoo.public()["agents"][0]["act"], "Reading main.rs")
        zoo.on_hook(ev("SessionEnd", sid="g1", agent="grok", pid=4343))  # /new ends the session...
        zoo.on_hook(ev("SessionStart", sid="g2", agent="grok", pid=4343, source="new"))  # ...and starts another
        b = zoo.public()["agents"]
        self.assertEqual((len(b), b[0]["id"], b[0]["st"]), (1, a["id"], "idle"))
        zoo.on_hook(ev("StopCancelled", sid="g2", agent="grok", pid=4343))
        self.assertEqual(zoo.public()["agents"][0]["act"], "Interrupted")

    def test_a_grok_permission_prompt(self):
        zoo = Zoo(Clock())
        zoo.on_hook(ev("UserPromptSubmit", sid="g1", agent="grok", pid=4343))
        zoo.on_hook(ev("Notification", sid="g1", agent="grok", pid=4343, notification_type="permission_prompt", needs=True, notice=""))
        a = zoo.public()["agents"][0]
        self.assertEqual((a["st"], a["act"]), ("needs", "Needs your permission"))

    def test_windows_come_from_the_session_list(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "active_sessions.json")
            with open(path, "w") as f:
                json.dump([{"session_id": "g1", "pid": 4343, "pid_start": 1793841699898695, "cwd": "/Users/me/private/ml-eval",
                            "opened_at": "2026-11-05T01:21:54Z"},
                           {"session_id": "g9", "pid": 999999, "pid_start": 1, "cwd": "/x"},  # not a running grok
                           "junk", {"pid": "nope"}], f)
            ws = sources.grok_windows(path, pids={4343}, rex_of=lambda pid: ("session:7", "block:7"))
            self.assertEqual(ws, [{"pid": 4343, "session": "g1", "started": 1793841699.898695, "folder": "ml-eval",
                                   "rex_session": "session:7", "rex_block": "block:7"}])
            self.assertEqual(sources.grok_windows(os.path.join(d, "missing.json"), pids=set()), [])

    def test_only_grok_apps_in_a_terminal_are_windows(self):
        rows = "\n".join(["88812 ttys006  grok", "88900 ??       /Users/me/.grok/bin/grok",
                          "86894 ??       /Applications/Grok Bot.app/Contents/MacOS/Grok Bot", "100 ttys001  grok-helper"])
        self.assertEqual(sources.grok_pids(rows), {88812})

    def test_a_window_shows_at_once_and_takes_its_session(self):
        clock = Clock()
        zoo = Zoo(clock)
        zoo.on_hook(ev("UserPromptSubmit", sid="g1", project="ml-eval", agent="grok", rex_session="", rex_block=""))  # no window yet
        w = {"pid": os.getpid(), "session": "g1", "started": clock.t - 60, "folder": "ml-eval",
             "rex_session": "session:7", "rex_block": "block:7"}
        zoo.sync_grok_windows([w])
        self.assertEqual(list(zoo.agents), [f"grok-{os.getpid()}"])
        a = zoo.agents[f"grok-{os.getpid()}"]
        self.assertEqual((a.state, a.rex_block, a.window), ("working", "block:7", True))
        zoo.on_hook(ev("Stop", sid="g1", agent="grok", rex_session="", rex_block=""))  # the session list names its window
        self.assertEqual((len(zoo.agents), a.state), (1, "done"))
        clock.t += zoo_mod.GONE_AFTER_S + 5
        zoo.on_poll([])  # not in `claude agents`: kept
        zoo.tick()       # its process (this test) lives: kept
        self.assertEqual(len(zoo.agents), 1)
        a.pid = 999999   # the window quit
        zoo.tick()
        self.assertEqual((zoo.agents, zoo.grok_alias), ({}, {}))

    def test_the_installer_writes_and_removes_its_own_grok_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hooks", "bentobox.json")
            with contextlib.redirect_stdout(io.StringIO()):
                install.install_grok_hooks(False, grok_dir=d, path=path)
            with open(path) as f:
                hooks = json.load(f)["hooks"]
            self.assertEqual(set(hooks), set(install.GROK_TOOL_EVENTS + install.GROK_OTHER_EVENTS))
            cmd = hooks["PreToolUse"][0]["hooks"][0]
            self.assertEqual(cmd["command"], install.hook_command())  # the same as Claude Code's: Grok runs it once
            self.assertNotIn("async", cmd)
            with contextlib.redirect_stdout(io.StringIO()):
                install.remove_grok_hooks(path)
            self.assertFalse(os.path.exists(path))
            install.install_grok_hooks(False, grok_dir=os.path.join(d, "no-grok"), path=path)  # Grok isn't installed
            self.assertFalse(os.path.exists(path))


class LateEvents(unittest.TestCase):
    """A window's hooks run as it dies: Stop and SessionEnd at the same moment, in either order, and without the
    window's pid once its process is gone. None of them may bring the session back as a new agent."""

    def grok_window(self, clock=None):
        zoo = Zoo(clock or Clock())
        zoo.sync_grok_windows([{"pid": os.getpid(), "session": "g1", "started": 0, "folder": "ml-eval",
                                "rex_session": "session:7", "rex_block": "block:7"}])
        return zoo, zoo.agents[f"grok-{os.getpid()}"]

    def late(self, name, sid="g1", kind="grok"):
        return ev(name, sid=sid, agent=kind, rex_session="", rex_block="")  # no pid: the hook found no window

    def test_a_stop_after_session_end_makes_no_ghost(self):
        zoo, a = self.grok_window()
        zoo.on_hook(self.late("SessionEnd"))
        zoo.on_hook(self.late("Stop"))
        self.assertEqual(list(zoo.agents), [a.sid])
        self.assertEqual(a.activity, "Waiting for a prompt")
        a.pid = 999999  # the window's process is gone
        zoo.tick()
        self.assertEqual(zoo.agents, {})

    def test_a_stop_before_session_end_makes_no_ghost(self):
        zoo, a = self.grok_window()
        zoo.on_hook(self.late("Stop"))
        zoo.on_hook(self.late("SessionEnd"))
        a.pid = 999999
        zoo.tick()
        self.assertEqual(zoo.agents, {})

    def test_events_after_the_window_left_make_no_ghost(self):
        zoo, a = self.grok_window()
        a.pid = 999999
        zoo.tick()
        for name in ("Stop", "StopFailure", "StopCancelled", "PreToolUse", "PostToolUse", "Notification", "SessionEnd"):
            zoo.on_hook(self.late(name))
            self.assertEqual(zoo.agents, {}, name)

    def test_a_stop_with_the_windows_pid_after_session_end_changes_nothing(self):
        zoo, a = self.grok_window()
        zoo.on_hook(ev("SessionEnd", sid="g1", agent="grok", pid=os.getpid()))
        zoo.on_hook(ev("Stop", sid="g1", agent="grok", pid=os.getpid()))
        self.assertEqual((list(zoo.agents), a.state, a.activity), ([a.sid], "idle", "Waiting for a prompt"))

    def test_a_resumed_session_comes_back(self):
        zoo, a = self.grok_window()
        a.pid = 999999
        zoo.tick()
        zoo.on_hook(ev("SessionStart", sid="g1", agent="grok", pid=os.getppid(), source="load"))
        zoo.on_hook(ev("UserPromptSubmit", sid="g1", agent="grok", pid=os.getppid()))
        self.assertEqual([(s, x.state) for s, x in zoo.agents.items()], [(f"grok-{os.getppid()}", "working")])

    def test_a_headless_session_ends_for_good_and_others_still_show(self):
        zoo = Zoo(Clock())
        zoo.on_hook(self.late("UserPromptSubmit"))
        self.assertEqual(list(zoo.agents), ["g1"])
        zoo.on_hook(self.late("SessionEnd"))
        zoo.on_hook(self.late("Stop"))
        self.assertEqual(zoo.agents, {})
        zoo.on_hook(self.late("UserPromptSubmit", sid="g2"))
        self.assertEqual(list(zoo.agents), ["g2"])

    def test_an_ended_session_is_forgotten_after_a_while(self):
        clock = Clock()
        zoo, a = self.grok_window(clock)
        a.pid = 999999
        zoo.tick()
        self.assertIn("g1", zoo.ended_sessions)
        clock.t += zoo_mod.LATE_EVENTS_S + 1
        zoo.tick()
        self.assertEqual(zoo.ended_sessions, {})

    def test_a_dead_codex_window_makes_no_ghost(self):
        zoo = Zoo(Clock())
        zoo.sync_codex_windows([dict(CodexWindows.W, pid=os.getpid())])
        zoo.on_hook(self.late("UserPromptSubmit", sid="s-1", kind="codex") | {"project": "shop"})
        zoo.agents[f"codex-window-{os.getpid()}"].pid = 999999
        zoo.tick()
        zoo.on_hook(self.late("Stop", sid="s-1", kind="codex") | {"project": "shop"})
        self.assertEqual(zoo.agents, {})

    def test_a_late_codex_stop_goes_to_no_other_window(self):
        zoo = Zoo(Clock())
        zoo.sync_codex_windows([dict(CodexWindows.W, pid=os.getpid()), dict(CodexWindows.W, pid=os.getppid())])
        zoo.on_hook(self.late("UserPromptSubmit", sid="s-1", kind="codex") | {"project": "shop"})
        zoo.on_hook(self.late("SessionEnd", sid="s-1", kind="codex") | {"project": "shop"})
        zoo.on_hook(self.late("Stop", sid="s-1", kind="codex") | {"project": "shop"})
        self.assertEqual({a.activity for a in zoo.agents.values()}, {"Waiting for a prompt"})
        self.assertEqual(zoo.codex_alias, {})


class KilledAgents(unittest.TestCase):
    def test_a_killed_claude_session_leaves_at_once(self):
        zoo = Zoo(Clock())
        zoo.on_poll([{"sessionId": "k1", "status": "busy", "pid": os.getpid()}])
        zoo.tick()
        self.assertIn("k1", zoo.agents)  # its process lives
        zoo.agents["k1"].pid = 999999    # killed
        zoo.tick()
        self.assertNotIn("k1", zoo.agents)


class RexFromProcess(unittest.TestCase):
    def test_only_the_rex_ids_are_taken(self):
        line = "claude --x API_KEY=sk-secret REX_SESSION=session:01a1-ff REX_BLOCK=block:02b2-ee HOME=/Users/me"
        self.assertEqual(dict(sources.REX_ENV.findall(line)), {"REX_SESSION": "session:01a1-ff", "REX_BLOCK": "block:02b2-ee"})

    def test_this_process(self):
        session, block = sources.rex_env(os.getpid())
        if os.environ.get("REX_BLOCK"):
            self.assertEqual(block, os.environ["REX_BLOCK"])

    def test_set_rex_fills_only_missing_panes(self):
        zoo = Zoo()
        zoo.on_poll([{"sessionId": "a", "status": "idle", "pid": 42}])
        self.assertEqual(zoo.missing_rex(), [("a", 42)])
        zoo.set_rex("a", "session:1", "block:1")
        self.assertEqual((zoo.missing_rex(), zoo.agents["a"].rex_block), ([], "block:1"))


class FakeRex(rex_mod.Rex):
    """Records the rex commands instead of running them."""

    def __init__(self, select_fails=False):
        super().__init__("rex")
        self.calls, self.select_fails = [], select_fails

    def _run(self, *args, timeout=4):
        self.calls.append(args)
        if "session.select" in args and self.select_fails:
            raise RuntimeError("Rex.app is not accepting actions")
        if args[:2] == ("block", "inspect"):
            return json.dumps({"block": {"window_id": "window:9"}})
        if args[:2] == ("client", "ls"):
            return json.dumps({"clients": [{"client_id": "client:1", "info": {"kind": "app", "name": ""}}]})
        return ""


class RexFocus(unittest.TestCase):
    def test_the_pane_is_looked_up_in_its_own_session(self):
        rex = FakeRex()
        rex.focus("session:other", "block:7")
        self.assertIn(("block", "inspect", "block:7", "-s", "session:other", "--json"), rex.calls)
        self.assertIn(("-C", "client:1", "do", "session.select", "session_id=session:other", "window_id=window:9"), rex.calls)
        self.assertEqual(rex.calls[-1], ("focus", "block:7", "-s", "session:other"))

    def test_the_pane_gets_the_focus_without_remote_control(self):
        rex = FakeRex(select_fails=True)
        with self.assertRaises(RuntimeError):
            rex.focus("session:other", "block:7")
        self.assertEqual(rex.calls[-1], ("focus", "block:7", "-s", "session:other"))


SHELL = "com.superlogical.terminal.shell"


class ListingRex(FakeRex):
    """Answers `rex block ls` with these rows, per session. Any other session fails."""

    def __init__(self, listing):
        super().__init__()
        self.listing = listing

    def _run(self, *args, timeout=4):
        self.calls.append(args)
        session = args[args.index("-s") + 1]
        if session not in self.listing:
            raise RuntimeError("no such session")
        return json.dumps({"blocks": self.listing[session]})


class PaneNames(unittest.TestCase):
    def agent(self):
        zoo = Zoo()
        zoo.on_poll([{"sessionId": "a", "name": "agent-zoo-08", "status": "idle"}])
        zoo.set_rex("a", "session:1", "block:1")
        return zoo

    def name(self, zoo):
        return zoo.public()["agents"][0]["name"]

    def test_a_named_pane_names_its_agent(self):
        zoo = self.agent()
        zoo.set_pane_names({"block:1": "otter"})
        self.assertEqual(self.name(zoo), "otter")
        zoo.on_poll([{"sessionId": "a", "name": "agent-zoo-08", "status": "idle"}])  # the agent list doesn't undo it
        self.assertEqual(self.name(zoo), "otter")

    def test_a_new_name_reaches_the_display(self):
        zoo = self.agent()
        before = zoo.version
        zoo.set_pane_names({"block:1": "otter"})
        self.assertGreater(zoo.version, before)
        before = zoo.version
        zoo.set_pane_names({"block:1": "otter"})
        self.assertEqual(zoo.version, before)  # no change, nothing to send

    def test_a_pane_that_loses_its_name_gives_back_the_agents_name(self):
        zoo = self.agent()
        zoo.set_pane_names({"block:1": "otter"})
        zoo.set_pane_names({"block:1": ""})
        self.assertEqual(self.name(zoo), "agent-zoo-08")

    def test_rexs_own_labels_are_not_names(self):
        rex = ListingRex({"session:1": [
            {"block_id": "block:01a1-f850e2f47faf", "label": "shell", "flavor": SHELL},
            {"block_id": "block:02b2-aaaabbbbcccc", "label": "Shell", "flavor": SHELL},
            {"block_id": "block:03c3-f850e2f47faf", "label": "Block e2f47faf", "flavor": SHELL},
            {"block_id": "block:04d4-111122223333", "label": "", "flavor": SHELL},
            {"block_id": "block:05e5-444455556666", "flavor": SHELL},
            {"block_id": "block:06f6-777788889999", "label": "Block 12345678", "flavor": SHELL},  # not its own id
            {"block_id": "block:07a7-aaaaaaaaaaaa", "label": "otter", "flavor": SHELL},
        ]})
        self.assertEqual(list(rex.pane_names(["session:1"]).values()), ["", "", "", "", "", "Block 12345678", "otter"])

    def test_a_session_rex_cant_list_keeps_its_names(self):
        zoo = self.agent()
        zoo.set_pane_names({"block:1": "otter"})
        zoo.set_pane_names(ListingRex({}).pane_names(["session:1"]))
        self.assertEqual(self.name(zoo), "otter")

    def test_a_listing_without_the_agents_pane_keeps_its_name(self):
        zoo = self.agent()
        zoo.set_pane_names({"block:1": "otter"})
        zoo.set_pane_names({"block:2": "lynx"})
        self.assertEqual(self.name(zoo), "otter")

    def test_only_sessions_with_an_agents_pane_are_listed(self):
        zoo = self.agent()
        zoo.on_poll([{"sessionId": "a", "status": "idle"}, {"sessionId": "b", "status": "idle"}])  # b isn't in Rex
        self.assertEqual(zoo.rex_sessions(), {"session:1"})
        rex = ListingRex({"session:1": []})
        rex.pane_names(zoo.rex_sessions())
        self.assertEqual(rex.calls, [("block", "ls", "-s", "session:1", "--autostart=false", "--json")])
        rex = ListingRex({})
        self.assertEqual(rex.pane_names(set()), {})
        self.assertEqual(rex.calls, [])  # no agent in Rex, no rex command

    def test_a_long_name_is_cut(self):
        zoo = self.agent()
        zoo.set_pane_names({"block:1": "x" * 60})
        self.assertEqual(len(self.name(zoo)), 40)

    def test_codex_and_grok_windows_take_the_name_too(self):
        zoo = Zoo()
        zoo.sync_codex_windows([dict(CodexWindows.W, pid=os.getpid())])
        zoo.sync_grok_windows([{"pid": os.getpid(), "session": "g", "started": 0, "folder": "ml-eval",
                                "rex_session": "session:7", "rex_block": "block:7"}])
        zoo.set_pane_names({"block:9": "lynx", "block:7": "heron"})
        self.assertEqual(sorted(a["name"] for a in zoo.public()["agents"]), ["heron", "lynx"])

    def test_the_poller_names_an_agent_it_finds(self):
        rows = [{"sessionId": "a", "name": "agent-zoo-08", "status": "idle", "pid": os.getpid()}]
        rex = ListingRex({"session:1": [{"block_id": "block:1", "label": "otter", "flavor": SHELL}]})
        zoo = Zoo()
        stop = mock.Mock()
        stop.is_set.side_effect = [False, True]
        with mock.patch.multiple(sources, poll_agents=lambda _: (rows, True), codex_windows=lambda: [],
                                 grok_windows=lambda: [], rex_env=lambda pid: ("session:1", "block:1")):
            sources.poller(zoo, {"claude_bin": "claude", "poll_seconds": 10}, stop, rex)
        self.assertEqual(self.name(zoo), "otter")


class SessionFiles(unittest.TestCase):
    def test_only_safe_fields_and_live_processes(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, f"{os.getpid()}.json"), "w") as f:
                json.dump({"pid": os.getpid(), "sessionId": "x", "name": "n", "messagingSocketPath": "/tmp/s",
                           "status": "busy"}, f)
            with open(os.path.join(d, "999999.json"), "w") as f:
                json.dump({"pid": 999999, "sessionId": "dead"}, f)
            with open(os.path.join(d, "123.abc.key"), "w") as f:
                f.write("secret")
            rows = sources.read_session_files(d)
        self.assertEqual([r["sessionId"] for r in rows], ["x"])
        self.assertNotIn("messagingSocketPath", rows[0])


class BesideTheOldApp(unittest.TestCase):
    def test_bentos_port_is_its_own(self):
        with open(HOOK) as f:
            self.assertIn('os.environ.get("BENTOBOX_PORT") or os.environ.get("BENTO_PORT") or "47824"', f.read())
        self.assertEqual(helpers.config.DEFAULT_HTTP_PORT, 47824)

    def test_the_old_apps_hooks_are_left_alone(self):
        old = {"type": "command", "command": "/usr/bin/python3 /x/agent-zoo/zookeeper/zoo_hook.py", "async": True}
        other = {"type": "command", "command": "my-own-hook"}
        settings = {"model": "private", "hooks": {"Stop": [{"hooks": [old]}, {"hooks": [other]}]}}
        cmd = {"type": "command", "command": install.hook_command(), "async": True}
        install.add_hooks(settings, cmd, install.TOOL_EVENTS, install.OTHER_EVENTS)
        install.add_hooks(settings, cmd, install.TOOL_EVENTS, install.OTHER_EVENTS)  # installing twice changes nothing
        stop = settings["hooks"]["Stop"]
        self.assertEqual(stop[:2], [{"hooks": [old]}, {"hooks": [other]}])
        self.assertEqual(len(stop), 3)
        self.assertTrue(install.ours(stop[2]))
        self.assertEqual(set(settings["hooks"]), set(install.TOOL_EVENTS + install.OTHER_EVENTS))
        self.assertEqual(settings["hooks"]["PreToolUse"][0]["matcher"], "*")

    def test_remove_takes_only_bentos_hooks(self):
        old = {"type": "command", "command": "/usr/bin/python3 /x/agent-zoo/zookeeper/zoo_hook.py"}
        cmd = {"type": "command", "command": install.hook_command()}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            settings = {"model": "private", "hooks": {"Stop": [{"hooks": [old]}]}}
            install.add_hooks(settings, cmd, install.TOOL_EVENTS, install.OTHER_EVENTS)
            with open(path, "w") as f:
                json.dump(settings, f)
            with contextlib.redirect_stdout(io.StringIO()):
                install.remove_hooks(path)
            with open(path) as f:
                after = json.load(f)
            self.assertEqual(after, {"model": "private", "hooks": {"Stop": [{"hooks": [old]}]}})
            self.assertTrue([n for n in os.listdir(d) if ".bentobox-backup-" in n])

    def test_our_entries_are_found_by_their_marker_wherever_the_repo_moved(self):
        moved = {"hooks": [{"type": "command", "command": "/usr/bin/python3 /elsewhere/desktop/agents/hook.py --from-bentobox"}]}
        before_the_marker = {"hooks": [{"type": "command", "command": f"/usr/bin/python3 {install.HOOK}"}]}
        someone_elses = {"hooks": [{"type": "command", "command": "/usr/bin/python3 /x/hook.py --from-bentobox-not"}]}
        self.assertTrue(install.ours(moved))
        self.assertTrue(install.ours(before_the_marker))
        self.assertFalse(install.ours(someone_elses))
        self.assertFalse(install.ours({"hooks": ["not a dict"]}))
        self.assertTrue(install.hook_command(" --agent codex").endswith("hook.py --agent codex --from-bentobox"))

    def test_settings_are_written_whole_and_keep_their_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                f.write("{}")
            os.chmod(path, 0o600)
            install.write_atomic(path, '{"a": 1}\n')
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), oct(0o600))
            with open(path) as f:
                self.assertEqual(json.load(f), {"a": 1})
            self.assertEqual(sorted(os.listdir(d)), ["settings.json"])  # no temporary file left

    def test_the_hook_ignores_the_marker(self):
        self.assertEqual(zoo_hook.agent_from_args(["hook.py", "--from-bentobox"]), "claude")
        self.assertEqual(zoo_hook.agent_from_args(["hook.py", "--agent", "codex", "--from-bentobox"]), "codex")

    def test_the_hook_sends_only_safe_fields(self):
        """The whole message, not just some words in it."""
        out = zoo_hook.build({"hook_event_name": "PreToolUse", "session_id": "s", "cwd": "/Users/me/private/proj",
                              "tool_name": "Bash", "tool_input": {"command": "rm -rf /", "description": "Clean up"},
                              "transcript_path": "/Users/me/x.jsonl", "prompt": "secret"},
                             {"REX_SESSION": "session:1", "REX_BLOCK": "block:1", "CLAUDE_CODE_ENTRYPOINT": "cli", "API_KEY": "secret"})
        self.assertEqual(out, {"event": "PreToolUse", "session_id": "s", "project": "proj", "entrypoint": "cli",
                               "rex_session": "session:1", "rex_block": "block:1", "activity": "Bash: Clean up"})


if __name__ == "__main__":
    unittest.main()
