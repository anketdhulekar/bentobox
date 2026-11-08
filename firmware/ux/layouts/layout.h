// One layout per screen shape, as tables: the same numbers as LAYOUTS in preview/bento.html, in the preview's units.
// A mode asks its layout where things go, and gfx scales the units to the board. A new shape is one more table here.
#pragma once
#include <stdbool.h>

typedef struct { float inset, width, radius; } lay_rim_t;

typedef struct {
    float pillY, pill, taskY, task, taskW, timeY, time, timeLong, statusY, status, micY, micR, noMicShift;
} lay_clock_t;

typedef struct {
    float bottom, inset, width, radius, pillY, pill, pillW, timeY, time, timeLong, taskY, task, taskW;
} lay_arch_t;

typedef struct { float feet, sc, nameY, name, actY, act, lineY, line, textW, topY, top; } lay_zoo_t;

typedef struct { float x, y; bool on_rim; } lay_point_t;

typedef struct {
    float h;
    lay_point_t clock, arc, zoo, list;
} lay_status_t;

typedef struct { float label, sub, hint; } lay_type_t; // sizes the pairing page and the like share

#define MENU_CELLS 6

typedef struct {
    const char *key;
    float x, y, w, h;
    float icon, ix, iy;  // the icon's size and middle
    float lx, ly, label; // the name
    float sy;            // the line under the name; 0 if there's none
} lay_tile_t;

// The menu. A round screen has `order`: compartments around the middle, clockwise, the first of them `first` slots past
// twelve o'clock (-0.5: its middle half a slot before, so twelve o'clock falls between the first two). A square one has
// `tiles`.
typedef struct {
    const char *order[MENU_CELLS];
    float first;
    float ro, ri, gap, corner, mid, icon, icon_dy, label_dy, label, hub, hub_small;
    lay_tile_t tiles[MENU_CELLS];
    float radius, sub;
} lay_menu_t;

typedef struct {
    bool wheel;
    int rows; // a row list: at most this many show, and a longer list scrolls
    float rowH, y0, maxY, x0, x1, titleY, title, hintY, hint, big, small;
} lay_list_t;

// The Clock: the time, its AM or PM, the date and the day.
typedef struct { float timeY, time, ampm, dateY, date, dayY, day; } lay_dayclock_t;

typedef struct {
    float topX, topY, top, titleY, title, titleW, dotsY, dotR, dotGap, keyTop, keyH, keyW, keyR, key;
} lay_lock_t;

// The details card: the character, the name under or beside it, then rows of a label and a value.
typedef struct {
    float charX, charFeet, charSc, nameX, nameY, name, nameW, subY, sub, rowY, rowH, row, labelX, valueX, valueW, hintY, hint;
    bool centered;
} lay_card_t;

typedef struct { float cy, step, width, maxH; } lay_wave_t;

typedef struct {
    float base;
    lay_rim_t rim;
    lay_clock_t clock;
    lay_arch_t arch;
    lay_zoo_t zoo, zooClock;
    lay_dayclock_t dayFull, dayLow; // the Clock alone, and below a running clock's arc
    float dotsRimInset;               // round: the agents' strip follows the rim
    bool hasDots; float dotsY, dotsGap; // square: the strip is a row
    int stripHalf;                    // how many dots show on each side of the chosen one
    float stripStep;                  // round: from one dot to the next, as a share of the rim
    float countY, countSize;          // the "12 / 240" over the chosen dot
    lay_status_t status;
    lay_type_t type;
    lay_menu_t menu;
    lay_list_t list;
    lay_lock_t lock;
    lay_card_t card;
    lay_wave_t wave;
    float toastY, toast;
} layout_t;

extern const layout_t LAYOUT_ROUND, LAYOUT_SQUARE;
