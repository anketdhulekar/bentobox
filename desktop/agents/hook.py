#!/usr/bin/env python3
"""The hook for Bentobox, for Claude Code, Codex and Grok.

Claude Code (or Codex, with --agent codex) runs this for each hook event, configured with
"async": true, so it never delays the agent. It reads the event from stdin, keeps only short,
safe fields, and posts them to the Bentobox app on 127.0.0.1.

Grok runs it too: from its own ~/.grok/hooks/bentobox.json, and from ~/.claude/settings.json
(Grok reads Claude Code's hooks). Both are the same command, so Grok runs it once per event. Grok's
events are recognised by their camelCase keys ("hookEventName") or GROK_HOOK_EVENT. Grok has no
"async": it waits for the hook, which takes a few milliseconds.

It never sends prompts, commands, file contents, file paths, tool output, transcript paths
or the agent's messages. The activity line is built from the tool name plus, at most, a short
safe field (a Bash command's description, a file's base name, a web domain).

It always exits 0, so a missing app never shows an error in Claude Code. It runs on the system Python, which can be
as old as 3.9, and it uses only the standard library.
"""
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

MAX_ACT = 48
MAX_NAME = 40

# Notification types that mean "Claude needs you" (from the Claude Code hooks reference).
NEEDS_TYPES = {"permission_prompt", "agent_needs_input", "elicitation_dialog", "elicitation_url_dialog"}


def short(text, limit=MAX_ACT):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def base(path):
    return os.path.basename(str(path or "").rstrip("/")) or "a file"


def fname(inp):
    """The base name of the file a tool works on (Claude says file_path, Grok also path or target_file)."""
    return base(inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or inp.get("target_file"))


