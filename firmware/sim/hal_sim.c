// All of hal.h, on the Mac. Storage is a file, the link and the debug interface are TCP servers on 127.0.0.1, the
// battery is fake, sound is a label on the window, and the mic plays a WAV file or silence.
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <fenv.h>
#include <stdlib.h>
#include <string.h>
#include <sys/event.h>
#include <sys/file.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#include <CommonCrypto/CommonKeyDerivation.h>

#include "../ux/ux.h"
#include "sim.h"

sim_options_t SIM;
void (*sim_window_command)(const char *cmd);

static pthread_mutex_t s_mu = PTHREAD_MUTEX_INITIALIZER;
static int64_t s_t0;

// ---- Time and memory ----------------------------------------------------------------------------------------------

int64_t hal_now_us(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    const int64_t t = (int64_t)ts.tv_sec * 1000000 + ts.tv_nsec / 1000;
    if (!s_t0) s_t0 = t;
    return t - s_t0 + 1;
}

// A sleep that keeps its time, as a board's does. macOS lets usleep's timer fire late to save power: a few milliseconds
// late in a terminal, and up to 100 ms late for a process it treats as background, such as a CI runner's. There the
// simulator drew ten frames a second, and every test that times an animation, a drag or the link failed. A kqueue timer
// marked critical fires on time at any priority.
static void sleep_us(int64_t us)
{
    static _Thread_local int kq = -1;
    if (kq < 0 && (kq = kqueue()) >= 0) fcntl(kq, F_SETFD, FD_CLOEXEC); // hal_restart execs the simulator again
    struct kevent ev, out;
    EV_SET(&ev, 1, EVFILT_TIMER, EV_ADD | EV_ONESHOT, NOTE_USECONDS | NOTE_CRITICAL, us, NULL);
    if (kq < 0 || (kevent(kq, &ev, 1, &out, 1, NULL) < 0 && errno != EINTR)) usleep((useconds_t)us);
}

// "%rounding flip": the FPU rounds differently every other frame, as a board's did. Floats then differ in their last bits
// from one frame to the next, and the change check must not take that for a change (the "steady" case).
static bool s_flip_rounding;

void hal_sleep_ms(int ms)
{
    if (s_flip_rounding) {
        static bool odd;
        odd = !odd;
        fesetround(odd ? FE_TOWARDZERO : FE_TONEAREST);
    }
    sleep_us((int64_t)ms * 1000);
}
uint32_t hal_fpu_control(void) { return (uint32_t)fegetround(); }
void *hal_alloc_large(size_t bytes) { return malloc(bytes); }
uint32_t hal_random(void) { return arc4random(); }

// A made-up address, locally administered, one for each simulated board.
void hal_device_mac(uint8_t mac[6])
{
    const uint8_t m[6] = {0x02, 0x00, 0x5E, 0x10, BOARD.shape == SHAPE_ROUND ? 0x01 : 0x02, 0x01};
    memcpy(mac, m, 6);
}

// ---- The screen ---------------------------------------------------------------------------------------------------

static uint16_t *s_panel;
static uint64_t s_panel_gen;
static int s_bright = HAL_BRIGHT_LEVELS - 1;

// --panel-file: the panel, in a file another process can map. A simulated restart replaces this process, and with it any
// window it has. So a viewer (--view) keeps one window open through restarts, and from one board's run to the next.
#define SHARED_MAGIC 0x42454E54u
#define SHARED_PIXELS (960 * 960)
typedef struct {
    uint32_t magic;
    int32_t width, height, shape, mount_deg, bright;
    float tilt;
    uint32_t pad;
    volatile uint64_t gen;
} shared_t;
static shared_t *s_shared;

static uint16_t *shared_map(const char *path)
{
    const size_t size = sizeof(shared_t) + SHARED_PIXELS * 2;
    const int fd = open(path, O_RDWR | O_CREAT, 0644);
    if (fd < 0) return NULL;
    void *p = ftruncate(fd, (off_t)size) ? MAP_FAILED : mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (p == MAP_FAILED) return NULL;
    s_shared = p;
    return (uint16_t *)(s_shared + 1);
}

