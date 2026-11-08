"""Collects a voice clip from the board, and answers with a `transcript`.

The board records first and sends afterwards. A clip with missing packets gets the error "failed", because a transcript
of half a sentence would be wrong in a way nobody would notice. If the board disconnects mid-clip, the clip is dropped:
nobody is left to answer. Several boards can send clips at once: each has its own.
"""
import base64
import threading
import time

from fields import whole
from speech import adpcm, audio
from speech.stt import cut, match_project
from util import log

MAX_CLIP_S = 120                   # a clip's audio, at most: more than any spoken task, and a limit on memory
MAX_CLIP_BYTES = 48000 * MAX_CLIP_S // 2 + 65536  # what that is in ADPCM at the highest rate, with room for packet headers
IDLE_S = 30                        # a clip that stops arriving, with the board still connected, has failed
DEFAULT_RATE = 16000
DONE_KEEP = 16


class Clip:
    def __init__(self, vid, rate, session, now):
        self.vid, self.rate, self.session, self.heard, self.started = vid, rate, session, now, now
        self.packets = {}
        self.size = 0
        self.samples = 0
        self.broken = False


_int = whole


def decode_packets(packets, count):
    """The clip's samples, or None if any packet is missing or unusable."""
    if len(packets) != count or any(seq not in packets for seq in range(count)):
        return None
    pcm = []
    for seq in range(count):
        predictor, index, samples, data = packets[seq]
        part = adpcm.decode(data, samples, predictor, index)
        if len(part) != samples:
            return None
        pcm += part
    return pcm


class VoiceDesk:
    """Voice clips from every connected board. Each board numbers its own clips, so a clip is known by (session, vid),
    where the session is the board's, and each board has at most one clip arriving at a time."""

    def __init__(self, send, transcribe, has_key, projects, clock=time.monotonic, spawn=None):
        self.send = send                 # send(message, session): dropped if that session has ended
        self.transcribe = transcribe     # transcribe(wav, keywords) -> text, or None if it failed
        self.has_key = has_key
        self.projects = projects         # () -> the cached project rows
        self.clock = clock
        self.spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True, name="voice").start())
        self.clips = {}                  # session -> the Clip arriving from that board
        self.done = []                   # (session, vid) of recent clips that got or will get an answer, oldest first
        self.cancelled = []              # (session, vid) of recent clips the board gave up on, oldest first
        self.lock = threading.Lock()

    @property
    def clip(self):
        """The clip started last, of those still arriving, or None: one board's view, for tests and the log."""
        with self.lock:
            return max(self.clips.values(), key=lambda c: c.started, default=None)

    @staticmethod
    def _remember(items, key):
        if key in items:
            items.remove(key)
        items.append(key)
        del items[:-DONE_KEEP]

    def handle(self, msg, session):
        kind, vid = msg.get("t"), _int(msg.get("vid"))
        if vid is None:
            return
        key = (session, vid)
        job = None
        with self.lock:
            if kind == "voice_start":
                rate = _int(msg.get("rate"))
                self.clips[session] = Clip(vid, rate if rate and 4000 <= rate <= 48000 else DEFAULT_RATE, session, self.clock())
                if key in self.cancelled:
                    self.cancelled.remove(key)
                if key in self.done:
                    self.done.remove(key)
            elif kind == "audio":
                self._add_packet(session, vid, msg)
            elif kind == "voice_cancel":
                clip = self.clips.get(session)
                if clip and clip.vid == vid:
                    del self.clips[session]
                self._remember(self.cancelled, key)  # an answer on its way is dropped
            elif kind == "voice_end":
                job = self._end(session, vid, msg)
        if job:
            self.spawn(job)

    def _add_packet(self, session, vid, msg):
        clip = self.clips.get(session)
        if not clip or clip.vid != vid:
            return
        clip.heard = self.clock()
        seq, predictor, index, samples = _int(msg.get("seq")), _int(msg.get("p")), _int(msg.get("i")), _int(msg.get("n"))
        try:
            data = base64.b64decode(msg.get("b64") or "", validate=True)
        except (ValueError, TypeError):
            data = None
        if (data is None or seq is None or seq < 0 or predictor is None or not -32768 <= predictor <= 32767
                or index is None or not 0 <= index <= 88 or samples is None or not 0 <= samples <= len(data) * 2):
            clip.broken = True
            return
        if seq in clip.packets:  # sent twice: the first copy stands
            return
        clip.size += len(data)
        clip.samples += samples
        if clip.samples > clip.rate * MAX_CLIP_S or clip.size > MAX_CLIP_BYTES:
            clip.broken = True
            clip.packets.clear()
            return
        clip.packets[seq] = (predictor, index, samples, data)

    def _end(self, session, vid, msg):
        key = (session, vid)
        clip = self.clips.get(session)
        if clip is None or clip.vid != vid:
            if key not in self.done:  # its start was missed, or it was dropped: the board still waits for an answer
                self.send({"t": "transcript", "vid": vid, "error": "failed"}, session)
            return None
        del self.clips[session]
        self._remember(self.done, key)
        count = _int(msg.get("packets"))
        return lambda: self._answer(clip, count, session)

    def _answer(self, clip, count, session):
        try:
            answer = self._work(clip, count)
        except Exception as e:
            log(f"speech: {type(e).__name__} on a clip")
            answer = {"error": "failed"}
        with self.lock:
            key = (session, clip.vid)
            if key in self.cancelled:
                self.cancelled.remove(key)
                return
        self.send({"t": "transcript", "vid": clip.vid, **answer}, session)

    def _work(self, clip, count):
        if clip.broken or count is None or count < 0:
            return {"error": "failed"}
        if count == 0 and not clip.packets:
            return {"error": "no_speech"}
        pcm = decode_packets(clip.packets, count)
        if pcm is None:
            log(f"speech: clip {clip.vid} is missing packets ({len(clip.packets)} of {count})")
            return {"error": "failed"}
        if not self.has_key():
            return {"error": "failed"}
        prepared = audio.prepare(pcm, clip.rate)
        if prepared is None:
            return {"error": "no_speech"}
        projects = self.projects()
        text = self.transcribe(audio.wav_bytes(prepared, clip.rate), [p["name"] for p in projects])
        if text is None:
            return {"error": "failed"}
        if not text:
            return {"error": "no_speech"}
        answer = {"text": cut(text)}
        project = match_project(text, projects)
        if project:
            answer["project"] = project
        return answer

    def disconnected(self, session=None):
        """A board went away: its clip, if one is still arriving, is dropped, and every other board's goes on. With no
        session, every clip. An answer on its way is dropped by send()."""
        with self.lock:
            if session is None:
                self.clips.clear()
            else:
                self.clips.pop(session, None)

    def tick(self):
        with self.lock:
            now = self.clock()
            for session, clip in [(s, c) for s, c in self.clips.items() if now - c.heard > IDLE_S]:
                del self.clips[session]
                self._remember(self.done, (session, clip.vid))
                log(f"speech: clip {clip.vid} stopped arriving")
                self.send({"t": "transcript", "vid": clip.vid, "error": "failed"}, session)
