"""Speech-to-text: a multipart WAV upload, with the Toggl project names as keywords. Transcripts are user data: the mic can pick up conversations in the room, so neither they
nor the service's responses are logged or saved."""
import json
import os

import http_call
from util import log

# The model name and the `keywords[]` field were proven against this service by an earlier firmware that called it
# directly: they aren't from OpenAI's published reference, which documents `model` and `prompt`. If the service
# turns them down, every clip gets "failed", and the log shows the HTTP status.
STT_MODEL = "gpt-transcribe"
TIMEOUT_S = 30
KEYWORD_ROOM = 1848      # bytes of keyword fields at most; project names past this are left out
MAX_TEXT_BYTES = 95      # the board's task length (LINK_TASK_LEN), less its terminator


def tidy(text):
    """A transcript as a task name: trimmed, with single spaces and no trailing period."""
    return " ".join(str(text or "").split()).rstrip(". ")


def cut(text, limit=MAX_TEXT_BYTES):
    """At most `limit` bytes of UTF-8, with no half character at the end, and no trailing period."""
    return text.encode()[:limit].decode("utf-8", "ignore").rstrip(". ")


def match_project(text, projects):
    """The id of the longest project name found in the text as whole words, ignoring case; 0 if none."""
    best, best_len, low = 0, 0, text.lower()
    for project in projects:
        name = str(project.get("name") or "").lower()
        if not name or len(name) <= best_len:
            continue
        at = low.find(name)
        while at >= 0:
            before = at == 0 or not low[at - 1].isalnum()
            after = at + len(name) == len(low) or not low[at + len(name)].isalnum()
            if before and after:
                best, best_len = project["id"], len(name)
                break
            at = low.find(name, at + 1)
    return best


def _field(boundary, name, value):
    return f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()


def multipart(boundary, wav, keywords):
    head = _field(boundary, "model", STT_MODEL) + _field(boundary, "response_format", "json")
    for name in keywords:
        part = _field(boundary, "keywords[]", name.replace("\r", " ").replace("\n", " "))
        if len(head) + len(part) > KEYWORD_ROOM:
            break
        head += part
    head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="task.wav"\r\n'
             "Content-Type: audio/wav\r\n\r\n").encode()
    return head + wav + f"\r\n--{boundary}--\r\n".encode()


class SpeechClient:
    def __init__(self, url, get_key, timeout=TIMEOUT_S):
        self.url, self.get_key, self.timeout = url, get_key, timeout
        self.no_keywords = False  # the service turned down our keyword list once; don't send it again

    def _post(self, wav, keywords, key):
        boundary = "----bentobox-" + os.urandom(12).hex()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"}
        body = multipart(boundary, wav, keywords)
        status, content, _ = http_call.call("POST", self.url, headers, body, self.timeout)
        if status < 0:  # dropped mid-upload: one more try
            log("speech: connection dropped, trying again")
            status, content, _ = http_call.call("POST", self.url, headers, body, self.timeout)
        return status, content

    def transcribe(self, wav, keywords=()):
        """The transcript, tidied ("" if nothing was heard), or None if it failed."""
        key = self.get_key()
        if not key:
            return None
        names = list(keywords) if not self.no_keywords else []
        status, content = self._post(wav, names, key)
        if status == 400 and names:
            log("speech: the keyword list was turned down; trying without it")
            self.no_keywords = True
            status, content = self._post(wav, [], key)
        if status != 200:
            log(f"speech: {'no answer' if status < 0 else f'HTTP {status}'}")
            return None
        try:
            text = json.loads(content).get("text")
        except (ValueError, AttributeError):
            log("speech: unreadable answer")
            return None
        return tidy(text) if isinstance(text, str) else ""
