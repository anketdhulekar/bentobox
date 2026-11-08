// The one function test_vectors.c needs from link.c, which otherwise wants the whole HAL. The same few lines: see
// firmware/link/link.c. run_tests.py checks that they still match.
#include <stdint.h>
#include <string.h>

#include "../../link/link.h"

void utf8_copy(char *dst, size_t n, const char *src)
{
    if (!n) return;
    size_t len = strlen(src);
    if (len >= n) {
        len = n - 1;
        while (len > 0 && ((uint8_t)src[len] & 0xC0) == 0x80) len--; // don't end inside a character
    }
    memmove(dst, src, len);
    dst[len] = 0;
}
