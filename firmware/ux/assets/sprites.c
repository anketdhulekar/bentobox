// The characters' pixel art, as rows of one letter per color: the same rows as the preview's. widgets/character.c draws them.
#include "sprites.h"

#define ROWS(...) (const char *const[]){__VA_ARGS__}
#define NROWS(...) (uint8_t)(sizeof(ROWS(__VA_ARGS__)) / sizeof(char *))
#define GRP(x, y, ...) {x, y, NROWS(__VA_ARGS__), ROWS(__VA_ARGS__)}
#define PART(...) {(const group_t[]){__VA_ARGS__}, (uint8_t)(sizeof((const group_t[]){__VA_ARGS__}) / sizeof(group_t))}


const part_t HATS[HAT_COUNT] = {
    [HAT_TOP] = PART(GRP(4, -5, ".KKKKKKKK.", ".KKKKKKKK.", ".KKKKKKKK.", ".RRRRRRRR.", "KKKKKKKKKK")),
    [HAT_PARTY] = PART(GRP(6, -6, "..YY..", "..PP..", ".PYPP.", ".PPCP.", "PPPPYP", "PCPPPP")),
    [HAT_CROWN] = PART(GRP(4, -4, "Y...YY...Y", "YY.YYYY.YY", "YYYYYYYYYY", "YRYYBBYYRY")),
    [HAT_CHEF] = PART(GRP(4, -6, "..WW..WW..", ".WWWWWWWW.", "WWWWWWWWWW", "WWWWWWWWWW", ".wwwwwwww.", ".WWWWWWWW.")),
    [HAT_WIZARD] = PART(GRP(3, -8, "......N.....", ".....NN.....", ".....NFN....", "....NNNN....", "....NNNFN...", "...NNNNNN...", "...NNNNNNN..", "nnnnnnnnnnnn")),
    [HAT_COWBOY] = PART(GRP(2, -4, ".....oooo.....", "....oooooo....", "O...DDDDDD...O", "OOOOOOOOOOOOOO")),
    [HAT_BEANIE] = PART(GRP(4, -5, "....WW....", "..GGGGGG..", ".GGGGGGGG.", "GGGGGGGGGG", "gggggggggg")),
    [HAT_PROPELLER] = PART(GRP(4, -5, "....kk....", "..RRFFRR..", ".RRRFFRRR.", "RRRRFFRRRR", "RRRRRRRRRR")),
    [HAT_SPROUT] = PART(GRP(4, -5, "..EE..EE..", ".EEEe.eEE.", "..EEEeEE..", "....HH....", "....H.....")),
};
const part_t PROP[4] = {
    PART(GRP(4, -6, "BBBBkkLLLL")), PART(GRP(4, -6, ".BBBkkLLL.")), PART(GRP(4, -6, "..BBkkLL..")), PART(GRP(4, -6, ".BBBkkLLL.")),
};
const part_t CAPE_BACK = PART(GRP(2, 1, "V............V", "VV..........VV", "VV..........VV", "VV..........VV", "VVV........VVV",
                                         "VVV........VVV", "VVVVVVVVVVVVVV", "VVVVVVVVVVVVVV", "VVVVVVVVVVVVVV"));
const part_t OUTFITS[OUT_COUNT] = {
    [OUT_DRESS] = PART(GRP(3, 5, "MMWMMMMMMWMM", "MMMMMMMMMMMM"), GRP(2, 7, "MMMMMMWMMMMMMM")),
    [OUT_TUTU] = PART(GRP(1, 6, "mMmMmMmMmMmMmMmM"), GRP(2, 7, "MmMmMmMmMmMmMm")),
    [OUT_CAPE] = PART(GRP(3, 1, "Y..........Y")),
    [OUT_BOWTIE] = PART(GRP(6, 5, "PPppPP", "P.pp.P")),
    [OUT_SCARF] = PART(GRP(3, 4, "TTTTTTTTTTTT"), GRP(12, 5, "tt", "tt", "tt")),
    [OUT_HERO] = PART(GRP(7, 4, "BFFB", "BFFB", ".BB.")),
    [OUT_HAWAII] = PART(GRP(3, 4, "ccFccccQcccc", "ccccQccccFcc", "cQccccFccccc", "cccccccccQcc")),
};
const part_t ACCS[ACC_COUNT] = {
    [ACC_SHADES] = PART(GRP(4, 2, "KKKkkkkKKK", "KKK....KKK")),
    [ACC_MOUSTACHE] = PART(GRP(5, 4, ".DDDDDD.", "D......D")),
    [ACC_HEADPHONES] = PART(GRP(3, -1, "kkkkkkkkkkkk"), GRP(1, 1, "RR............RR", "RR............RR", "RR............RR")),
    [ACC_MONOCLE] = PART(GRP(11, 1, "YYY", "Y.Y", "Y.Y", "YYY"), GRP(14, 5, "y"), GRP(15, 6, "y")),
    [ACC_BALLOON] = PART(GRP(17, -9, ".RRR.", "RRRRR", "RRWRR", "RRRRR", ".RRR.", "..R.."), GRP(19, -3, "S"), GRP(18, -2, "S"), GRP(18, -1, "S"),
                         GRP(18, 0, "S"), GRP(17, 1, "S"), GRP(17, 2, "S"), GRP(17, 3, "S")),
    [ACC_COFFEE] = PART(GRP(17, 1, "S.S", ".S."), GRP(17, 3, "OOOO", "WWWWW", "WWWW", "WWWW")),
};

