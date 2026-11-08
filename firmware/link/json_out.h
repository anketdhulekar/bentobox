// A small JSON writer into a fixed buffer. Strings are escaped and never produce invalid UTF-8, because the other end
// parses what comes out and a name could hold anything. If the buffer is too small, it says so and writes no more.
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    char *buf;
    size_t cap, len;
    bool overflow;
} jw_t;

void jw_init(jw_t *w, char *buf, size_t cap);
void jw_begin(jw_t *w, const char *key);      // an object; key NULL inside an array or at the top
void jw_end(jw_t *w);
void jw_array(jw_t *w, const char *key);
void jw_array_end(jw_t *w);
void jw_str(jw_t *w, const char *key, const char *value);
void jw_i64(jw_t *w, const char *key, int64_t value);
void jw_bool(jw_t *w, const char *key, bool value);
void jw_float(jw_t *w, const char *key, float value);
// Ends the document with a newline and a NUL. false if it didn't fit.
bool jw_finish(jw_t *w);
