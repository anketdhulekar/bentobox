"""Cleans up a clip and wraps it in a WAV."""
import math
import struct


def wav_bytes(pcm, rate):
    data = struct.pack(f"<{len(pcm)}h", *pcm)
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
                       b"data", len(data)) + data


def prepare(pcm, rate):
    """Removes hum, trims to the loud part and normalizes. None if it holds no speech, which saves a paid request."""
    frame, skip = rate // 50, rate // 20        # 20 ms frames; the first 50 ms is the mic's filter settling
    if len(pcm) <= skip + 10 * frame:
        return None
    s = _high_pass(pcm[skip:], rate)
    levels = [sum(abs(v) for v in s[f * frame:(f + 1) * frame]) // frame for f in range(len(s) // frame)]
    noise = sorted(levels)[len(levels) // 5] or 1  # the 20th percentile is the room's noise
    threshold = max(noise * 3, 12)
    loud = [f for f, level in enumerate(levels) if level > threshold]
    if len(loud) < 6:
        return None
    pad = 250 // 20  # keep 250 ms around the loud part
    first, last = max(loud[0] - pad, 0), min(loud[-1] + pad + 1, len(levels))
    kept = s[first * frame:last * frame]
    peak = max(max(abs(v) for v in kept), 1)
    gain = min(23000 / peak, 30.0)  # about -3 dBFS, at most +30 dB
    if gain > 1:
        kept = [max(-32768, min(32767, int(v * gain))) for v in kept]
    return kept


def _high_pass(pcm, rate):
    """150 Hz, 4th-order Butterworth, as two biquads in transposed direct form II."""
    w0 = 2 * math.pi * 150 / rate
    cw, sw = math.cos(w0), math.sin(w0)
    out = pcm
    for q in (0.5412, 1.3066):
        alpha = sw / (2 * q)
        a0 = 1 + alpha
        b0, b1, b2 = (1 + cw) / 2 / a0, -(1 + cw) / a0, (1 + cw) / 2 / a0
        a1, a2 = -2 * cw / a0, (1 - alpha) / a0
        z0 = z1 = 0.0
        filtered = []
        for v in out:
            y = b0 * v + z0
            z0 = b1 * v - a1 * y + z1
            z1 = b2 * v - a2 * y
            filtered.append(y)
        out = filtered
    return [max(-32768, min(32767, int(v))) for v in out]