// ---- The Codex slime's faces -----------------------------------------------------------------------------------

#define NPX(a) (uint8_t)(sizeof(a) / sizeof(px_t))
static const px_t F_OPEN_E[] = {{5, 4}, {6, 4}, {5, 5}, {6, 5}, {5, 6}, {6, 6}, {11, 4}, {12, 4}, {11, 5}, {12, 5}, {11, 6}, {12, 6}};
static const px_t F_OPEN_S[] = {{6, 4}, {12, 4}}, F_OPEN_M[] = {{8, 7}, {9, 7}};
static const px_t F_WIDE_E[] = {{4, 3}, {5, 3}, {6, 3}, {4, 4}, {5, 4}, {6, 4}, {4, 5}, {5, 5}, {6, 5}, {4, 6}, {5, 6}, {6, 6},
                                {11, 3}, {12, 3}, {13, 3}, {11, 4}, {12, 4}, {13, 4}, {11, 5}, {12, 5}, {13, 5}, {11, 6}, {12, 6}, {13, 6}};
static const px_t F_WIDE_S[] = {{6, 3}, {5, 4}, {13, 3}, {12, 4}}, F_WIDE_M[] = {{8, 7}, {9, 7}, {8, 8}, {9, 8}};
static const px_t F_HAPPY_E[] = {{4, 5}, {5, 4}, {6, 4}, {7, 5}, {10, 5}, {11, 4}, {12, 4}, {13, 5}};
static const px_t F_HAPPY_M[] = {{7, 7}, {8, 8}, {9, 8}, {10, 7}};
static const px_t F_CLOSED_E[] = {{4, 5}, {5, 6}, {6, 6}, {7, 5}, {10, 5}, {11, 6}, {12, 6}, {13, 5}};
static const px_t F_X_E[] = {{4, 4}, {6, 4}, {5, 5}, {4, 6}, {6, 6}, {11, 4}, {13, 4}, {12, 5}, {11, 6}, {13, 6}};
static const px_t F_X_M[] = {{8, 8}, {9, 7}, {10, 8}};
const face_t SLIME_FACES[5] = { // by pose eyes: 0 open, 1 closed, 2 happy, 3 wide, 4 x
    {F_OPEN_E, F_OPEN_S, F_OPEN_M, NPX(F_OPEN_E), NPX(F_OPEN_S), NPX(F_OPEN_M)},
    {F_CLOSED_E, NULL, F_OPEN_M, NPX(F_CLOSED_E), 0, NPX(F_OPEN_M)},
    {F_HAPPY_E, NULL, F_HAPPY_M, NPX(F_HAPPY_E), 0, NPX(F_HAPPY_M)},
    {F_WIDE_E, F_WIDE_S, F_WIDE_M, NPX(F_WIDE_E), NPX(F_WIDE_S), NPX(F_WIDE_M)},
    {F_X_E, NULL, F_X_M, NPX(F_X_E), 0, NPX(F_X_M)},
};

// ---- The Grok ball's faces --------------------------------------------------------------------------------------

static const px_t B_OPEN_E[] = {{7, 2}, {8, 2}, {7, 3}, {8, 3}, {7, 4}, {8, 4}, {7, 5}, {8, 5}, {11, 2}, {12, 2}, {11, 3}, {12, 3}, {11, 4}, {12, 4}, {11, 5}, {12, 5}};
static const px_t B_OPEN_G[] = {{7, 2}, {11, 2}};
// The preview's round eye: 3 wide and 5 tall, without its corners.
static const px_t B_WIDE_E[] = {{7, 1}, {6, 2}, {7, 2}, {8, 2}, {6, 3}, {7, 3}, {8, 3}, {6, 4}, {7, 4}, {8, 4}, {7, 5},
                                {12, 1}, {11, 2}, {12, 2}, {13, 2}, {11, 3}, {12, 3}, {13, 3}, {11, 4}, {12, 4}, {13, 4}, {12, 5}};
static const px_t B_WIDE_G[] = {{6, 2}, {7, 2}, {11, 2}, {12, 2}};
static const px_t B_HAPPY_E[] = {{6, 3}, {7, 3}, {5, 4}, {8, 4}, {12, 3}, {13, 3}, {11, 4}, {14, 4}};
static const px_t B_CLOSED_E[] = {{7, 4}, {8, 4}, {11, 4}, {12, 4}};
static const px_t B_X_E[] = {{6, 2}, {8, 2}, {7, 3}, {6, 4}, {8, 4}, {11, 2}, {13, 2}, {12, 3}, {11, 4}, {13, 4}};
const face_t BALL_FACES[5] = {
    {B_OPEN_E, B_OPEN_G, NULL, NPX(B_OPEN_E), NPX(B_OPEN_G), 0},
    {B_CLOSED_E, NULL, NULL, NPX(B_CLOSED_E), 0, 0},
    {B_HAPPY_E, NULL, NULL, NPX(B_HAPPY_E), 0, 0},
    {B_WIDE_E, B_WIDE_G, NULL, NPX(B_WIDE_E), NPX(B_WIDE_G), 0},
    {B_X_E, NULL, NULL, NPX(B_X_E), 0, 0},
};
