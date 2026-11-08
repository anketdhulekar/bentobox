"""The config and state files in ~/.config/bentobox. Nothing secret lives here: keys are in the Keychain.

The folder was ~/.config/bento while the app was called Bento: the first time it's needed, an old folder moves over
whole, with its settings, the Toggl runs still on their way, the shared clock and the lock code."""
import json
import os

from util import write_json_atomic

DEFAULT_HTTP_PORT = 47824  # for the agent hooks and the preview, on 127.0.0.1 only
DEFAULTS = {
    "link": "ble",                # "ble": the board; "tcp": the simulator or fake_board.py
    "display_host": "127.0.0.1",  # for "tcp" only
    "display_port": 7777,
    "http_port": DEFAULT_HTTP_PORT,
    "poll_seconds": 10,
    "claude_bin": "",
    "rex_bin": "",
    "toggl_org": 0,               # the Toggl organization id (not a secret)
    "toggl_api": "https://focus.toggl.com/api",
    "stt_url": "https://api.openai.com/v1/audio/transcriptions",
    "security_bin": "/usr/bin/security",
}
ADVANCED = ("toggl_api", "stt_url", "security_bin")  # written to the file only if someone changed them


def config_dir():
    home = os.path.expanduser("~")
    path, legacy = os.path.join(home, ".config", "bentobox"), os.path.join(home, ".config", "bento")
    if not os.path.exists(path) and os.path.isdir(legacy):
        try:
            os.rename(legacy, path)
        except OSError:
            return legacy  # rather the old folder than an empty new one
    return path


def config_path():
    return os.path.join(config_dir(), "config.json")


def state_path(name):
    return os.path.join(config_dir(), name)


def load_config(path=None):
    cfg = dict(DEFAULTS)
    try:
        with open(path or config_path()) as f:
            saved = json.load(f)
    except FileNotFoundError:
        return cfg
    except ValueError:
        raise SystemExit(f"{path or config_path()} isn't valid JSON. Fix or delete it.") from None
    if isinstance(saved, dict):
        cfg.update(saved)
    return cfg


def save_config(cfg, path=None):
    out = {k: v for k, v in cfg.items() if k not in ADVANCED or v != DEFAULTS[k]}
    write_json_atomic(path or config_path(), out)