bool sim_view_open(const char *path)
{
    // One viewer per file. The lock goes when the process does, so nothing is left to clean up.
    char lock[1040];
    snprintf(lock, sizeof lock, "%s.view", path);
    const int fd = open(lock, O_RDWR | O_CREAT, 0644);
    if (fd < 0 || flock(fd, LOCK_EX | LOCK_NB)) return false;
    s_panel = shared_map(path);
    return s_panel != NULL;
}

void sim_view_sync(void)
{
    if (!s_shared || s_shared->magic != SHARED_MAGIC) return;
    if (s_shared->width <= 0 || s_shared->width * s_shared->height > SHARED_PIXELS) return;
    sim_view_board(s_shared->width, s_shared->height, s_shared->shape, s_shared->mount_deg);
}

void hal_screen_present(const uint16_t *fb, int y0, int rows)
{
    if (y0 < 0 || y0 % HAL_BAND_ROWS) {
        fprintf(stderr, "sim: bad band y0=%d rows=%d\n", y0, rows);
        abort();
    }
    if (y0 + rows > BOARD.height) rows = BOARD.height - y0;
    pthread_mutex_lock(&s_mu);
    memcpy(s_panel + y0 * BOARD.width, fb + y0 * BOARD.width, (size_t)rows * (size_t)BOARD.width * 2);
    s_panel_gen++;
    if (s_shared) s_shared->gen = s_panel_gen;
    pthread_mutex_unlock(&s_mu);
}

void hal_screen_brightness(int level)
{
    s_bright = level;
    if (s_shared) s_shared->bright = level;
}

int sim_brightness(void) { return SIM.view && s_shared ? s_shared->bright : s_bright; }

void sim_copy_panel(uint16_t *out)
{
    pthread_mutex_lock(&s_mu);
    if (s_panel) memcpy(out, s_panel, (size_t)BOARD.width * (size_t)BOARD.height * 2);
    pthread_mutex_unlock(&s_mu);
}

uint64_t sim_panel_generation(void) { return SIM.view && s_shared ? s_shared->gen : s_panel_gen; }

static void panel_write_ppm(const char *path)
{
    FILE *f = fopen(path, "wb");
    if (!f) return;
    const int W = BOARD.width, H = BOARD.height;
    fprintf(f, "P6\n%d %d\n255\n", W, H);
    pthread_mutex_lock(&s_mu);
    for (int i = 0; i < W * H; i++) {
        const uint16_t p = s_panel[BOARD.mount_deg == 180 ? W * H - 1 - i : i]; // what a person sees, as the board is held
        const uint8_t rgb[3] = {(uint8_t)((p >> 8 & 0xF8) | (p >> 13)), (uint8_t)((p >> 3 & 0xFC) | (p >> 9 & 3)), (uint8_t)((p << 3 & 0xF8) | (p >> 2 & 7))};
        fwrite(rgb, 1, 3, f);
    }
    pthread_mutex_unlock(&s_mu);
    fclose(f);
}

// ---- Input ----------------------------------------------------------------------------------------------------------

#define INPUT_CAP 256
static hw_event_t s_input[INPUT_CAP];
static int s_in_head, s_in_tail;

static void push_input(hw_event_t e)
{
    pthread_mutex_lock(&s_mu);
    const int next = (s_in_tail + 1) % INPUT_CAP;
    if (next != s_in_head) {
        s_input[s_in_tail] = e;
        s_in_tail = next;
    }
    pthread_mutex_unlock(&s_mu);
}

bool hal_input_poll(hw_event_t *e)
{
    pthread_mutex_lock(&s_mu);
    const bool have = s_in_head != s_in_tail;
    if (have) {
        *e = s_input[s_in_head];
        s_in_head = (s_in_head + 1) % INPUT_CAP;
    }
    pthread_mutex_unlock(&s_mu);
    return have;
}

