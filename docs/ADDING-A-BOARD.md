# Adding a board

Bento is organized so that a new board is mostly a table of facts. The screens and modes don't change unless the board brings a screen shape Bento hasn't seen.

## What you add

| The new board has | What you add |
|---|---|
| A round or square screen, and chips Bento already supports | `firmware/boards/<board>/` |
| A chip Bento doesn't support yet | Also `firmware/drivers/<chip>/`, an ESP-IDF component that fills in one of the structs in `driver_api.h` |
| A new screen shape, such as a wide rectangle | Also `firmware/ux/layouts/<shape>` |
| A different chip family from the ESP32 | Also `firmware/platform/<family>/` |

Many AMOLED boards share the same display, touch and power chips, so most new boards should stop at the first row.

## The board folder

```text
firmware/boards/<board>/
  board_info.c       the portable facts the UX reads: screen, features. The simulator compiles it too
  board.c            the hardware facts: pins, which chips, how feedback maps to vibration or sound
  board.cmake        the board's chips: its list of folders under firmware/drivers/
  sdkconfig.board    its build settings: flash size, PSRAM
  README.md          where to buy it, its quirks, how to reach its USB port
```

`board_info.c` is only this, and includes nothing but `hal.h`:

```c
const board_info_t BOARD = {
    .name = "amoled-2.16",
    .width = 480, .height = 480, .shape = SHAPE_SQUARE, .panel = PANEL_AMOLED,
    .has = { .knob = false, .haptics = false, .mic = true,
             .speaker = true, .motion = true, .battery = true },
};
```

`board.c` names the chips and gives each its pins. It's a table, too:

```c
static const co5300_cfg_t SCREEN = { .sclk = 38, .d0 = 4, /* ... */ .width = 480, .height = 480 };

const board_hw_t BOARD_HW = {
    .i2c_sda = 15, .i2c_scl = 14,
    .screen = &CO5300, .screen_cfg = &SCREEN, .brightness = {70, 150, 255},
    .touch = &CST9217, .touch_cfg = &TOUCH,
    .speaker = &ES8311, .speaker_cfg = &SPEAKER,
    .feedback = { [FB_TICK] = {.min_gap_ms = 40, SOUND(TICK)}, /* ... */ },
};
```

`firmware/platform/esp32/board_hw.h` lists every field. A chip the board doesn't have stays out.

The UX reads these facts and adapts:

- **Screen:** It picks the layout for the shape and sizes it to the width and height.
- **Features:** It hides what the board doesn't have. A board without a speaker has no Sound row in Settings, and a board without a mic has no mic button.
- **Feedback:** It asks for a "tick" or an "alert" by name, and the board plays it with whatever it has.

## Steps

1. **Create the folder:** `tools/bento new-board <board>` copies `firmware/boards/_template/`.
2. **Fill in the facts:** Take the pins and chips from the board's schematic. If the screen shows upside down when the board is held the right way up, set `mount_deg` to 180 in `board_info.c`: the screen and touch both follow. A quarter turn (90 or 270) only works on a square panel: a wide or tall screen turns only upside down, both for `mount_deg` and when a motion sensor turns it.
3. **Add any missing chip drivers** under `firmware/drivers/<chip>/`. A driver that needs a component from the registry names it in its own `idf_component.yml`.
4. **Try it without hardware:** Add the board to `firmware/sim/board_select.c` and `firmware/sim/Makefile`. `tools/bento sim <board>` then opens the real UX in a Mac window at this board's size and shape, and `tools/bento test sim --board <board>` runs the tests on it ([Tests](TESTS.md)).
5. **Prepare the board:** Back up its flash, and add its MAC address to `boards.local.md`, which git ignores.
6. **Build and flash, with the owner's go-ahead:** `tools/bento build <board>`, then `tools/bento flash <board>`, which refuses to flash if the connected board's MAC doesn't match.
7. **Check it on the hardware:** `tools/bento build <board> --hw-test` runs a small test instead of the UX, with color bars, touch dots, a counter, feedback on input and a log of Bluetooth, motion and power. Then check touch positions, rotation if it has a motion sensor, and frame rate with the real UX.

The new board then needs nothing else from the desktop app, because it speaks the same Bluetooth protocol.

## Rules that keep this easy

- **The UX knows only `firmware/hal/`.** Code in `firmware/ux/` can't include drivers, board files or ESP-IDF headers. Compiling it on the Mac for the simulator enforces this.
- **No board names in the UX.** Code in `firmware/ux/` checks features, such as `BOARD.has.speaker`, and never which board it's running on.
- **One driver per chip, not per board.** Pins come from the board folder, so two boards with the same chip share its driver.
- **Modes don't know about boards.** Each mode lives in `firmware/ux/modes/` and asks its layout where things go.
