// The UX: everything a person sees and touches, the same on every board. It uses only firmware/hal/hal.h and
// firmware/link/, so it compiles on the Mac for the simulator. The platform's main loop is just:
//
//     hal_init();
//     ux_init();
//     for (;;) ux_frame();
//
// ux_frame reads input, runs the clocks and the link, draws what changed, and paces itself with hal_sleep_ms.
#pragma once

#ifdef __cplusplus
extern "C" {
#endif

void ux_init(void);
void ux_frame(void);

#ifdef __cplusplus
}
#endif