void sim_touch(hw_kind_t kind, int x, int y) { push_input((hw_event_t){.kind = kind, .x = x, .y = y}); }
void sim_knob(int step) { push_input((hw_event_t){.kind = HW_KNOB, .step = step}); }
void sim_button(hw_button_t b) { push_input((hw_event_t){.kind = HW_BUTTON, .button = b}); }

// ---- Feedback ---------------------------------------------------------------------------------------------------------

static char s_fb_text[48];
static int64_t s_fb_until;
static _Atomic int s_volume = 6; // the window's thread and the UX's both use these

void hal_feedback(feedback_t kind, bool sound_allowed)
{
    static const char *const BUZZ[FB_COUNT] = {"Vibrates: tick", "Vibrates: bump", "Vibrates: tap", "Vibrates: double", "Vibrates: alert", "Vibrates for 30 s", "Stops vibrating", "Vibrates: yay", "Vibrates: poof", "Vibrates: drop", "Vibrates: land"};
    static const char *const PLAY[FB_COUNT] = {"Plays: click", "Plays: low click", "Plays: tap", "Plays: double tap", "Plays: chime", "Plays: alarm", "Stops the alarm", "Plays: yay", "Plays: poof", "Plays: drop", "Plays: land"};
    char text[48] = "";
    if (BOARD.has.haptics) {
        snprintf(text, sizeof text, "%s", BUZZ[kind]);
    } else if (BOARD.has.speaker && sound_allowed && s_volume > 0) {
        snprintf(text, sizeof text, "%s (volume %d)", PLAY[kind], s_volume);
    }
    if (!text[0]) return;
    pthread_mutex_lock(&s_mu);
    memcpy(s_fb_text, text, sizeof s_fb_text);
    s_fb_until = hal_now_us() + 900000;
    pthread_mutex_unlock(&s_mu);
}

void hal_volume(int level) { s_volume = level; }

void sim_feedback_text(char *out, int n)
{
    pthread_mutex_lock(&s_mu);
    snprintf(out, (size_t)n, "%s", hal_now_us() < s_fb_until ? s_fb_text : "");
    pthread_mutex_unlock(&s_mu);
}

// ---- Motion and power --------------------------------------------------------------------------------------------------

static _Atomic float s_tilt;
static _Atomic bool s_flat;

void sim_set_tilt(float deg) { s_tilt = deg; }
float sim_tilt(void) { return SIM.view && s_shared ? s_shared->tilt : s_tilt; }
void sim_set_flat(bool flat) { s_flat = flat; }
bool sim_flat(void) { return s_flat; }

bool hal_motion_read(float *x, float *y, float *z)
{
    if (s_shared) s_shared->tilt = s_tilt;
    if (!BOARD.has.motion) return false;
    if (s_flat) {
        *x = 0;
        *y = 0;
        *z = -1;
        return true;
    }
    const float a = s_tilt * 3.14159265f / 180.0f;
    *x = sinf(a);
    *y = cosf(a);
    *z = 0;
    return true;
}

void hal_power_read(hal_power_t *out)
{
    out->ok = BOARD.has.battery;
    out->percent = BOARD.has.battery ? 82 : -1;
    out->usb = false;
    out->charging = false;
}

// ---- The mic: a WAV file, in real time, then silence -------------------------------------------------------------------

static int16_t *s_wav;
static size_t s_wav_len;
static bool s_mic_on;
static int64_t s_mic_start;
static size_t s_mic_pos;

static void load_wav(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) {
        fprintf(stderr, "sim: can't read %s\n", path);
        return;
    }
    fseek(f, 0, SEEK_END);
    const long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *d = malloc((size_t)size);
    if (d && fread(d, 1, (size_t)size, f) == (size_t)size && size > 44 && !memcmp(d, "RIFF", 4)) {
        int channels = 1;
        for (long off = 12; off + 8 <= size;) {
            uint32_t len;
            memcpy(&len, d + off + 4, 4);
            if (!memcmp(d + off, "fmt ", 4) && off + 24 <= size) {
                uint16_t c;
                memcpy(&c, d + off + 10, 2);
                channels = c ? c : 1;
            }
            if (!memcmp(d + off, "data", 4)) {
                size_t avail = (size_t)(size - off - 8);
                if (len < avail) avail = len;
                s_wav_len = avail / 2 / (size_t)channels;
                s_wav = malloc(s_wav_len * 2);
                for (size_t i = 0; i < s_wav_len; i++) memcpy(&s_wav[i], d + off + 8 + i * 2 * (size_t)channels, 2);
                break;
            }
            off += 8 + (long)len + (len & 1);
        }
    }
    free(d);
    fclose(f);
}

