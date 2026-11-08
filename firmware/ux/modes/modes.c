// The modes: a plain list. A new one is one more line here, a file that defines its ux_mode_t, and a place in the layouts' menus.
#include <string.h>

#include "../app.h"

const ux_mode_t *const MODES[] = {
    &MODE_TIMER,
    &MODE_POMODORO,
    &MODE_SETTINGS,
    &MODE_ZOO,
    &MODE_LOCK,
    &MODE_CLOCK,
};
const int MODE_COUNT = (int)(sizeof MODES / sizeof MODES[0]);

const ux_mode_t *mode_by_key(const char *key)
{
    for (int i = 0; i < MODE_COUNT; i++) {
        if (!strcmp(MODES[i]->key, key)) return MODES[i];
    }
    return NULL;
}

const ux_mode_t *clock_running(void)
{
    for (int i = 0; i < MODE_COUNT; i++) {
        if (MODES[i]->running && MODES[i]->running()) return MODES[i];
    }
    return NULL;
}
