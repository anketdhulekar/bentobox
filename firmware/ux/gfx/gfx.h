// The renderer. One frame buffer, in logical (upright) pixels, drawn in immediate mode.
//
// Each frame runs the screen's drawing code twice. The record pass hashes every primitive into the bands it touches;
// a band whose hash differs from the previous frame's has changed. The paint pass then draws only into those bands,
// and only they are presented. So an unchanged frame costs a little arithmetic and no PSRAM traffic at all, and
// comparing pixels against the previous frame (about 15 ms on PSRAM at 480 x 480) isn't needed. The debug interface's
// "#verify on" repaints everything into a second buffer each frame and checks that the bands agree.
//
// Coordinates are in layout units: the preview's numbers, scaled to the board by gfx_init's k. No function here uses
// float division or libm per pixel: Xtensa does both in software.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum { ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT } gfx_align_t;

// To the nearest 5 and 6 bits. Cutting them off instead would turn a warm white pink: #F0EEEA would be #F7EFEF.
static inline uint16_t rgb565(uint32_t rgb)
{
    const uint32_t r = (rgb >> 16) & 0xFF, g = (rgb >> 8) & 0xFF, b = rgb & 0xFF;
    return (uint16_t)(((r * 249 + 1014) >> 11) << 11 | ((g * 253 + 505) >> 10) << 5 | ((b * 249 + 1014) >> 11));
}

// ---- The frame -------------------------------------------------------------------------------------------------

void gfx_init(int width, int height, float k);
int gfx_width(void);
int gfx_height(void);
float gfx_scale(void);
uint16_t *gfx_framebuffer(void);
int gfx_band_count(void);
void gfx_set_offset(int ox, int oy); // the burn-in shift, in layout units

void gfx_record_begin(void);
uint64_t gfx_record_end(void); // a bit per band: those that changed since the last frame
#ifdef BENTO_DEBUG_INTERFACE
// Why a frame repainted ("#why" in docs/DEBUG.md): every mark the recording pass makes, with its rows, its hash, the code
// that made it and, for text, its first letters, so a frame's marks can be compared with the last frame's.
typedef struct {
    uint32_t hash, at;
    int16_t y0, y1;
    char text[16];
    uint32_t words[18]; // what it hashed: a shape's coordinates as float bits, or an icon's first three parts' inputs
} gfx_mark_t;
void gfx_why(bool on);
void gfx_why_label(const char *s, int len); // text primitives: what the next mark draws
// The marks that differ from the last frame's, in drawing order, at most max of them, and what each was the frame before. *n and *prev_n: how many marks
// each frame made. *ref: the address of gfx_mark_rows, so a host can place the others in the binary.
int gfx_why_diff(gfx_mark_t *out, gfx_mark_t *was, int max, int *n, int *prev_n, uint32_t *ref);
void gfx_why_words(const void *words, int n); // shapes and icons: what the next mark hashed
#endif
void gfx_paint_begin(uint64_t bands, uint16_t *target); // target: the frame buffer, or another of the same size
void gfx_paint_end(void);

// Limits every primitive to these rows (layout units) until gfx_clip_reset, in both passes.
void gfx_clip_rows(float y0, float y1);
void gfx_clip_reset(void);

// The whole screen, whatever the burn-in shift: anything shifted would leave its edge unpainted.
void gfx_clear(uint16_t c);

// ---- Shapes: alpha is 0 .. 255. Edges are anti-aliased ------------------------------------------------------------

void gfx_rect(float x, float y, float w, float h, uint16_t c, int alpha); // fractional edges blend
void gfx_block(float x1, float y1, float x2, float y2, uint16_t c, int alpha); // rounds to whole pixels: pixel art
void gfx_disc(float cx, float cy, float r, uint16_t c, int alpha);
void gfx_ring(float cx, float cy, float r, float lw, uint16_t c, int alpha);
void gfx_rrect(float x, float y, float w, float h, float r, uint16_t c, int alpha);
void gfx_rrect_line(float x, float y, float w, float h, float r, float lw, uint16_t c, int alpha);

// A small drawing made of overlapping strokes, blended as one: the overlaps don't show even at low alpha.
// Up to 128 x 128 pixels. Angles as in canvas: radians, clockwise from three o'clock.
void gfx_icon_begin(float x0, float y0, float x1, float y1);
void gfx_icon_line(float ax, float ay, float bx, float by, float lw, bool round_caps);
void gfx_icon_arc(float cx, float cy, float r, float lw, float a0, float a1, bool round_caps);
void gfx_icon_ring(float cx, float cy, float r, float lw);
void gfx_icon_rrect(float x, float y, float w, float h, float r);
void gfx_icon_poly(const float *xy, int points); // filled
void gfx_icon_end(uint16_t c, int alpha);
void gfx_icon_end_contrast(void); // black over anything bright, white over anything dark, pixel by pixel

// ---- Text --------------------------------------------------------------------------------------------------------

// y is the baseline. maxw > 0 cuts the text with an ellipsis, as the preview's text() does. Returns the width.
float gfx_text(const char *s, float x, float y, float size, int weight, uint16_t c, gfx_align_t align, float maxw, int alpha);
float gfx_text_width(const char *s, float size, int weight);
// The same text, clipped to [wx0, wx1) with its edges fading over `fade`: the task line while it scrolls.
void gfx_text_window(const char *s, float x, float y, float size, int weight, uint16_t c, float wx0, float wx1, float fade, int alpha);

// The clock's digits: tabular, centered on cx, scaled around its middle. shine (0..1) adds the blurred glow, at its
// fullest at 1: the preview's shadow stays opaque and only its blur grows, so at its peak the glow is at full strength.
void gfx_time(const char *s, float cx, float y, float size, uint16_t c, float scale, int alpha, float shine);
float gfx_time_width(const char *s, float size); // in layout units, as gfx_time lays it out
