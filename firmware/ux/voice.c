// Voice: record through hal_mic_*, track speech as it comes, clean up, encode and send as protocol/PROTOCOL.md says. The
// board records first and sends afterwards. The work is spread across frames, so the screen never stops. Audio stays
// in this buffer and goes only to the paired Mac. A transcript is user data: it becomes the task, and is never logged.
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app.h"

#define RATE 16000
#define MAX_SECONDS 15
#define MAX_SAMPLES (RATE * MAX_SECONDS)
#define CHUNK 256           // samples per tracking step: 16 ms
#define END_PAUSE_MS 1300   // this much quiet after speech ends the recording
#define GIVE_UP_MS 6000     // nothing said for this long: stop anyway
#define WARMUP_CHUNKS 3     // the mic's filter settles over the first ~50 ms
#define MIN_LISTEN_MS 500
#define PACKET_SAMPLES 1600 // 0.1 s: 800 bytes of ADPCM
#define WAIT_MS 20000       // how long to wait for a transcript
#define FILTER_PER_FRAME 8000
#define PACKETS_PER_FRAME 2

#define FRAME_LEN (RATE / 50) // 20 ms
#define SKIP_LEN (RATE / 20)  // the first 50 ms: the filter settling

static struct {
    int16_t *pcm;
    size_t len;
    size_t tracked;
    int64_t start_us;
    // Live speech tracking
    float floor_lv, voice_lv;
    int loud_run, chunks;
    int64_t voiced_ms, heard_ms, last_voice_ms, t_ms;
    bool done_talking;
    float level;
    int64_t wave_at;
    // Clean-up
    int stage;
    size_t pos;
    float z[2][2];
    size_t kept_off, kept_len;
    uint32_t *lv, *sorted;
    // Sending
    int vid;
    int seq;
    size_t sent;
    adpcm_state_t st;
    int64_t wait_since;
} V;

static void fail(const char *hint)
{
    S.voice = VOICE_IDLE;
    ux_bump(hint);
    ux_log("voice", "failed");
}

void voice_init(void)
{
    V.vid = 0;
    size_t n = sizeof V.vid;
    if (!hal_store_get("vid", &V.vid, &n) || n != sizeof V.vid || V.vid < 0) V.vid = 0;
}

void voice_start(int64_t now)
{
    if (!S.connected) {
        ux_bump("no Mac");
        return;
    }
    if (!V.pcm) V.pcm = hal_alloc_large(MAX_SAMPLES * sizeof(int16_t));
    if (!V.lv) V.lv = hal_alloc_large(2 * (MAX_SAMPLES / FRAME_LEN + 2) * sizeof(uint32_t));
    V.sorted = V.lv ? V.lv + MAX_SAMPLES / FRAME_LEN + 2 : NULL;
    if (!V.pcm || !V.lv || !hal_mic_start()) {
        fail("transcription failed");
        return;
    }
    V.len = V.tracked = 0;
    V.start_us = now;
    V.floor_lv = V.voice_lv = 0;
    V.loud_run = V.chunks = 0;
    V.voiced_ms = V.last_voice_ms = V.t_ms = 0;
    V.done_talking = false;
    V.level = 0;
    V.wave_at = now;
    memset(S.wave, 0, sizeof S.wave);
    S.mic_level = 0;
    S.voice = VOICE_LISTENING;
    ux_feedback(FB_TAP);
    ux_log("voice", "listening");
}

// As the old firmware's: room noise as a floor, speech as sound above it that lasts, and quiet after it as the end.
static void track(const int16_t *x, size_t n)
{
    int32_t sum = 0;
    for (size_t i = 0; i < n; i++) sum += x[i];
    const int32_t dc = sum / (int32_t)n;
    uint32_t dev = 0;
    for (size_t i = 0; i < n; i++) dev += (uint32_t)abs(x[i] - dc);
    const float lv = (float)(dev / (uint32_t)n);
    V.t_ms += (int64_t)n * 1000 / RATE;
    if (++V.chunks <= WARMUP_CHUNKS) return;
    if (V.chunks == WARMUP_CHUNKS + 1 || lv < V.floor_lv) V.floor_lv = lv; // room noise: follows quiet quickly...
    else V.floor_lv += (lv - V.floor_lv) / 200.0f;                        // ...and rises slowly (~3 s)
    // Speech is loud for a while; a click or bump is one chunk. Once someone has spoken, sounds much quieter than
    // their voice don't count as more speech.
    float thresh = V.floor_lv * 2.5f + 4.0f;
    if (V.voiced_ms >= 150 && V.voice_lv * 0.25f > thresh) thresh = V.voice_lv * 0.25f;
    V.loud_run = lv > thresh ? V.loud_run + 1 : 0;
    if (V.loud_run >= 3) { // ~50 ms of sustained sound
        V.last_voice_ms = V.t_ms;
        V.voiced_ms += (int64_t)n * 1000 / RATE;
        V.voice_lv = V.voice_lv > 0 ? V.voice_lv + (lv - V.voice_lv) * 0.1f : lv;
    }
    const float level = (lv - V.floor_lv) / (V.floor_lv * 8.0f + 40.0f);
    V.level = level < 0 ? 0 : level > 1 ? 1 : level;
    const bool heard = V.voiced_ms >= 150;
    V.done_talking = (heard && V.t_ms - V.last_voice_ms > END_PAUSE_MS) || (!heard && V.t_ms > GIVE_UP_MS);
}

