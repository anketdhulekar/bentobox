// IMA ADPCM and base64 for voice. The steps are protocol/gen_vectors.py's, which the desktop app decodes.
#include "link.h"

static const int16_t STEPS[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97,
    107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
    876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428,
    4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350,
    22385, 24623, 27086, 29794, 32767,
};
static const int8_t INDEX_STEP[8] = {-1, -1, -1, -1, 2, 4, 6, 8};

size_t adpcm_encode(const int16_t *pcm, size_t samples, adpcm_state_t *st, uint8_t *out)
{
    int predictor = st->predictor, index = st->index;
    if (index < 0) index = 0;
    if (index > 88) index = 88;
    size_t bytes = 0;
    int pending = -1;
    for (size_t i = 0; i < samples; i++) {
        const int step = STEPS[index];
        int delta = pcm[i] - predictor, nibble = 0;
        if (delta < 0) {
            nibble = 8;
            delta = -delta;
        }
        if (delta >= step) {
            nibble |= 4;
            delta -= step;
        }
        if (delta >= step >> 1) {
            nibble |= 2;
            delta -= step >> 1;
        }
        if (delta >= step >> 2) nibble |= 1;
        // Decode it again, to follow what the other end will have.
        int diff = step >> 3;
        if (nibble & 1) diff += step >> 2;
        if (nibble & 2) diff += step >> 1;
        if (nibble & 4) diff += step;
        predictor = nibble & 8 ? predictor - diff : predictor + diff;
        if (predictor > 32767) predictor = 32767;
        if (predictor < -32768) predictor = -32768;
        index += INDEX_STEP[nibble & 7];
        if (index < 0) index = 0;
        if (index > 88) index = 88;
        if (pending < 0) {
            pending = nibble; // the first sample of each byte is in its low nibble
        } else {
            out[bytes++] = (uint8_t)(pending | nibble << 4);
            pending = -1;
        }
    }
    if (pending >= 0) out[bytes++] = (uint8_t)pending;
    st->predictor = predictor;
    st->index = index;
    return bytes;
}

size_t b64_encode(const uint8_t *d, size_t len, char *out)
{
    static const char T[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    size_t n = 0;
    size_t i = 0;
    for (; i + 2 < len; i += 3) {
        const uint32_t v = (uint32_t)d[i] << 16 | (uint32_t)d[i + 1] << 8 | d[i + 2];
        out[n++] = T[v >> 18];
        out[n++] = T[(v >> 12) & 63];
        out[n++] = T[(v >> 6) & 63];
        out[n++] = T[v & 63];
    }
    if (i < len) {
        const uint32_t v = (uint32_t)d[i] << 16 | (i + 1 < len ? (uint32_t)d[i + 1] << 8 : 0);
        out[n++] = T[v >> 18];
        out[n++] = T[(v >> 12) & 63];
        out[n++] = i + 1 < len ? T[(v >> 6) & 63] : '=';
        out[n++] = '=';
    }
    out[n] = 0;
    return n;
}