bool hal_mic_start(void)
{
    if (!BOARD.has.mic) return false;
    s_mic_on = true;
    s_mic_start = hal_now_us();
    s_mic_pos = 0;
    return true;
}

int hal_mic_read(int16_t *out, int max)
{
    if (!s_mic_on) return 0;
    const size_t due = (size_t)((hal_now_us() - s_mic_start) * 16000 / 1000000);
    if (due <= s_mic_pos) return 0;
    size_t n = due - s_mic_pos;
    if (n > (size_t)max) n = (size_t)max;
    for (size_t i = 0; i < n; i++) {
        const size_t p = s_mic_pos + i;
        out[i] = p < s_wav_len ? s_wav[p] : 0;
    }
    s_mic_pos += n;
    return (int)n;
}

void hal_mic_stop(void) { s_mic_on = false; }

// ---- Storage: a file, written whole on each change ------------------------------------------------------------------------

#define STORE_MAX 300
typedef struct {
    char key[16];
    size_t len;
    uint8_t *data;
} store_item_t;
static store_item_t s_store[STORE_MAX];
static int s_nstore;

static void store_flush(void)
{
    if (!SIM.store_path) return;
    char tmp[1100];
    snprintf(tmp, sizeof tmp, "%s.tmp", SIM.store_path);
    FILE *f = fopen(tmp, "wb");
    if (!f) return;
    for (int i = 0; i < s_nstore; i++) {
        const uint32_t len = (uint32_t)s_store[i].len;
        fwrite(s_store[i].key, 1, 16, f);
        fwrite(&len, 1, 4, f);
        fwrite(s_store[i].data, 1, len, f);
    }
    fclose(f);
    rename(tmp, SIM.store_path);
}

static void store_load(void)
{
    if (!SIM.store_path) return;
    FILE *f = fopen(SIM.store_path, "rb");
    if (!f) return;
    char key[16];
    uint32_t len;
    while (s_nstore < STORE_MAX && fread(key, 1, 16, f) == 16 && fread(&len, 1, 4, f) == 4) {
        if (len > 1024 * 1024) break;
        store_item_t *it = &s_store[s_nstore];
        it->data = malloc(len ? len : 1);
        it->len = len;
        memcpy(it->key, key, 16);
        it->key[15] = 0;
        if (fread(it->data, 1, len, f) != len) break;
        s_nstore++;
    }
    fclose(f);
}

static store_item_t *store_find(const char *key)
{
    for (int i = 0; i < s_nstore; i++) {
        if (!strcmp(s_store[i].key, key)) return &s_store[i];
    }
    return NULL;
}

static void check_key(const char *key)
{
    if (strlen(key) > 15) {
        fprintf(stderr, "sim: storage key too long: %s\n", key);
        abort();
    }
}

bool hal_store_get(const char *key, void *out, size_t *len)
{
    check_key(key);
    const store_item_t *it = store_find(key);
    if (!it || it->len > *len) return false;
    memcpy(out, it->data, it->len);
    *len = it->len;
    return true;
}

bool hal_store_set(const char *key, const void *data, size_t len)
{
    check_key(key);
    store_item_t *it = store_find(key);
    if (!it) {
        if (s_nstore >= STORE_MAX) return false;
        it = &s_store[s_nstore++];
        memset(it, 0, sizeof *it);
        strcpy(it->key, key);
    }
    free(it->data);
    it->data = malloc(len ? len : 1);
    memcpy(it->data, data, len);
    it->len = len;
    store_flush();
    return true;
}