static void begin_cleanup(void)
{
    hal_mic_stop();
    V.heard_ms = V.voiced_ms;
    if (V.len <= SKIP_LEN + 10 * FRAME_LEN) {
        fail("didn\xE2\x80\x99t catch that");
        return;
    }
    S.voice = VOICE_CLEANING;
    V.stage = 0;
    V.pos = SKIP_LEN;
    memset(V.z, 0, sizeof V.z);
}

void voice_stop(int64_t now)
{
    if (S.voice != VOICE_LISTENING) return;
    if (now - V.start_us < MIN_LISTEN_MS * MS_US) {
        hal_mic_stop();
        fail("didn\xE2\x80\x99t catch that");
        return;
    }
    ux_feedback(FB_TICK);
    begin_cleanup();
}

void voice_cancel(void)
{
    if (S.voice == VOICE_LISTENING) hal_mic_stop();
    if (S.voice == VOICE_SENDING || S.voice == VOICE_WAITING) {
        char line[64];
        const int n = snprintf(line, sizeof line, "{\"t\":\"voice_cancel\",\"vid\":%d}\n", V.vid);
        if (n > 0) link_send_line(line, (size_t)n);
    }
    S.voice = VOICE_IDLE;
}

// High-pass at 150 Hz: a 4th-order Butterworth, as two biquads. Removes DC, hum and rumble, and keeps speech.
static void highpass_some(void)
{
    static const float Q[2] = {0.5412f, 1.3066f};
    static float b[2][3], a[2][2];
    static bool ready;
    if (!ready) {
        const float w0 = 2.0f * PI_F * 150.0f / (float)RATE, cw = cosf(w0), sw = sinf(w0);
        for (int k = 0; k < 2; k++) {
            const float alpha = sw / (2 * Q[k]), a0 = 1 + alpha;
            b[k][0] = (1 + cw) / 2 / a0;
            b[k][1] = -(1 + cw) / a0;
            b[k][2] = (1 + cw) / 2 / a0;
            a[k][0] = -2 * cw / a0;
            a[k][1] = (1 - alpha) / a0;
        }
        ready = true;
    }
    size_t end = V.pos + FILTER_PER_FRAME;
    if (end > V.len) end = V.len;
    int16_t *s = V.pcm;
    for (size_t i = V.pos; i < end; i++) {
        float v = s[i];
        for (int k = 0; k < 2; k++) {
            const float y = b[k][0] * v + V.z[k][0];
            V.z[k][0] = b[k][1] * v - a[k][0] * y + V.z[k][1];
            V.z[k][1] = b[k][2] * v - a[k][1] * y;
            v = y;
        }
        s[i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
    }
    V.pos = end;
}

static int cmp_u32(const void *a, const void *b)
{
    const uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
    return x < y ? -1 : x > y;
}

// Trim to the loud part, and normalize. false if nothing sounded like speech.
static bool analyze(void)
{
    int16_t *s = V.pcm + SKIP_LEN;
    const size_t len = V.len - SKIP_LEN, frames = len / FRAME_LEN;
    if (!frames) return false;
    for (size_t f = 0; f < frames; f++) {
        uint32_t sum = 0;
        for (int i = 0; i < FRAME_LEN; i++) sum += (uint32_t)abs(s[f * FRAME_LEN + (size_t)i]);
        V.lv[f] = sum / FRAME_LEN;
    }
    memcpy(V.sorted, V.lv, frames * sizeof(uint32_t));
    qsort(V.sorted, frames, sizeof(uint32_t), cmp_u32);
    const uint32_t floor_lv = V.sorted[frames / 5] ? V.sorted[frames / 5] : 1; // the 20th percentile: room noise
    uint32_t thresh = floor_lv * 3;
    if (thresh < 12) thresh = 12;
    size_t loud = 0, first = frames, last = 0;
    for (size_t f = 0; f < frames; f++) {
        if (V.lv[f] > thresh) {
            loud++;
            if (f < first) first = f;
            last = f;
        }
    }
    if (loud < 6 && V.heard_ms < 300) return false; // neither check heard speech
    if (loud < 6) { // the live tracker heard speech: keep it all
        first = 0;
        last = frames;
    }
    // Keep 250 ms around the loud part.
    const size_t pad = 250 / 20;
    first = first > pad ? first - pad : 0;
    last = last + pad + 1 < frames ? last + pad + 1 : frames;
    V.kept_off = SKIP_LEN + first * FRAME_LEN;
    V.kept_len = (last - first) * FRAME_LEN;
    s = V.pcm + V.kept_off;
    // Normalize the peak to about -3 dBFS, at most 30x (+30 dB).
    int peak = 1;
    for (size_t i = 0; i < V.kept_len; i++) {
        const int a = abs(s[i]);
        if (a > peak) peak = a;
    }
    int gain256 = 23000 * 256 / peak;
    if (gain256 > 30 * 256) gain256 = 30 * 256;
    if (gain256 > 256) {
        for (size_t i = 0; i < V.kept_len; i++) {
            const int32_t v = (int32_t)s[i] * gain256 >> 8;
            s[i] = (int16_t)(v > 32767 ? 32767 : (v < -32768 ? -32768 : v));
        }
    }
    return true;
}

static void send_some(int64_t now)
{
    static char line[1400];
    static uint8_t adpcm[PACKET_SAMPLES / 2 + 1];
    for (int k = 0; k < PACKETS_PER_FRAME && S.voice == VOICE_SENDING; k++) {
        if (V.sent >= V.kept_len) {
            const int n = snprintf(line, sizeof line, "{\"t\":\"voice_end\",\"vid\":%d,\"packets\":%d}\n", V.vid, V.seq);
            if (!link_send_line(line, (size_t)n)) {
                fail("no Mac");
                return;
            }
            S.voice = VOICE_WAITING;
            V.wait_since = now;
            ux_log("voice", "sent");
            return;
        }
        size_t count = V.kept_len - V.sent;
        if (count > PACKET_SAMPLES) count = PACKET_SAMPLES;
        const adpcm_state_t before = V.st;
        const size_t bytes = adpcm_encode(V.pcm + V.kept_off + V.sent, count, &V.st, adpcm);
        int n = snprintf(line, sizeof line, "{\"t\":\"audio\",\"vid\":%d,\"seq\":%d,\"p\":%d,\"i\":%d,\"n\":%u,\"b64\":\"", V.vid, V.seq, before.predictor, before.index, (unsigned)count);
        n += (int)b64_encode(adpcm, bytes, line + n);
        line[n++] = '"';
        line[n++] = '}';
        line[n++] = '\n';
        if (!link_send_line(line, (size_t)n)) {
            fail("no Mac");
            return;
        }
        V.sent += count;
        V.seq++;
    }
}

void voice_update(int64_t now)
{
    switch (S.voice) {
    case VOICE_LISTENING: {
        // Read what's there, and track it in chunks.
        for (int spin = 0; spin < 16; spin++) {
            const size_t room = MAX_SAMPLES - V.len;
            if (!room) break;
            const int got = hal_mic_read(V.pcm + V.len, room > 2048 ? 2048 : (int)room);
            if (got <= 0) break;
            V.len += (size_t)got;
        }
        while (V.len - V.tracked >= CHUNK) {
            track(V.pcm + V.tracked, CHUNK);
            V.tracked += CHUNK;
        }
        S.mic_level += (V.level - S.mic_level) * (1 - expf(-F.dt_ms / 80));
        if (now - V.wave_at >= 60 * MS_US) { // the last ~1.3 s of level, one bar per 60 ms
            V.wave_at = now;
            memmove(S.wave, S.wave + 1, sizeof S.wave - 1);
            S.wave[20] = (uint8_t)(sqrtf(V.level) * 255.0f);
        }
        if (V.done_talking || V.len >= MAX_SAMPLES) begin_cleanup();
        break;
    }
    case VOICE_CLEANING:
        if (V.stage == 0) {
            highpass_some();
            if (V.pos >= V.len) V.stage = 1;
            break;
        }
        if (!analyze()) {
            fail("didn\xE2\x80\x99t catch that");
            break;
        }
        V.vid = (V.vid + 1) & 0x3FFFFFFF;
        hal_store_set("vid", &V.vid, sizeof V.vid);
        {
            char line[64];
            const int n = snprintf(line, sizeof line, "{\"t\":\"voice_start\",\"vid\":%d,\"rate\":%d}\n", V.vid, RATE);
            if (!link_send_line(line, (size_t)n)) {
                fail("no Mac");
                break;
            }
        }
        V.seq = 0;
        V.sent = 0;
        V.st = (adpcm_state_t){0, 0};
        S.voice = VOICE_SENDING;
        ux_log("voice", "sending");
        break;
    case VOICE_SENDING:
        send_some(now);
        break;
    case VOICE_WAITING:
        if (now - V.wait_since > WAIT_MS * MS_US) fail("transcription failed");
        else if (!S.connected) fail("no Mac");
        break;
    default:
        break;
    }
}

void voice_on_transcript(const link_transcript_t *t, int64_t now)
{
    if (S.voice != VOICE_WAITING || t->vid != V.vid) return; // an old answer
    if (!t->text) {
        fail(t->no_speech ? "didn\xE2\x80\x99t catch that" : "transcription failed");
        return;
    }
    S.voice = VOICE_IDLE;
    ux_feedback(FB_TAP);
    ux_log("voice", "transcript");
    // Naming a project selects it ("Acme design review": Acme).
    if (t->project > 0) {
        const link_project_t *p = picker_find_project(t->project);
        if (p) S.project = *p;
    }
    clock_set_task(t->text, 0, false, now);
}
