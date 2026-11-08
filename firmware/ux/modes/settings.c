// Settings, in groups: Device, Connectivity, Agents, and Timer & Pomodoro. Each group is a page, and no page shows more
// than a screenful of rows: a round screen's wheel shows about five, and a square one scrolls past five. A board shows
// only the rows it has what they need for. Swiping down goes back up a level, onto the row that opened the page. The Mac
// list and pairing follow the zoo's: a Mac chosen, or forgotten; two minutes in which a new one may pair.
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "../app.h"

enum { PAGE_MAIN, PAGE_DEVICE, PAGE_CONNECT, PAGE_AGENTS, PAGE_CLOCKS, PAGE_MACS, PAGE_PAIR, PAGE_BRIGHT, PAGE_COUNT };
enum {
    R_DEVICE, R_CONNECT, R_AGENTS, R_CLOCKS,                                      // the groups
    R_BRIGHT, R_SOUND, R_THEME, R_ROTATE, R_BATTERY, R_FIRMWARE, R_MACADDR,       // Device
    R_MAC, R_PAIR,                                                                // Connectivity
    R_AUTO,                                                                       // Agents
    R_SPLIT, R_AUTO_SWITCH, R_SWITCH_AFTER,                                       // Timer & Pomodoro
    R_MAX
};
#define END -1
#define ROWS_MAX 10 // a page's rows, and its END: Device has the most, 7

static const int8_t PAGE_ROWS[PAGE_COUNT][ROWS_MAX] = {
    [PAGE_MAIN] = {R_DEVICE, R_CONNECT, R_AGENTS, R_CLOCKS, END},
    [PAGE_DEVICE] = {R_BRIGHT, R_SOUND, R_THEME, R_ROTATE, R_BATTERY, R_FIRMWARE, R_MACADDR, END},
    [PAGE_CONNECT] = {R_MAC, R_PAIR, END},
    [PAGE_AGENTS] = {R_AUTO, END},
    [PAGE_CLOCKS] = {R_SPLIT, R_AUTO_SWITCH, R_SWITCH_AFTER, END},
    [PAGE_MACS] = {END},
    [PAGE_PAIR] = {END},
    [PAGE_BRIGHT] = {END},
};
static const char *const PAGE_TITLE[PAGE_COUNT] = {"Settings", "Device", "Connectivity", "Agents", "Timer & Pomodoro",
                                                   "Choose the Mac", "", "Brightness"};
// Where swiping down goes from each page, and the row there that opened it.
static const int8_t PAGE_UP[PAGE_COUNT] = {END, PAGE_MAIN, PAGE_MAIN, PAGE_MAIN, PAGE_MAIN, PAGE_CONNECT, PAGE_CONNECT, PAGE_DEVICE};
static const int8_t OPENED_BY[PAGE_COUNT] = {END, R_DEVICE, R_CONNECT, R_AGENTS, R_CLOCKS, R_MAC, R_PAIR, R_BRIGHT};

static const int SWITCH_AFTERS[] = {5, 10, 15, 30, 60, 300};
#define SWITCH_AFTER_COUNT ((int)(sizeof SWITCH_AFTERS / sizeof SWITCH_AFTERS[0]))

static int s_rows[ROWS_MAX], s_nrows;
static hal_mac_t s_macs[HAL_MACS_MAX];
static int s_nmacs;
static char s_chosen[16];
static char s_val[R_MAX][24];
static char s_forget_label[64];
static int64_t s_macs_at;

// ---- Which rows a page has, on this board --------------------------------------------------------------------------

static bool row_shown(int r)
{
    if (r == R_SOUND) return BOARD.has.speaker;
    if (r == R_ROTATE) return BOARD.has.motion;
    if (r == R_BATTERY) return BOARD.has.battery;
    if (r == R_SWITCH_AFTER) return S.auto_switch; // only while it switches by itself
    return true;
}

static void refresh_rows(void)
{
    s_nrows = 0;
    const int page = S.set.page >= 0 && S.set.page < PAGE_COUNT ? S.set.page : PAGE_MAIN;
    for (int i = 0; i < ROWS_MAX && PAGE_ROWS[page][i] != END; i++) {
        if (row_shown(PAGE_ROWS[page][i])) s_rows[s_nrows++] = PAGE_ROWS[page][i];
    }
}