void hal_store_erase(const char *key)
{
    check_key(key);
    store_item_t *it = store_find(key);
    if (!it) return;
    free(it->data);
    *it = s_store[--s_nstore];
    store_flush();
}

void hal_pbkdf2_sha256(const char *password, const uint8_t *salt, size_t salt_len, uint32_t iterations, uint8_t out[32])
{
    CCKeyDerivationPBKDF(kCCPBKDF2, password, strlen(password), salt, salt_len, kCCPRFHmacAlgSHA256, iterations, out, 32);
}

// ---- A TCP server that takes one client at a time ----------------------------------------------------------------------

typedef struct {
    int listen_fd, client_fd;
    int port;
} server_t;

static void server_start(server_t *s, int port)
{
    s->client_fd = -1;
    s->port = port;
    s->listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    const int yes = 1;
    setsockopt(s->listen_fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof yes);
    struct sockaddr_in a = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port), .sin_addr.s_addr = htonl(INADDR_LOOPBACK)};
    if (bind(s->listen_fd, (struct sockaddr *)&a, sizeof a) || listen(s->listen_fd, 1)) {
        // Fatal: carrying on would leave a test talking to whatever else holds the port, such as a leftover simulator.
        fprintf(stderr, "sim: can't listen on 127.0.0.1:%d: %s. Is another simulator running? (--link-port, --debug-port)\n", port, strerror(errno));
        exit(2);
    }
    fcntl(s->listen_fd, F_SETFL, O_NONBLOCK);
    fcntl(s->listen_fd, F_SETFD, FD_CLOEXEC);
}

static void server_accept(server_t *s)
{
    if (s->listen_fd < 0 || s->client_fd >= 0) return;
    const int fd = accept(s->listen_fd, NULL, NULL);
    if (fd < 0) return;
    fcntl(fd, F_SETFL, O_NONBLOCK);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
    const int yes = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &yes, sizeof yes);
    setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &yes, sizeof yes);
    s->client_fd = fd;
}

static void server_drop(server_t *s)
{
    if (s->client_fd >= 0) close(s->client_fd);
    s->client_fd = -1;
}

static int server_read(server_t *s, void *out, int max)
{
    server_accept(s);
    if (s->client_fd < 0) return 0;
    const ssize_t n = recv(s->client_fd, out, (size_t)max, 0);
    if (n > 0) return (int)n;
    if (n == 0 || (errno != EAGAIN && errno != EWOULDBLOCK)) server_drop(s);
    return 0;
}

static bool server_write(server_t *s, const void *data, size_t len)
{
    server_accept(s);
    if (s->client_fd < 0) return false;
    const char *p = data;
    int waits = 0;
    while (len) {
        const ssize_t n = send(s->client_fd, p, len, 0);
        if (n > 0) {
            p += n;
            len -= (size_t)n;
            waits = 0;
            continue;
        }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && waits++ < 5000) {
            sleep_us(1000); // a slow reader: wait for it, up to 5 s
            continue;
        }
        server_drop(s);
        return false;
    }
    return true;
}

// ---- The link -----------------------------------------------------------------------------------------------------------

static server_t s_link, s_debug;
static int64_t s_kick_at;
static int s_pair_until_s; // in pair mode until then
static bool s_pairing_now;
static uint32_t s_pair_code;
static hal_mac_t s_macs[HAL_MACS_MAX];
static int s_nmacs;
static char s_chosen[16];
static char s_peer[16]; // the connected Mac's key

// The paired Macs last, as a board's do.
typedef struct {
    int32_t n;
    hal_mac_t macs[HAL_MACS_MAX];
    char chosen[16];
} saved_macs_t;

static void macs_save(void)
{
    saved_macs_t v;
    memset(&v, 0, sizeof v);
    v.n = s_nmacs;
    memcpy(v.macs, s_macs, sizeof v.macs);
    memcpy(v.chosen, s_chosen, sizeof v.chosen);
    hal_store_set("sim_macs", &v, sizeof v);
}

