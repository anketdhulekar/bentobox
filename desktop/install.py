#!/usr/bin/env python3
"""Set up, install or remove Bentobox, the desktop app on this Mac. Every command takes --dry-run, which changes nothing.

  install.py setup       create desktop/.venv with the pinned requirements.txt (bleak, for Bluetooth) and build the app
  install.py app         build only the app: desktop/build/Bentobox.app
  install.py hooks       add the hooks to Claude Code (~/.claude/settings.json) and, if they are installed, to Codex
                         (~/.codex/hooks.json) and Grok (~/.grok/hooks/bentobox.json); a backup of each changed file is
                         made first
  install.py launchd     run the app at login and keep it running (through Bentobox.app)
  install.py uninstall   remove the hooks and the launchd agent. The Keychain keys and ~/.config/bentobox stay.

The hooks run agents/hook.py with "async": true, so Claude never waits for them. Each entry carries the argument
--from-bentobox, which hook.py ignores, so uninstall removes only what this script added, wherever the repo has moved
since; entries from before the marker are recognised by the path of hook.py. Other hooks are left alone. Codex skips new
hooks until you trust them: run /hooks in Codex once after installing. Grok runs hooks only in folders you trust (it asks
the first time); it also reads Claude Code's hooks, and runs the same command only once.

The app is a small launcher (macos/launcher.c) that runs Bentobox as its child, so that macOS asks once whether "Bentobox" may
use Bluetooth, also when launchd starts it with no terminal.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "agents", "hook.py")
MARKER = "--from-bentobox"  # in every hook command this script writes: how it finds its own entries again
REQUIREMENTS = os.path.join(HERE, "requirements.txt")
APP_SCRIPT = os.path.join(HERE, "bentobox.py")
SETTINGS = os.path.expanduser("~/.claude/settings.json")
CODEX_HOOKS = os.path.expanduser("~/.codex/hooks.json")
GROK_DIR = os.path.expanduser("~/.grok")
GROK_HOOKS = os.path.join(GROK_DIR, "hooks", "bentobox.json")  # a file of our own
LABEL = "local.bentobox"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
LOG = os.path.expanduser("~/Library/Logs/bentobox.log")
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable  # the hook: standard library only
VENV = os.path.join(HERE, ".venv")
VENV_PYTHON = os.path.join(VENV, "bin", "python")
APP = os.path.join(HERE, "build", "Bentobox.app")
LAUNCHER = os.path.join(APP, "Contents", "MacOS", "bentobox-launcher")

TOOL_EVENTS = ["PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest"]
OTHER_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure",
                "SubagentStart", "SubagentStop", "SessionEnd"]
# Codex's events (developers.openai.com/codex/hooks): the same names where they match, no Notification,
# and Interrupt when you stop a turn.
CODEX_TOOL_EVENTS = ["PreToolUse", "PostToolUse", "PermissionRequest"]
CODEX_OTHER_EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "Interrupt", "SubagentStart", "SubagentStop", "SessionEnd"]
# Grok's events (~/.grok/docs/user-guide/10-hooks.md): Claude Code's names, no PermissionRequest (a permission
# prompt is a Notification), and StopCancelled when you stop a turn.
GROK_TOOL_EVENTS = ["PreToolUse", "PostToolUse", "PostToolUseFailure"]
GROK_OTHER_EVENTS = ["SessionStart", "UserPromptSubmit", "Notification", "Stop", "StopFailure", "StopCancelled",
                     "SubagentStart", "SubagentStop", "SessionEnd"]


def hook_command(args=""):
    return f"{shlex.quote(PYTHON)} {shlex.quote(HOOK)}{args} {MARKER}"


def ours(entry):
    """An entry this script added: it has the marker, or (from before the marker) runs this clone's hook.py."""
    commands = [str(h.get("command", "")) for h in entry.get("hooks", []) if isinstance(h, dict)]
    return any(MARKER in c.split() or HOOK in c for c in commands)


def write_atomic(path, text, mode=None):
    """Writes a whole file, or nothing: a crash midway leaves the old file. The file keeps its permissions."""
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o777
        except FileNotFoundError:
            mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".bentobox-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_settings(path=SETTINGS):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_settings(settings, dry_run, path=SETTINGS):
    text = json.dumps(settings, indent=2) + "\n"
    if dry_run:  # only the hooks: the rest of the file can hold private values
        print(f"{path}:")
        print(json.dumps({"hooks": settings.get("hooks", {})}, indent=2))
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        backup = f"{path}.bentobox-backup-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, backup)
        print(f"Backup: {backup}")
    write_atomic(path, text)
    print(f"Updated {path}")


def add_hooks(settings, command, tool_events, other_events):
    hooks = settings.setdefault("hooks", {})
    for event in tool_events + other_events:
        entries = [e for e in hooks.get(event, []) if not ours(e)]
        entry = {"matcher": "*", "hooks": [command]} if event in tool_events else {"hooks": [command]}
        hooks[event] = entries + [entry]


def install_hooks(dry_run):
    settings = load_settings()
    add_hooks(settings, {"type": "command", "command": hook_command(), "async": True, "timeout": 5},
              TOOL_EVENTS, OTHER_EVENTS)
    save_settings(settings, dry_run)
    print("New Claude Code sessions use the hooks. Sessions that already run pick them up when they reload settings.")
    if not os.path.isdir(os.path.dirname(CODEX_HOOKS)):
        return
    codex = load_settings(CODEX_HOOKS)
    add_hooks(codex, {"type": "command", "command": hook_command(" --agent codex"), "async": True, "timeout": 5},
              CODEX_TOOL_EVENTS, CODEX_OTHER_EVENTS)
    save_settings(codex, dry_run, CODEX_HOOKS)
    print("Codex: run /hooks in Codex once to trust the new hooks (Codex skips new hooks until then).")