static int index_of(int row)
{
    for (int i = 0; i < s_nrows; i++) {
        if (s_rows[i] == row) return i;
    }
    return 0;
}

static void refresh_macs(int64_t now)
{
    s_nmacs = hal_macs_list(s_macs, HAL_MACS_MAX);
    hal_macs_chosen(s_chosen, sizeof s_chosen);
    s_macs_at = now;
}

// The Mac that's connected now, by name: what "Connected to" says.
static const char *connected_mac_name(void)
{
    if (!S.connected) return "No Mac";
    for (int i = 0; i < s_nmacs; i++) {
        if (s_macs[i].connected) return s_macs[i].name;
    }
    return "A Mac";
}

static const char *delay_text(int s, char *out, size_t n)
{
    if (s < 60) snprintf(out, n, "%d s", s);
    else snprintf(out, n, "%d min", s / 60);
    return out;
}

// ---- The rows ---------------------------------------------------------------------------------------------------------

static void group_row(int i, list_row_t *r)
{
    const int row = s_rows[i];
    char *val = s_val[row];
    switch (row) {
    case R_DEVICE: r->label = "Device"; r->chevron = true; break;
    case R_CONNECT: r->label = "Connectivity"; r->chevron = true; break;
    case R_AGENTS: r->label = "Agents"; r->chevron = true; break;
    case R_CLOCKS: r->label = "Timer & Pomodoro"; r->chevron = true; break;
    case R_BRIGHT:
        r->label = "Brightness";
        snprintf(val, sizeof s_val[0], "%d", S.bright + 1);
        r->value = val;
        r->chevron = true;
        break;
    case R_SOUND:
        r->label = "Sound";
        if (S.vol) snprintf(val, sizeof s_val[0], "%d", S.vol);
        r->value = S.vol ? val : "Off";
        break;
    case R_THEME: r->label = "Color"; r->value = S.theme ? "Inverted" : "Mono"; break;
    case R_ROTATE: r->label = "Auto-rotate"; r->value = S.autorotate ? "On" : "Off"; break;
    case R_BATTERY: {
        const int percent = S.battery_override ? S.battery_percent : S.power.percent;
        const bool charging = S.battery_override ? S.battery_charging : S.power.charging;
        if (percent < 0) snprintf(val, sizeof s_val[0], "Unknown");
        else snprintf(val, sizeof s_val[0], "%d%%%s", percent > 100 ? 100 : percent, charging ? ", charging" : "");
        r->label = "Battery";
        r->value = val;
        break;
    }
    case R_FIRMWARE: r->label = "Firmware"; r->value = BENTO_FW_VERSION; break;
    case R_MACADDR: {
        uint8_t m[6];
        hal_device_mac(m);
        snprintf(val, sizeof s_val[0], "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
        r->label = "MAC address";
        r->value = val;
        break;
    }
    case R_MAC: r->label = "Connected to"; r->value = connected_mac_name(); r->chevron = true; break;
    case R_PAIR: r->label = "Pair a new Mac"; r->chevron = true; break;
    case R_AUTO: r->label = "Auto-scroll"; r->value = S.autoscroll ? "On" : "Off"; break;
    case R_SPLIT: r->label = "Split view"; r->value = S.split == SPLIT_CLOCK ? "Clock" : "Agents"; break;
    case R_AUTO_SWITCH: r->label = "Auto switch"; r->value = S.auto_switch ? "On" : "Off"; break;
    case R_SWITCH_AFTER: r->label = "Switch after"; r->value = delay_text(S.split_after_s, val, sizeof s_val[0]); break;
    default: r->label = "?"; break; // every row has a case above
    }
}

static bool row_is_info(int row) { return row == R_BATTERY || row == R_FIRMWARE || row == R_MACADDR; }

static void mac_row(int i, list_row_t *r)
{
    if (i == 0) {
        r->label = "Any Mac";
        r->check = !s_chosen[0];
        return;
    }
    const hal_mac_t *m = &s_macs[i - 1];
    if (S.set.forget_row == i && F.now < S.set.forget_until) {
        snprintf(s_forget_label, sizeof s_forget_label, "Tap again to forget %s", m->name);
        r->label = s_forget_label;
        r->danger = true;
        return;
    }
    r->label = m->name;
    r->check = s_chosen[0] && !strcmp(s_chosen, m->key);
    if (!r->check && m->connected) r->value = "connected";
}

static void build_view(list_view_t *v)
{
    memset(v, 0, sizeof *v);
    v->title = PAGE_TITLE[S.set.page];
    if (S.set.page == PAGE_MACS) {
        v->count = s_nmacs + 1;
        v->row = mac_row;
        v->hint1 = (S.set.key > 0 && s_nmacs) ? "tap to choose, hold to forget" : "tap to choose";
    } else {
        v->count = s_nrows;
        v->row = group_row;
        const int row = S.set.key >= 0 && S.set.key < s_nrows ? s_rows[S.set.key] : END;
        v->hint1 = row == END || row_is_info(row) ? "" : row <= R_CLOCKS || row == R_BRIGHT || row == R_MAC || row == R_PAIR ? "tap to open" : "tap to change";
    }
    v->hint2 = S.set.page == PAGE_MAIN ? "swipe down to leave" : "swipe down to go back";
    v->below = S.set.page == PAGE_MAIN ? NULL : "Swipe down to go back";
    v->cursor = S.set.key;
    v->pos = S.set.pos;
    const lay_list_t *G_ = &F.L->list;
    v->scrollable = !G_->wheel && G_->rows && v->count > G_->rows;
    v->scroll = v->scrollable ? S.set.scroll : 0;
}

static void snap_list(void) { S.set.pos = (float)S.set.key; }

// ---- Moving between pages ----------------------------------------------------------------------------------------------

static void open_page(int page, int key)
{
    S.set.page = page;
    refresh_rows();
    S.set.key = key;
    S.set.scroll = 0;
    S.set.forget_row = -1;
    snap_list();
}

static void settings_enter(int64_t now)
{
    S.set.from = S.mode;
    refresh_macs(now);
    open_page(PAGE_MAIN, 0);
}

static void go_up(void)
{
    const int from = S.set.page, up = PAGE_UP[from];
    if (from == PAGE_PAIR) hal_macs_pair_cancel();
    S.set.page = up;
    refresh_rows();
    open_page(up, index_of(OPENED_BY[from]));
}

static void settings_back(int64_t now)
{
    if (S.set.page != PAGE_MAIN) {
        go_up();
    } else {
        S.mode = S.set.from ? S.set.from : &MODE_TIMER;
        ux_mark_dirty(now);
        ux_log("mode", S.mode->key);
    }
    ux_feedback(FB_TAP);
}

static void act_row(int i, int64_t now)
{
    S.set.key = i;
    switch (s_rows[i]) {
    case R_DEVICE: open_page(PAGE_DEVICE, 0); ux_feedback(FB_TAP); return;
    case R_CONNECT: open_page(PAGE_CONNECT, 0); ux_feedback(FB_TAP); return;
    case R_AGENTS: open_page(PAGE_AGENTS, 0); ux_feedback(FB_TAP); return;
    case R_CLOCKS: open_page(PAGE_CLOCKS, 0); ux_feedback(FB_TAP); return;
    case R_BRIGHT: S.set.page = PAGE_BRIGHT; ux_feedback(FB_TAP); return;
    case R_BATTERY:
    case R_FIRMWARE:
    case R_MACADDR: ux_feedback(FB_BUMP); return;
    case R_MAC: {
        refresh_macs(now);
        int key = 0;
        for (int m = 0; m < s_nmacs; m++) {
            if (s_chosen[0] && !strcmp(s_macs[m].key, s_chosen)) key = m + 1;
        }
        open_page(PAGE_MACS, key);
        ux_feedback(FB_TAP);
        return;
    }
    case R_PAIR:
        S.set.page = PAGE_PAIR;
        hal_macs_pair_start();
        ux_log("pair", "start");
        ux_feedback(FB_TAP);
        return;
    case R_AUTO:
        S.autoscroll = !S.autoscroll;
        S.next_scroll = 0;
        break;
    case R_THEME: S.theme = !S.theme; break;
    case R_SOUND:
        S.vol += 2;
        if (S.vol > 10) S.vol = 0;
        hal_volume(S.vol);
        break;
    case R_ROTATE: S.autorotate = !S.autorotate; break;
    case R_SPLIT: S.split = S.split == SPLIT_CLOCK ? SPLIT_AGENTS : SPLIT_CLOCK; break;
    case R_AUTO_SWITCH:
        S.auto_switch = !S.auto_switch;
        refresh_rows(); // Switch after comes and goes with it
        break;
    case R_SWITCH_AFTER: {
        int next = 0;
        for (int k = 0; k < SWITCH_AFTER_COUNT; k++) {
            if (SWITCH_AFTERS[k] == S.split_after_s) next = (k + 1) % SWITCH_AFTER_COUNT;
        }
        S.split_after_s = SWITCH_AFTERS[next];
        break;
    }
    default: return; // every row has a case above
    }
    ux_mark_dirty(now);
    ux_feedback(FB_TAP);
}

static void act_mac(int i, int64_t now)
{
    if (S.set.forget_row >= 0 && now < S.set.forget_until) {
        const int row = S.set.forget_row;
        S.set.forget_row = -1;
        if (row == i && i > 0 && i <= s_nmacs) {
            char msg[64];
            snprintf(msg, sizeof msg, "Forgot %s", s_macs[i - 1].name);
            hal_macs_forget(s_macs[i - 1].key);
            refresh_macs(now);
            if (S.set.key > s_nmacs) S.set.key = s_nmacs;
            ux_toast(msg, 1600);
            ux_feedback(FB_DOUBLE);
            return;
        }
    }
    S.set.key = i;
    if (i == 0) hal_macs_choose("");
    else if (i <= s_nmacs) hal_macs_choose(s_macs[i - 1].key);
    refresh_macs(now);
    ux_feedback(FB_TAP);
}

// ---- The brightness page: ten bars, set by the knob or a tap, and live. It never wraps round: in a dark room, that
// would go through full brightness to reach the dimmest.
#define BAR_X0 0.16f
#define BAR_SPAN 0.68f
#define BAR_BOTTOM 0.66f
#define BAR_TALLEST 0.14f
#define BRIGHT_NUM_Y 0.46f   // the level's number
#define BRIGHT_HINT1_Y 0.76f // the two hints under the bars
#define BRIGHT_HINT2_Y 0.82f

static void set_brightness(int level, int64_t now)
{
    S.bright = level;
    hal_screen_brightness(level);
    ux_mark_dirty(now);
    ux_feedback(FB_TICK);
}

static void draw_bright(void)
{
    const layout_t *L = F.L;
    const theme_t *T = F.T;
    const float base = L->base, mid = base / 2, step = base * BAR_SPAN / HAL_BRIGHT_LEVELS, w = step * 0.62f;
    gfx_clear(T->face);
    gfx_text("Brightness", mid, L->list.titleY, L->list.title, 700, L->list.wheel ? T->sub : T->ink, ALIGN_CENTER, 0, 255);
    char num[12];
    snprintf(num, sizeof num, "%d", S.bright + 1);
    gfx_text(num, mid, base * BRIGHT_NUM_Y, L->type.label * 1.5f, 700, T->ink, ALIGN_CENTER, 0, 255);
    for (int i = 0; i < HAL_BRIGHT_LEVELS; i++) {
        const float h = base * BAR_TALLEST * (0.25f + 0.75f * (float)i / (HAL_BRIGHT_LEVELS - 1));
        const float x = base * BAR_X0 + step * (float)i + (step - w) / 2;
        gfx_rrect(x, base * BAR_BOTTOM - h, w, h, w * 0.3f, i <= S.bright ? T->ink : T->faint, 255);
    }
    gfx_text(BOARD.has.knob ? "turn, or tap a bar" : "tap a bar", mid, base * BRIGHT_HINT1_Y, L->type.hint, 500, T->sub, ALIGN_CENTER, 0, 255);
    gfx_text("swipe down to go back", mid, base * BRIGHT_HINT2_Y, L->type.hint, 500, T->sub, ALIGN_CENTER, 0, 255);
}

static void bright_tap(float u, float v, int64_t now)
{
    const float base = F.L->base, top = base * (BAR_BOTTOM - BAR_TALLEST) - base * 0.03f, bottom = base * BAR_BOTTOM + base * 0.03f;
    if (v < top || v > bottom) return;
    const int i = (int)floorf((u - base * BAR_X0) / (base * BAR_SPAN / HAL_BRIGHT_LEVELS));
    if (i >= 0 && i < HAL_BRIGHT_LEVELS) set_brightness(i, now);
}

// ---- Input -------------------------------------------------------------------------------------------------------------

static void settings_turn(int dir, int64_t now)
{
    if (S.set.page == PAGE_BRIGHT) {
        const int next = S.bright + dir;
        if (next < 0 || next >= HAL_BRIGHT_LEVELS) ux_bump(NULL);
        else set_brightness(next, now);
        return;
    }
    if (S.set.page == PAGE_PAIR) {
        ux_bump(NULL);
        return;
    }
    list_view_t v;
    build_view(&v);
    const int next = S.set.key + dir;
    if (next < 0 || next >= v.count) {
        ux_bump(NULL);
        return;
    }
    S.set.key = next;
    ux_feedback(FB_TICK);
}

static void settings_tap(float u, float pos, int64_t now)
{
    if (S.set.page == PAGE_BRIGHT) {
        bright_tap(u, pos, now);
        return;
    }
    if (S.set.page == PAGE_PAIR) {
        settings_back(now);
        return;
    }
    list_view_t v;
    build_view(&v);
    const int i = list_row_at(&v, pos);
    if (i < 0) return;
    if (S.set.page == PAGE_MACS) act_mac(i, now);
    else act_row(i, now);
}

// Hold a Mac to forget it: it asks first, for three seconds.
static void settings_hold(int64_t now)
{
    if (S.set.page == PAGE_MACS && !F.L->list.wheel && S.press_v >= 0) { // with no knob: the row under the finger
        list_view_t v;
        build_view(&v);
        const int i = list_row_at(&v, S.press_v);
        if (i >= 0) S.set.key = i;
    }
    if (S.set.page != PAGE_MACS || S.set.key < 1 || S.set.key > s_nmacs) {
        ux_feedback(FB_BUMP);
        return;
    }
    S.set.forget_row = S.set.key;
    S.set.forget_until = now + 3 * SEC_US;
    ux_feedback(FB_TICK);
}

static void settings_swipe(swipe_t dir, int64_t now)
{
    if (dir == SWIPE_DOWN) settings_back(now);
    else ux_feedback(FB_BUMP);
}

bool settings_drag_wanted(void)
{
    if (S.set.page == PAGE_PAIR || S.set.page == PAGE_BRIGHT) return false;
    list_view_t v;
    build_view(&v);
    return v.scrollable;
}

void settings_drag(float dv, int64_t now)
{
    (void)now;
    list_view_t v;
    build_view(&v);
    const float max = list_max_scroll(&v);
    S.set.scroll -= dv;
    if (S.set.scroll < 0) S.set.scroll = 0;
    if (S.set.scroll > max) S.set.scroll = max;
}

// ---- Drawing -----------------------------------------------------------------------------------------------------------

// The pairing page, in shares of the screen's height: the same on every shape.
#define PAIR_TITLE_Y 0.33f
#define PAIR_CODE_Y 0.52f // the code, or the countdown before one
#define PAIR_HELP_Y 0.63f
#define PAIR_BACK_Y 0.71f

static void draw_pair(void)
{
    const float base = F.L->base, mid = base / 2;
    const lay_type_t *D = &F.L->type;
    const theme_t *T = F.T;
    hal_link_status_t st;
    hal_link_status(&st);
    gfx_clear(T->face);
    gfx_text("Pair with your Mac", mid, base * PAIR_TITLE_Y, D->label * 0.75f, 700, T->ink, ALIGN_CENTER, base * 0.8f, 255);
    char big[16];
    if (st.pairing) {
        if (F.capture_mask) { // a code is shown only on the screen, never in a capture
            snprintf(big, sizeof big, "000 000");
        } else {
            const unsigned code = (unsigned)(st.code % 1000000u);
            snprintf(big, sizeof big, "%03u %03u", code / 1000, code % 1000);
        }
        gfx_text(big, mid, base * PAIR_CODE_Y, D->label * 1.5f, 700, T->ink, ALIGN_CENTER, 0, 255);
        gfx_text("Type this code on your Mac.", mid, base * PAIR_HELP_Y, D->sub, 500, T->sub, ALIGN_CENTER, base * 0.75f, 255);
    } else {
        const int left = hal_macs_pair_left();
        snprintf(big, sizeof big, "%d:%02d", left / 60, left % 60);
        gfx_text(big, mid, base * PAIR_CODE_Y, D->label * 1.5f, 700, T->ink, ALIGN_CENTER, 0, 255);
        gfx_text("Open Bentobox on the new Mac.", mid, base * PAIR_HELP_Y, D->sub, 500, T->sub, ALIGN_CENTER, base * 0.75f, 255);
    }
    gfx_text("Tap to go back.", mid, base * PAIR_BACK_Y, D->hint, 500, T->sub, ALIGN_CENTER, base * 0.7f, 255);
}

static void settings_draw(void)
{
    if (S.set.page == PAGE_PAIR) {
        draw_pair();
        return;
    }
    if (S.set.page == PAGE_BRIGHT) {
        draw_bright();
        return;
    }
    list_view_t v;
    build_view(&v);
    list_draw(&v);
}

void settings_tick(int64_t now)
{
    if (S.mode != &MODE_SETTINGS) return;
    if (S.set.page < PAGE_MACS) refresh_rows(); // a row can come or go: Switch after, with Auto switch
    if (S.set.key >= s_nrows && S.set.page < PAGE_MACS) S.set.key = s_nrows ? s_nrows - 1 : 0;
    const float ease = 1 - expf(-F.dt_ms / 70);
    S.set.pos += ((float)S.set.key - S.set.pos) * ease;
    if (fabsf((float)S.set.key - S.set.pos) < 0.002f) S.set.pos = (float)S.set.key;
    if (S.set.page == PAGE_MACS && now - s_macs_at > SEC_US) refresh_macs(now);
    if (S.set.page == PAGE_CONNECT && now - s_macs_at > SEC_US) refresh_macs(now); // "Connected to" stays current
    if (S.set.forget_row >= 0 && now >= S.set.forget_until) S.set.forget_row = -1;
    if (S.set.page == PAGE_PAIR) {
        hal_link_status_t st;
        hal_link_status(&st);
        if (!st.pairing && hal_macs_pair_left() <= 0) { // done, or timed out
            refresh_macs(now);
            go_up();
        }
    }
}

void settings_apply_all(void)
{
    hal_screen_brightness(S.bright);
    hal_volume(S.vol);
    refresh_rows();
    refresh_macs(hal_now_us());
}

const char *settings_page_name(void)
{
    static const char *const NAMES[PAGE_COUNT] = {"main", "device", "connect", "agents", "clocks", "macs", "pair", "bright"};
    return S.set.page >= 0 && S.set.page < PAGE_COUNT ? NAMES[S.set.page] : "main";
}

void settings_debug_rows(void (*emit)(const char *label, const char *value))
{
    if (S.set.page == PAGE_PAIR || S.set.page == PAGE_BRIGHT) return;
    list_view_t v;
    build_view(&v);
    for (int i = 0; i < v.count; i++) {
        list_row_t r = {0};
        v.row(i, &r);
        emit(r.label, r.check ? "check" : r.value ? r.value : "");
    }
}

const ux_mode_t MODE_SETTINGS = {
    .key = "settings", .label = "Settings", .color = 0x4C74F0,
    .enter = settings_enter, .draw = settings_draw,
    .turn = settings_turn, .tap = settings_tap, .hold = settings_hold, .swipe = settings_swipe,
    .status = WHERE_LIST,
};