static void macs_load(void)
{
    saved_macs_t v;
    size_t n = sizeof v;
    if (!hal_store_get("sim_macs", &v, &n) || n != sizeof v || v.n < 0 || v.n > HAL_MACS_MAX) return;
    s_nmacs = v.n;
    memcpy(s_macs, v.macs, sizeof v.macs);
    memcpy(s_chosen, v.chosen, sizeof v.chosen);
    s_chosen[sizeof s_chosen - 1] = 0;
}

static void poll_link_side(void)
{
    if (SIM.demo) {
        demo_poll();
        return;
    }
    server_accept(&s_link);
    if (s_kick_at && hal_now_us() > s_kick_at) {
        s_kick_at = 0;
        server_drop(&s_link);
    }
}

void hal_link_status(hal_link_status_t *out)
{
    poll_link_side();
    out->connected = SIM.demo ? demo_connected() : s_link.client_fd >= 0;
    out->pairing = s_pairing_now;
    out->code = s_pair_code;
}

int hal_link_read(uint8_t *out, int max)
{
    poll_link_side();
    if (SIM.demo) return demo_read(out, max);
    return server_read(&s_link, out, max);
}

bool hal_link_send(const char *data, size_t len)
{
    if (SIM.demo) {
        if (!demo_connected()) return false;
        char *copy = malloc(len + 1);
        memcpy(copy, data, len);
        copy[len] = 0;
        demo_on_line(copy);
        free(copy);
        return true;
    }
    return server_write(&s_link, data, len);
}

const char *hal_link_refuse_reason(void)
{
    if (s_pairing_now) return NULL; // the Mac that just paired stays
    if (hal_macs_pair_left() > 0) return "pairing";
    if (s_chosen[0] && strcmp(s_chosen, s_peer)) return "another Mac is chosen";
    return NULL;
}

void hal_link_kick(const char *reason)
{
    char line[96];
    const int n = snprintf(line, sizeof line, "{\"t\":\"busy\",\"reason\":\"%s\"}\n", reason);
    server_write(&s_link, line, (size_t)n);
    s_kick_at = hal_now_us() + 200000;
}

void hal_link_remember_host(const char *name)
{
    s_peer[0] = 0;
    if (!name[0]) return;
    for (int i = 0; i < s_nmacs; i++) {
        if (!strcmp(s_macs[i].name, name)) {
            snprintf(s_peer, sizeof s_peer, "%s", s_macs[i].key);
            return;
        }
    }
    if (s_nmacs >= HAL_MACS_MAX) return;
    hal_mac_t *m = &s_macs[s_nmacs];
    memset(m, 0, sizeof *m);
    snprintf(m->key, sizeof m->key, "sim%d", s_nmacs + 1);
    snprintf(m->name, sizeof m->name, "%s", name);
    snprintf(s_peer, sizeof s_peer, "%s", m->key);
    s_nmacs++;
    macs_save();
}

int hal_macs_list(hal_mac_t *out, int max)
{
    const int n = s_nmacs < max ? s_nmacs : max;
    const bool up = SIM.demo ? demo_connected() : s_link.client_fd >= 0;
    for (int i = 0; i < n; i++) {
        out[i] = s_macs[i];
        out[i].connected = up && !strcmp(s_macs[i].key, s_peer);
    }
    return n;
}

void hal_macs_chosen(char *out, size_t n) { snprintf(out, n, "%s", s_chosen); }

void hal_macs_choose(const char *key)
{
    snprintf(s_chosen, sizeof s_chosen, "%s", key);
    macs_save();
    if (!SIM.demo && key[0] && s_link.client_fd >= 0 && strcmp(key, s_peer)) hal_link_kick("another Mac is chosen");
}

void hal_macs_forget(const char *key)
{
    for (int i = 0; i < s_nmacs; i++) {
        if (!strcmp(s_macs[i].key, key)) {
            memmove(&s_macs[i], &s_macs[i + 1], (size_t)(s_nmacs - i - 1) * sizeof(hal_mac_t));
            s_nmacs--;
            break;
        }
    }
    if (!strcmp(s_chosen, key)) s_chosen[0] = 0;
    if (!strcmp(s_peer, key)) {
        s_peer[0] = 0;
        if (!SIM.demo) server_drop(&s_link);
    }
    macs_save();
}

