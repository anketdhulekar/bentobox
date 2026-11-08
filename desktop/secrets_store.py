"""API keys, in the macOS Keychain under the service "bentobox". Keys saved under "bento", as the app was called before,
move over the first time they're asked for.

A key never appears in argv, a log, a config file or an exception. Writing feeds `security -i` through stdin, with the
key in hexadecimal, so no other process can read it from the command line and no quoting can go wrong. Reading takes it
from the tool's stdout, which stays inside this process.

Every error here is a fixed sentence. Nothing from the tool's output, and no original exception, travels with it:
subprocess's TimeoutExpired can carry the output, so the exceptions are raised outside the `except` blocks.
"""
import subprocess

SERVICE = "bentobox"
LEGACY_SERVICE = "bento"  # the app's earlier name
NAMES = ("toggl", "speech")
SECURITY_BIN = "/usr/bin/security"
MAX_KEY = 1024


class SecretStoreError(Exception):
    """A fixed message: never the key or the tool's output."""


def clean_key(raw):
    """Keys pasted from a web page can carry invisible characters or stray spaces. Keeps only plain ASCII key
    characters. Returns (key, how many characters were dropped): only the count may be shown."""
    kept = [c for c in raw if 0x20 < ord(c) < 0x7F]
    return "".join(kept), len(raw) - len(kept)


def _check(name):
    if name not in NAMES:
        raise SecretStoreError("unknown key name")


class MemoryStore:
    """For tests: nothing leaves this object."""

    def __init__(self, **keys):
        self.keys = dict(keys)

    def has(self, name):
        _check(name)
        return bool(self.keys.get(name))

    def get(self, name):
        _check(name)
        return self.keys.get(name) or None

    def set(self, name, value):
        _check(name)
        self.keys[name] = value


class KeychainStore:
    def __init__(self, security_bin=None, service=SERVICE, legacy_service=LEGACY_SERVICE):
        self.bin = security_bin or SECURITY_BIN
        self.service, self.legacy = service, legacy_service

    def _run(self, args, stdin=None):
        res = None
        try:
            res = subprocess.run([self.bin, *args], input=stdin, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        if res is None:
            raise SecretStoreError("couldn't run the macOS security tool")
        return res

    def _exists(self, service, name):
        """Without -w, the tool shows only a key's attributes, never the key."""
        return self._run(["find-generic-password", "-s", service, "-a", name]).returncode == 0

    def _read(self, service, name):
        res = self._run(["find-generic-password", "-s", service, "-a", name, "-w"])
        return (res.stdout.rstrip("\n") or None) if res.returncode == 0 else None

    def _move_legacy(self, name):
        """A key saved under the app's earlier name: saved again under this one, and the old copy removed. True if there
        was one."""
        if not self.legacy or not self._exists(self.legacy, name):
            return False
        value = self._read(self.legacy, name)
        if not value:
            return False
        self.set(name, value)
        return True

    def has(self, name):
        _check(name)
        return self._exists(self.service, name) or self._move_legacy(name)

    def get(self, name):
        _check(name)
        value = self._read(self.service, name)
        if value is None and self._move_legacy(name):
            value = self._read(self.service, name)
        return value

    def set(self, name, value):
        _check(name)
        if not value or len(value) > MAX_KEY:
            raise SecretStoreError("that key is empty or too long")
        line = f"add-generic-password -U -s {self.service} -a {name} -X {value.encode().hex()}\n"
        self._run(["-i"], stdin=line)
        # `security -i` reports at most its last command's status, so read the key back to be sure it's there.
        if self._read(self.service, name) != value:
            raise SecretStoreError("the Keychain didn't accept the key")
        if self.legacy and self._exists(self.legacy, name):  # one copy, under the current name
            self._run(["delete-generic-password", "-s", self.legacy, "-a", name])