def activity(tool, inp):
    """A short line that says what the tool does, without private details."""
    inp = inp if isinstance(inp, dict) else {}
    # Claude Code's tools, and Grok's names for the same kinds of tool
    if tool in ("Bash", "run_terminal_command", "bash", "Shell"):
        desc = inp.get("description")
        return short(f"Bash: {desc}") if desc else "Running a command"
    if tool in ("Edit", "MultiEdit", "NotebookEdit", "search_replace", "hashline_edit", "StrReplace", "edit_file"):
        return short(f"Editing {fname(inp)}")
    if tool in ("Write", "write", "write_file"):
        return short(f"Writing {fname(inp)}")
    if tool in ("Read", "read_file"):
        return short(f"Reading {fname(inp)}")
    if tool in ("Grep", "Glob", "LS", "grep", "grep_search", "list_dir", "glob", "lsp"):
        return "Searching the codebase"
    if tool in ("WebSearch", "web_search", "web_search_preview"):
        return "Searching the web"
    if tool in ("WebFetch", "web_fetch", "Fetch"):
        host = urllib.parse.urlparse(str(inp.get("url") or "")).hostname or "the web"
        return short(f"Fetching {host}")
    if tool in ("Task", "Agent", "spawn_subagent", "task"):
        kind = inp.get("subagent_type")
        return short(f"Starting a subagent: {kind}") if kind else "Starting a subagent"
    if tool in ("TodoWrite", "todo_write"):
        return "Updating the plan"
    if tool == "Skill":
        name = inp.get("skill") or inp.get("name")
        return short(f"Using the {name} skill") if name else "Using a skill"
    # Codex's tools
    if tool == "apply_patch":  # only the file names from the patch's headers, never its content
        names = [base(m) for m in re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", str(inp.get("command") or ""), re.M)]
        if not names:
            return "Editing files"
        return short(f"Editing {names[0]}" + (f" and {len(names) - 1} more" if len(names) > 1 else ""))
    if tool == "update_plan":
        return "Updating the plan"
    if tool == "view_image":
        return "Looking at an image"
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return short(f"Using {parts[1]}") if len(parts) > 2 else "Using a tool"
    if tool in ("use_tool", "search_tool", "CallMcpTool"):
        return "Using a tool"
    if "__" in tool:  # Grok names an MCP tool <server>__<tool>
        return short(f"Using {tool.split('__')[0]}")
    return short(tool or "Working")


def notice(event):
    """Claude's notice text, only when it is one of its fixed templates."""
    msg = str(event.get("message") or "")
    return short(msg, 64) if msg.startswith("Claude ") else ""


def ps_table():
    """pid -> (ppid, tty, comm) for every process, from one `ps` run: walking up a few parents costs one call, not one
    per parent."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,tty=,comm="], capture_output=True, text=True, timeout=1).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    table = {}
    for line in out.splitlines():
        try:
            pid, ppid, tty, comm = line.split(None, 3)
            table[int(pid)] = (int(ppid), tty, comm)
        except ValueError:
            continue
    return table


def grok_proc(row=None):
    """The Grok window that runs this hook: (pid, has a terminal), from the nearest `grok` ancestor.

    The Grok app in a terminal runs hooks as its own children, with its environment (so the Rex pane or Herdr
    terminal is the window's). A Grok without a terminal (`grok host-server`, shared by several sessions) gives no
    pid, and its pane may be stale: then the app matches the session by ~/.grok/active_sessions.json.
    row: pid -> (ppid, tty, comm) or None; by default, from one ps_table()."""
    row = row or ps_table().get
    pid = os.getppid()
    for _ in range(10):
        r = row(pid)
        if r is None:
            break
        ppid, tty, comm = r
        if os.path.basename(comm) == "grok":
            return pid, tty not in ("??", "-", "")
        if ppid <= 1:
            break
        pid = ppid
    return 0, False


def codex_proc(row=None):
    """The Codex process that runs this hook: (pid, has a terminal). It is the nearest `codex` ancestor or,
    when that one's parents are `codex` processes on the same terminal (a launcher chain), the outermost
    of them: the same process the app takes as the window (window_pids in sources.py).

    Codex often runs hooks in its shared background process (no terminal), whose environment holds the
    Rex pane or Herdr terminal it was started from, long ago. Then the app finds the session's own Codex window."""
    row = row or ps_table().get
    pid, found, found_tty = os.getppid(), 0, ""
    for _ in range(10):
        r = row(pid)
        if r is None:
            break
        ppid, tty, comm = r
        if found:
            if os.path.basename(comm) != "codex" or tty != found_tty:
                break
            found = pid
        elif os.path.basename(comm).startswith("codex"):
            found, found_tty = pid, tty
        if ppid <= 1:
            break
        pid = ppid
    return (found, found_tty not in ("??", "-", "")) if found else (0, False)


def get(event, *keys):
    """The first key that is set: Claude Code uses snake_case keys; Grok sends camelCase keys too."""
    for k in keys:
        if event.get(k):
            return event[k]
    return ""


def is_grok(event, env):
    return "hookEventName" in event or bool(env.get("GROK_HOOK_EVENT"))


def build(event, env, agent="claude"):
    name = event.get("hook_event_name") or ""
    out = {
        "event": name,
        "session_id": get(event, "session_id", "sessionId"),
        "project": base(event.get("cwd")) if event.get("cwd") else "",
        "entrypoint": env.get("CLAUDE_CODE_ENTRYPOINT", ""),
        "rex_session": env.get("REX_SESSION", ""),
        "rex_block": env.get("REX_BLOCK", ""),
        "hc_terminal": env.get("HC_TERMINAL_ID", ""),
    }
    if agent in ("codex", "grok"):
        out["agent"] = agent
    agent_id = get(event, "agent_id", "agentId", "subagent_id", "subagentId")
    agent_type = get(event, "agent_type", "agentType", "subagent_type", "subagentType")
    if agent_id:  # the event comes from inside a subagent
        out["agent_id"] = str(agent_id)
        out["agent_type"] = short(agent_type or "subagent", 24)
    tool = str(get(event, "tool_name", "toolName"))
    if name == "SessionStart":
        out["title"] = short(event.get("session_title") or "", MAX_NAME)
        out["source"] = event.get("source") or ""
    elif name in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
        out["activity"] = activity(tool, get(event, "tool_input", "toolInput"))
    elif name == "PermissionRequest":
        out["activity"] = short(f"Needs permission: {tool or 'a tool'}")
    elif name == "Notification":
        ntype = get(event, "notification_type", "notificationType")
        out["notification_type"] = ntype
        out["notice"] = notice(event) if agent == "claude" else ""  # only Claude's fixed templates are known
        out["needs"] = ntype in NEEDS_TYPES
    elif name in ("SubagentStart", "SubagentStop"):
        out["agent_id"] = str(agent_id or "")
        out["agent_type"] = short(agent_type or "subagent", 24)
    elif name == "SessionEnd":
        out["reason"] = short(event.get("reason") or "", 24)
    return out


# install.py adds --from-bentobox to every command it writes, only so it can find its own entries again. It means nothing
# here, and any other argument but --agent is ignored too.
def agent_from_args(argv):
    return "codex" if "--agent" in argv and argv[argv.index("--agent") + 1:argv.index("--agent") + 2] == ["codex"] else "claude"


def main():
    try:
        event = json.loads(sys.stdin.read() or "{}")
        if not isinstance(event, dict):
            return
        agent = "grok" if is_grok(event, os.environ) else agent_from_args(sys.argv)
        out = build(event, os.environ, agent)
        if agent == "grok":
            pid, window = grok_proc()
            if window:  # the Grok app in a terminal: its pid and pane are the session's
                out["pid"] = pid
            else:       # a shared Grok process: its pane may be stale; the app matches the session
                out["rex_session"] = out["rex_block"] = out["hc_terminal"] = ""
        elif agent == "codex":
            pid, window = codex_proc()
            if window:  # Codex runs the hook in its window: its pid and pane are the session's
                out["pid"] = pid
            else:       # in the shared background process: its pane is stale; the app matches a window
                out["rex_session"] = out["rex_block"] = out["hc_terminal"] = ""
        body = json.dumps(out).encode()
        port = int(os.environ.get("BENTOBOX_PORT") or os.environ.get("BENTO_PORT") or "47824")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/hook", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        # No proxy: these events stay on this Mac, whatever the system's proxy settings say.
        urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=0.5).read()
    except Exception:
        pass  # the app isn't running, or the event was odd: Claude must never notice


if __name__ == "__main__":
    main()
    sys.exit(0)