void hal_macs_pair_start(void)
{
    s_pair_until_s = (int)(hal_now_us() / 1000000) + HAL_PAIR_SECONDS;
    if (!SIM.demo && s_link.client_fd >= 0) hal_link_kick("pairing"); // make room: one connection at a time
}

void hal_macs_pair_cancel(void)
{
    s_pair_until_s = 0;
    s_pairing_now = false;
}

int hal_macs_pair_left(void)
{
    const int left = s_pair_until_s - (int)(hal_now_us() / 1000000);
    if (left <= 0) {
        s_pairing_now = false;
        return 0;
    }
    return left;
}

void sim_toggle_mac(void)
{
    if (SIM.demo) demo_toggle_mac();
}

void sim_add_agents(int delta)
{
    if (SIM.demo) demo_set_agents(demo_agent_count() + delta);
}

int sim_agent_count(void) { return SIM.demo ? demo_agent_count() : 0; }

void sim_toggle_needs(void)
{
    if (SIM.demo) demo_toggle_needs();
}

// ---- The debug interface. Lines that start with % are for the simulator itself, and never reach the UX ---------------------

static bool s_at_line_start = true;
static char s_ctl[512];
static int s_ctl_len = -1;

void hal_debug_write(const char *data, size_t len) { server_write(&s_debug, data, len); }

static void sim_command(char *cmd)
{
    char reply[160];
    if (!strncmp(cmd, "panel ", 6)) { // the panel as it is, as a PPM file: what a person would see, code and all
        panel_write_ppm(cmd + 6);
        snprintf(reply, sizeof reply, "SIM ok panel\n");
    } else if (!strcmp(cmd, "pair on")) { // a new Mac starts pairing, with the preview's sample code
        s_pairing_now = true;
        s_pair_code = 482913;
        snprintf(reply, sizeof reply, "SIM ok pair\n");
    } else if (!strcmp(cmd, "pair off")) {
        s_pairing_now = false;
        snprintf(reply, sizeof reply, "SIM ok pair\n");
    } else if (!strncmp(cmd, "tilt ", 5)) {
        s_tilt = (float)atof(cmd + 5);
        snprintf(reply, sizeof reply, "SIM ok tilt\n");
    } else if (!strncmp(cmd, "flat ", 5)) {
        s_flat = atoi(cmd + 5) != 0;
        snprintf(reply, sizeof reply, "SIM ok flat\n");
    } else if (!strncmp(cmd, "agents ", 7)) { // with --demo
        if (SIM.demo) demo_set_agents(atoi(cmd + 7));
        snprintf(reply, sizeof reply, "SIM ok agents %d\n", sim_agent_count());
    } else if (!strncmp(cmd, "touch ", 6)) { // "%touch down X Y": as the mouse does
        char what[8];
        int x, y;
        if (sscanf(cmd + 6, "%7s %d %d", what, &x, &y) == 3) {
            sim_touch(!strcmp(what, "down") ? HW_TOUCH_DOWN : !strcmp(what, "up") ? HW_TOUCH_UP : HW_TOUCH_MOVE, x, y);
        }
        snprintf(reply, sizeof reply, "SIM ok touch\n");
    } else if (!strncmp(cmd, "window ", 7) || !strncmp(cmd, "click ", 6) || !strncmp(cmd, "key ", 4) || !strncmp(cmd, "scroll ", 7)) {
        if (sim_window_command) {
            sim_window_command(cmd);
            snprintf(reply, sizeof reply, "SIM ok window\n");
        } else {
            snprintf(reply, sizeof reply, "SIM error no window\n");
        }
    } else if (!strcmp(cmd, "rounding flip") || !strcmp(cmd, "rounding steady")) {
        s_flip_rounding = !strcmp(cmd, "rounding flip");
        if (!s_flip_rounding) fesetround(FE_TONEAREST);
        snprintf(reply, sizeof reply, "SIM ok rounding\n");
    } else if (!strncmp(cmd, "knob ", 5)) {
        sim_knob(atoi(cmd + 5));
        snprintf(reply, sizeof reply, "SIM ok knob\n");
    } else if (!strncmp(cmd, "mac ", 4)) { // adds a paired Mac, for the list
        hal_mac_t *m = s_nmacs < HAL_MACS_MAX ? &s_macs[s_nmacs++] : NULL;
        if (m) {
            memset(m, 0, sizeof *m);
            snprintf(m->key, sizeof m->key, "sim%d", s_nmacs);
            snprintf(m->name, sizeof m->name, "%s", cmd + 4);
            macs_save();
        }
        snprintf(reply, sizeof reply, "SIM ok mac\n");
    } else {
        snprintf(reply, sizeof reply, "SIM error\n");
    }
    server_write(&s_debug, reply, strlen(reply));
}