def install_grok_hooks(dry_run, grok_dir=GROK_DIR, path=GROK_HOOKS):
    """The same command as Claude Code's, without "async" (Grok doesn't know it and would report it). Grok also
    runs the hooks in ~/.claude/settings.json; it runs a command that both files name only once."""
    if not os.path.isdir(grok_dir):
        return
    grok = {}
    add_hooks(grok, {"type": "command", "command": hook_command(), "timeout": 5}, GROK_TOOL_EVENTS, GROK_OTHER_EVENTS)
    save_settings(grok, dry_run, path)
    print("Grok: the hooks run in folders you trust (Grok asks the first time you start it in a folder).")


def remove_hooks(path=SETTINGS, dry_run=False):
    settings = load_settings(path)
    hooks = settings.get("hooks", {})
    if not any(ours(e) for entries in hooks.values() for e in entries):
        return
    for event in list(hooks):
        kept = [e for e in hooks[event] if not ours(e)]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    save_settings(settings, dry_run, path)


def remove_grok_hooks(path=GROK_HOOKS, dry_run=False):
    """Grok's hook file is ours alone: delete it, if it still holds our hook."""
    if os.path.exists(path) and any(ours(e) for es in load_settings(path).get("hooks", {}).values() for e in es):
        if dry_run:
            print(f"Would remove {path}")
            return
        os.remove(path)
        print(f"Removed {path}")


def find_python():
    """A Python 3.10 or newer for the venv (bleak needs it); the system's /usr/bin/python3 is too old. Python 3.13 first:
    a newer one may have no wheels yet for bleak's dependencies."""
    for name in ("python3.13", "python3.12", "python3.11", "python3.10", "python3"):
        exe = shutil.which(name)
        if exe and subprocess.run([exe, "-c", "import sys; sys.exit(sys.version_info < (3, 10))"]).returncode == 0:
            return exe
    return None


def build_app(dry_run=False):
    info = os.path.join(HERE, "macos", "Info.plist")
    source = os.path.join(HERE, "macos", "launcher.c")
    if dry_run:
        print(f"Would build {APP}: {info} and {source}, compiled with cc and signed ad hoc")
        return
    if not shutil.which("cc"):
        sys.exit("Building the app needs a C compiler: xcode-select --install")
    os.makedirs(os.path.dirname(LAUNCHER), exist_ok=True)
    shutil.copy2(info, os.path.join(APP, "Contents", "Info.plist"))
    subprocess.run(["cc", "-O2", "-Wall", "-o", LAUNCHER, source], check=True)
    subprocess.run(["codesign", "--force", "-s", "-", APP], check=True, capture_output=True)  # ad hoc
    print(f"Built {APP}")


def setup(dry_run=False):
    python = find_python()
    if dry_run:
        if not os.path.exists(VENV_PYTHON):
            print(f"Would create {VENV} with {python or 'Python 3.10 or newer (none found)'}")
        print(f"Would install {os.path.basename(REQUIREMENTS)} (bleak) into {VENV}")
        build_app(dry_run=True)
        return
    if not os.path.exists(VENV_PYTHON):
        if not python:
            sys.exit("Install Python 3.10 or newer first (for example: brew install python@3.13).")
        subprocess.run([python, "-m", "venv", VENV], check=True)
    subprocess.run([VENV_PYTHON, "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
    subprocess.run([VENV_PYTHON, "-m", "pip", "install", "-q", "-r", REQUIREMENTS], check=True)
    print(f"Python environment: {VENV} (with {os.path.basename(REQUIREMENTS)})")
    build_app()
    print("Next: bentobox.py init, then bentobox.py run in a Rex tab, or install.py launchd. The first time, choose Pair a new "
          "Mac on the board, and type the code it shows into the macOS dialog.")


PLIST_TEXT = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array><string>{launcher}</string><string>{python}</string><string>{script}</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>{path}</string></dict>
</dict>
</plist>
"""


def install_launchd(dry_run):
    path = ":".join(p for p in [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin",
                                "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    if not dry_run and not (os.path.exists(VENV_PYTHON) and os.path.exists(LAUNCHER)):
        sys.exit("Run install.py setup first (the Python environment and the app).")
    text = PLIST_TEXT.format(label=LABEL, launcher=LAUNCHER, python=VENV_PYTHON, script=APP_SCRIPT, log=LOG, path=path)
    if dry_run:
        print(f"Would write {PLIST} and start {LABEL}:")
        print(text)
        return
    write_atomic(PLIST, text, mode=0o644)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, PLIST], check=True)
    print(f"Started {LABEL}. Log: {LOG}")
    print("If macOS asks whether Bentobox may use Bluetooth, click Allow.")


def uninstall(dry_run=False):
    remove_hooks(dry_run=dry_run)
    if os.path.exists(CODEX_HOOKS):
        remove_hooks(CODEX_HOOKS, dry_run=dry_run)
    remove_grok_hooks(dry_run=dry_run)
    if dry_run:
        print(f"Would stop {LABEL}" + (f" and remove {PLIST}" if os.path.exists(PLIST) else ""))
        return
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
        print(f"Removed {PLIST}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["setup", "app", "hooks", "launchd", "uninstall"])
    ap.add_argument("--dry-run", action="store_true", help="print the result instead of changing anything")
    a = ap.parse_args()
    if a.what == "setup":
        setup(a.dry_run)
    elif a.what == "app":
        build_app(a.dry_run)
    elif a.what == "hooks":
        install_hooks(a.dry_run)
        install_grok_hooks(a.dry_run)
    elif a.what == "launchd":
        install_launchd(a.dry_run)
    else:
        uninstall(a.dry_run)


if __name__ == "__main__":
    main()
