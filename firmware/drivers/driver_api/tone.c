#include <math.h>
#include <string.h>

#include "driver_api.h"

size_t tone_render(const tone_t *tones, int count, int16_t *out, size_t max_samples)
{
    float end = 0;
    for (int i = 0; i < count; i++) end = fmaxf(end, tones[i].start + tones[i].dur);
    size_t samples = (size_t)(end * TONE_RATE);
    if (samples > max_samples) samples = max_samples;
    memset(out, 0, samples * sizeof(int16_t));
    for (int i = 0; i < count; i++) {
        const tone_t *t = &tones[i];
        float phase = 0;
        const size_t s0 = (size_t)(t->start * TONE_RATE), s1 = (size_t)((t->start + t->dur) * TONE_RATE);
        for (size_t s = s0; s < s1 && s < samples; s++) {
            const float k = (float)(s - s0) / (float)(s1 - s0);
            const float f = t->f0 * powf(t->f1 / t->f0, k); // an exponential sweep, as in Web Audio
            phase += f / TONE_RATE;
            phase -= floorf(phase);
            const float w = t->wave == WAVE_SINE       ? sinf(phase * 6.2832f)
                            : t->wave == WAVE_TRIANGLE ? 4 * fabsf(phase - 0.5f) - 1
                                                       : (phase < 0.5f ? 1 : -1);
            const float env = fminf(1, (float)(s - s0) / (0.012f * TONE_RATE)) * expf(-5 * k); // quick attack, smooth decay
            const int v = out[s] + (int)(w * env * t->vol * 32767);
            out[s] = (int16_t)(v > 32767 ? 32767 : v < -32768 ? -32768 : v);
        }
    }
    return samples;
}
