#!/usr/bin/env python3
"""Writes samples/vectors.json: the values that the desktop app and the firmware must both reproduce.

The IMA ADPCM here is the reference. The firmware encodes voice with the same steps, and the desktop app decodes it.
"""
import base64
import hashlib
import json
import math
from pathlib import Path

STEPS = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97,
         107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
         876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428,
         4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350,
         22385, 24623, 27086, 29794, 32767]
INDEX_STEP = [-1, -1, -1, -1, 2, 4, 6, 8]


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def nibble_step(predictor, index, nibble):
    """Decode one 4-bit code. Returns the new (predictor, index)."""
    step = STEPS[index]
    diff = step >> 3
    if nibble & 1:
        diff += step >> 2
    if nibble & 2:
        diff += step >> 1
    if nibble & 4:
        diff += step
    predictor = predictor - diff if nibble & 8 else predictor + diff
    return clamp(predictor, -32768, 32767), clamp(index + INDEX_STEP[nibble & 7], 0, 88)


def encode(pcm, predictor=0, index=0):
    """Encode 16-bit samples: the first sample of each byte is in its low nibble."""
    out, pending = bytearray(), None
    for sample in pcm:
        step, delta, nibble = STEPS[index], sample - predictor, 0
        if delta < 0:
            nibble, delta = 8, -delta
        if delta >= step:
            nibble |= 4
            delta -= step
        if delta >= step >> 1:
            nibble |= 2
            delta -= step >> 1
        if delta >= step >> 2:
            nibble |= 1
        predictor, index = nibble_step(predictor, index, nibble)
        if pending is None:
            pending = nibble
        else:
            out.append(pending | nibble << 4)
            pending = None
    if pending is not None:
        out.append(pending)
    return bytes(out)


def decode(data, samples, predictor=0, index=0):
    pcm = []
    for byte in data:
        for nibble in (byte & 15, byte >> 4):
            if len(pcm) == samples:
                return pcm
            predictor, index = nibble_step(predictor, index, nibble)
            pcm.append(predictor)
    return pcm


def main():
    pcm = [round(9000 * math.sin(i * 0.11) + 3000 * math.sin(i * 0.47)) for i in range(64)]
    data = encode(pcm)
    salt = bytes(range(16))
    vectors = {
        "adpcm": {
            "pcm": pcm, "predictor": 0, "index": 0, "b64": base64.b64encode(data).decode(), "decoded": decode(data, len(pcm)),
        },
        "lock": {
            "code": "1234", "salt": salt.hex(), "iter": 2000,
            "hash": hashlib.pbkdf2_hmac("sha256", b"1234", salt, 2000, 32).hex(),
        },
    }
    path = Path(__file__).parent / "samples" / "vectors.json"
    path.write_text(json.dumps(vectors, indent=1) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
