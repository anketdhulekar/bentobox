"""Small helpers shared by the whole desktop app."""
import json
import os
import sys
import tempfile
import time

log_stream = None  # None: stdout, which launchd sends to the log file. The tests read the log from a buffer.
LOG_KEEP_BYTES = 5 * 1024 * 1024


def log(msg):
    """One line for the log. Never pass a secret, a lock code, a transcript or a pairing code to this, and never a whole
    message from the board."""
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, file=log_stream or sys.stdout, flush=True)


def rotate_log(path, limit=LOG_KEEP_BYTES, stream=None):
    """At startup, when launchd sends stdout to `path` and it has grown past `limit`: keep it as `path`.1 (one old file)
    and start the log again. launchd opens the file for appending, so after the truncation new lines start at the top.
    Does nothing if stdout is anything else, such as a terminal. True if it rotated."""
    stream = stream or sys.stdout
    try:
        out, on_disk = os.fstat(stream.fileno()), os.stat(path)
    except (OSError, ValueError, AttributeError):
        return False
    if (out.st_dev, out.st_ino) != (on_disk.st_dev, on_disk.st_ino) or on_disk.st_size <= limit:
        return False
    try:
        stream.flush()
        with open(path, "rb") as src, open(path + ".1", "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        os.ftruncate(stream.fileno(), 0)
        return True
    except OSError:
        return False


def short(text, limit):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def write_json_atomic(path, data):
    """Saves JSON so that a crash leaves either the old file or the new one, readable only by this user. Once this
    returns, the data is on the disk. Raises OSError if it can't be saved."""
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-")  # mkstemp's file is 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
