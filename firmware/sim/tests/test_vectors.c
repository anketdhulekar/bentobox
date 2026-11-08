// The values in protocol/samples/vectors.json, which the desktop app must reproduce too. Compiled against firmware/link
// and the simulator's PBKDF2. A tiny reader: it only needs these fields.
#include <CommonCrypto/CommonKeyDerivation.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../../link/link.h"

static char *read_file(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    const long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *d = malloc((size_t)n + 1);
    if (fread(d, 1, (size_t)n, f) != (size_t)n) return NULL;
    d[n] = 0;
    fclose(f);
    return d;
}

static int read_ints(const char *s, const char *key, int *out, int max)
{
    const char *p = strstr(s, key);
    if (!p || !(p = strchr(p, '['))) return 0;
    p++;
    int n = 0;
    while (*p && *p != ']' && n < max) {
        char *end;
        out[n++] = (int)strtol(p, &end, 10);
        p = end;
        while (*p == ',' || *p == ' ' || *p == '\n') p++;
    }
    return n;
}

static void read_str(const char *s, const char *key, char *out, size_t cap)
{
    const char *p = strstr(s, key);
    p = strchr(p + strlen(key) + 1, '"') + 1;
    const char *q = strchr(p, '"');
    size_t n = (size_t)(q - p);
    if (n >= cap) n = cap - 1;
    memcpy(out, p, n);
    out[n] = 0;
}

int main(int argc, char **argv)
{
    char *json = read_file(argc > 1 ? argv[1] : "../../protocol/samples/vectors.json");
    if (!json) {
        puts("FAIL can't read vectors.json");
        return 1;
    }
    int fails = 0;
    int ints[256];
    const int n = read_ints(json, "\"pcm\"", ints, 256);
    int16_t pcm[256];
    for (int i = 0; i < n; i++) pcm[i] = (int16_t)ints[i];
    uint8_t adpcm[128];
    adpcm_state_t st = {0, 0};
    const size_t bytes = adpcm_encode(pcm, (size_t)n, &st, adpcm);
    char b64[256], want[256];
    b64_encode(adpcm, bytes, b64);
    read_str(json, "\"b64\"", want, sizeof want);
    if (strcmp(b64, want)) {
        printf("FAIL adpcm: %s, wanted %s\n", b64, want);
        fails++;
    } else {
        printf("ok   adpcm: %d samples give the vector's bytes\n", n);
    }
    // Two packets, each with its own state, give the same bytes.
    adpcm_state_t a = {0, 0};
    uint8_t split[128];
    size_t k = adpcm_encode(pcm, 32, &a, split);
    k += adpcm_encode(pcm + 32, (size_t)n - 32, &a, split + k);
    if (k != bytes || memcmp(split, adpcm, k)) {
        puts("FAIL adpcm in two packets");
        fails++;
    } else {
        puts("ok   adpcm: split in two packets, the same bytes");
    }
    char salt_hex[64], hash_hex[80], code[8];
    read_str(json, "\"salt\"", salt_hex, sizeof salt_hex);
    read_str(json, "\"hash\"", hash_hex, sizeof hash_hex);
    read_str(json, "\"code\"", code, sizeof code);
    uint8_t salt[32], out[32];
    const size_t salt_len = strlen(salt_hex) / 2;
    for (size_t i = 0; i < salt_len; i++) {
        unsigned v;
        sscanf(salt_hex + i * 2, "%2x", &v);
        salt[i] = (uint8_t)v;
    }
    CCKeyDerivationPBKDF(kCCPBKDF2, code, strlen(code), salt, salt_len, kCCPRFHmacAlgSHA256, 2000, out, 32);
    char got[65];
    for (int i = 0; i < 32; i++) snprintf(got + i * 2, 3, "%02x", out[i]);
    if (strcmp(got, hash_hex)) {
        printf("FAIL pbkdf2: %s, wanted %s\n", got, hash_hex);
        fails++;
    } else {
        puts("ok   pbkdf2: the simulator's hash gives the vector's");
    }
    // utf8_copy never cuts a character.
    char small[5];
    utf8_copy(small, sizeof small, "abc\xE2\x80\xA6"); // "abc…"
    if (strcmp(small, "abc")) {
        puts("FAIL utf8_copy cut a character");
        fails++;
    } else {
        puts("ok   utf8_copy keeps characters whole");
    }
    return fails ? 1 : 0;
}
