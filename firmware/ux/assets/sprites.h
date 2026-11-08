// The characters' pixel art. One mascot pixel is square. The mascot is 18 x 10: torso x 3..14, y 0..7; eyes x 5 and 12,
// y 2..3; arms x 1..2 and 15..16, y 4..5; legs x 4, 6, 11, 13, y 8..9. Hats sit above y 0.
#pragma once
#include <stddef.h>
#include <stdint.h>

typedef struct {
    int8_t x0, y0;
    uint8_t n;
    const char *const *rows;
} group_t;

typedef struct {
    const group_t *groups;
    uint8_t count;
} part_t;

typedef struct { int8_t x, y; } px_t;

typedef struct { const px_t *eyes, *spark, *mouth; uint8_t ne, ns, nm; } face_t;

enum { HAT_TOP, HAT_PARTY, HAT_CROWN, HAT_CHEF, HAT_WIZARD, HAT_COWBOY, HAT_BEANIE, HAT_PROPELLER, HAT_SPROUT, HAT_COUNT };
enum { OUT_DRESS, OUT_TUTU, OUT_CAPE, OUT_BOWTIE, OUT_SCARF, OUT_HERO, OUT_HAWAII, OUT_NONE, OUT_COUNT };
enum { ACC_SHADES, ACC_MOUSTACHE, ACC_HEADPHONES, ACC_MONOCLE, ACC_BALLOON, ACC_COFFEE, ACC_NONE, ACC_COUNT };

extern const part_t HATS[HAT_COUNT], PROP[4], CAPE_BACK, OUTFITS[OUT_COUNT], ACCS[ACC_COUNT];
extern const face_t SLIME_FACES[5], BALL_FACES[5]; // by pose eyes: 0 open, 1 closed, 2 happy, 3 wide, 4 x