int hal_debug_read(char *out, int max)
{
    char buf[1024];
    const int got = server_read(&s_debug, buf, (int)sizeof buf < max ? (int)sizeof buf : max);
    int n = 0;
    for (int i = 0; i < got; i++) {
        const char c = buf[i];
        if (s_ctl_len >= 0) {
            if (c == '\n') {
                s_ctl[s_ctl_len] = 0;
                s_ctl_len = -1;
                s_at_line_start = true;
                sim_command(s_ctl);
            } else if (s_ctl_len < (int)sizeof s_ctl - 1) {
                s_ctl[s_ctl_len++] = c;
            }
            continue;
        }
        if (s_at_line_start && c == '%') {
            s_ctl_len = 0;
            continue;
        }
        s_at_line_start = c == '\n';
        out[n++] = c;
    }
    return n;
}

void hal_restart(void)
{
    // A real restart: memory gone, storage kept, new sockets. The harness connects again.
    if (s_link.listen_fd >= 0) close(s_link.listen_fd);
    if (s_debug.listen_fd >= 0) close(s_debug.listen_fd);
    server_drop(&s_link);
    server_drop(&s_debug);
    execv(SIM.argv[0], SIM.argv);
    fprintf(stderr, "sim: can't start again: %s\n", strerror(errno));
    exit(1);
}

void hal_memory(size_t *free_internal, size_t *min_free_internal, size_t *free_large)
{
    *free_internal = 0;
    *min_free_internal = 0;
    *free_large = 0;
}

// ---- Start ------------------------------------------------------------------------------------------------------------------

void hal_init(void)
{
    signal(SIGPIPE, SIG_IGN);
    s_panel = SIM.panel_file ? shared_map(SIM.panel_file) : NULL;
    if (s_panel) {
        memset(s_panel, 0, (size_t)BOARD.width * (size_t)BOARD.height * 2);
        s_shared->width = BOARD.width;
        s_shared->height = BOARD.height;
        s_shared->shape = (int32_t)BOARD.shape;
        s_shared->mount_deg = BOARD.mount_deg;
        s_shared->bright = s_bright;
        s_shared->tilt = s_tilt;
        s_shared->gen++;
        s_shared->magic = SHARED_MAGIC;
    } else {
        s_panel = calloc((size_t)BOARD.width * (size_t)BOARD.height, 2);
    }
    store_load();
    if (SIM.wav) load_wav(SIM.wav);
    macs_load();
    if (SIM.demo) {
        hal_link_remember_host("Mac mini");
        hal_link_remember_host("MacBook Pro"); // the demo's own, and connected
        demo_start();
    }
    if (!SIM.demo) server_start(&s_link, SIM.link_port);
    server_start(&s_debug, SIM.debug_port);
}

void sim_run_ux(void)
{
    hal_init();
    ux_init();
    fprintf(stderr, "bento-sim: %s, %d x %d. Debug interface on 127.0.0.1:%d%s\n", BOARD.name, BOARD.width, BOARD.height, SIM.debug_port, SIM.demo ? ", with the demo's agents." : "");
    if (!SIM.demo) fprintf(stderr, "bento-sim: the link listens on 127.0.0.1:%d\n", SIM.link_port);
    for (;;) ux_frame();
}
