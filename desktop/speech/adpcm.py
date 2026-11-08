"""IMA ADPCM, 4 bits a sample, low nibble first. protocol/gen_vectors.py is the reference. Each `audio` packet carries its
own decoder state, so a packet decodes on its own."""

STEPS = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97,
         107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
         876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428,
         4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350,
         22385, 24623, 27086, 29794, 32767]
INDEX_STEP = [-1, -1, -1, -1, 2, 4, 6, 8]


def decode(data, samples, predictor=0, index=0):
    """16-bit samples from the codes in data: at most `samples` of them."""
    pcm = []
    for byte in data:
        for nibble in (byte & 15, byte >> 4):
            if len(pcm) == samples:
                return pcm
            step = STEPS[index]
            diff = step >> 3
            if nibble & 1:
                diff += step >> 2
            if nibble & 2:
                diff += step >> 1
            if nibble & 4:
                diff += step
            predictor = predictor - diff if nibble & 8 else predictor + diff
            predictor = max(-32768, min(32767, predictor))
            index = max(0, min(88, index + INDEX_STEP[nibble & 7]))
            pcm.append(predictor)
    return pcm
