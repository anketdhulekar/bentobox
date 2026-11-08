// The Clock: the time of day and the date. On its own it fills the screen; under a running clock it takes the space
// below the arc, as the agents do. The time is the Mac's, from `state`: until a Mac has told it, there's none to show.
#include <stdio.h>
#include <string.h>

#include "../app.h"

static const char *const DAYS[7] = {"Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"};
static const char *const DAYS_SHORT[7] = {"Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"};
static const char *const MONTHS[12] = {"January", "February", "March", "April", "May", "June", "July", "August", "September",
                                       "October", "November", "December"};
static const char *const MONTHS_SHORT[12] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};

bool daytime_get(int64_t now, daytime_t *d)
{
    if (!link_clock_known()) return false;
    const int64_t local = link_unix_now(now) + link_utc_offset();
    const int64_t days = local >= 0 ? local / 86400 : (local - 86399) / 86400;
    const int secs = (int)(local - days * 86400), hour = secs / 3600;
    d->hour = hour;
    d->minute = secs / 60 % 60;
    d->pm = hour >= 12;
    d->hour12 = hour % 12 ? hour % 12 : 12;
    d->weekday = (int)(((days + 4) % 7 + 7) % 7); // 1 January 1970 was a Thursday
    // The civil date from a day count (Howard Hinnant's algorithm).
    const int64_t z = days + 719468, era = (z >= 0 ? z : z - 146096) / 146097;
    const int64_t doe = z - era * 146097, yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100), mp = (5 * doy + 2) / 153;
    d->day = (int)(doy - (153 * mp + 2) / 5 + 1);
    d->month = (int)(mp < 10 ? mp + 3 : mp - 9);
    d->year = (int)(yoe + era * 400 + (d->month <= 2));
    return true;
}

// "7th": 1st, 2nd, 3rd, 4th ... 11th, 12th, 13th ... 21st, 22nd, 23rd ... 31st.
const char *daytime_ordinal(int day)
{
    const int tens = day % 100, ones = day % 10;
    return tens >= 11 && tens <= 13 ? "th" : ones == 1 ? "st" : ones == 2 ? "nd" : ones == 3 ? "rd" : "th";
}

void daytime_text(const daytime_t *d, daytime_text_t what, char *out, size_t n)
{
    switch (what) {
    case DT_TIME: snprintf(out, n, "%d:%02d", d->hour12, d->minute); break;
    case DT_AMPM: snprintf(out, n, "%s", d->pm ? "PM" : "AM"); break;
    case DT_DATE: snprintf(out, n, "%d%s %s %d", d->day, daytime_ordinal(d->day), MONTHS[d->month - 1], d->year); break;
    case DT_DAY: snprintf(out, n, "%s", DAYS[d->weekday]); break;
    case DT_SHORT_DATE: snprintf(out, n, "%s %d%s %s", DAYS_SHORT[d->weekday], d->day, daytime_ordinal(d->day), MONTHS_SHORT[d->month - 1]); break;
    }
}

// The time with a smaller AM or PM after it, the two centred together. tabular: the clocks' digits, every one as wide.
void daytime_draw_time(const daytime_t *d, float cx, float y, float size, float ampm, uint16_t ink, uint16_t sub, bool tabular)
{
    char hm[8], ap[4];
    daytime_text(d, DT_TIME, hm, sizeof hm);
    daytime_text(d, DT_AMPM, ap, sizeof ap);
    const float w = tabular ? gfx_time_width(hm, size) : gfx_text_width(hm, size, 700);
    const float gap = size * 0.1f, aw = gfx_text_width(ap, ampm, 700), x0 = cx - (w + gap + aw) / 2;
    if (tabular) gfx_time(hm, x0 + w / 2, y, size, ink, 1, 255, 0);
    else gfx_text(hm, x0, y, size, 700, ink, ALIGN_LEFT, 0, 255);
    gfx_text(ap, x0 + w + gap, y, ampm, 700, sub, ALIGN_LEFT, 0, 255);
}

static void dayclock_draw(void)
{
    const layout_t *L = F.L;
    const theme_t *T = F.T;
    const ux_mode_t *run = clock_running();
    const lay_dayclock_t *D = run ? &L->dayLow : &L->dayFull;
    const float mid = L->base / 2;
    gfx_clear(T->face);
    if (run) clock_draw_arch(run);
    daytime_t d;
    if (!daytime_get(F.now, &d)) {
        gfx_text("--:--", mid, D->timeY, D->time * 0.8f, 700, T->sub, ALIGN_CENTER, 0, 255);
        gfx_text("Waiting for the Mac\xE2\x80\x99s time", mid, D->dateY, D->date, 600, T->sub, ALIGN_CENTER, L->base * 0.8f, 255);
        return;
    }
    char line[48];
    daytime_draw_time(&d, mid, D->timeY, D->time, D->ampm, T->ink, T->sub, true);
    daytime_text(&d, DT_DATE, line, sizeof line);
    gfx_text(line, mid, D->dateY, D->date, 600, T->ink, ALIGN_CENTER, L->base * 0.8f, 255);
    daytime_text(&d, DT_DAY, line, sizeof line);
    gfx_text(line, mid, D->dayY, D->day, 500, T->sub, ALIGN_CENTER, 0, 255);
}

static void dayclock_tap(float u, float v, int64_t now)
{
    (void)u;
    const ux_mode_t *run = clock_running();
    if (run && v < F.L->arch.bottom) { // the clock's section: back to its face, as on Agents
        S.mode = run;
        ux_mark_dirty(now);
        ux_feedback(FB_TAP);
        ux_log("mode", run->key);
        return;
    }
    ux_bump(NULL);
}

static void dayclock_turn(int dir, int64_t now)
{
    (void)dir;
    (void)now;
    ux_bump(NULL);
}

static void dayclock_hold(int64_t now)
{
    (void)now;
    ux_bump(NULL);
}

static void dayclock_swipe(swipe_t dir, int64_t now)
{
    (void)dir;
    (void)now;
    ux_feedback(FB_BUMP);
}

const ux_mode_t MODE_CLOCK = {
    .key = "clock", .label = "Clock", .color = 0xEC4899,
    .draw = dayclock_draw,
    .turn = dayclock_turn, .tap = dayclock_tap, .hold = dayclock_hold, .swipe = dayclock_swipe,
    .status = WHERE_ZOO,
};
