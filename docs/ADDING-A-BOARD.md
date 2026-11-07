# Adding a board

Bento is organized so that a new board is mostly a table of facts. The screens and modes don't change unless the board brings a screen shape Bento hasn't seen.

This describes the target design. The firmware isn't written yet, so the names and code below may change as it's built.

## What you add

| The new board has | What you add |
|---|---|
| A round or square screen, and chips Bento already supports | `firmware/boards/<board>/` |
| A chip Bento doesn't support yet | Also `firmware/drivers/<chip>/` |
| A new screen shape, such as a wide rectangle | Also `firmware/ux/layouts/<shape>` |
| A different chip family from the ESP32 | Also `firmware/platform/<family>/` |

Many AMOLED boards share the same display, touch and power chips, so most new boards should stop at the first row.

## The board folder

```text
firmware/boards/<board>/
  board.c            the board's facts: screen, features, pins and chips
  sdkconfig.board    its build settings: flash size, PSRAM
  README.md          where to buy it, its quirks, how to reach its USB port
```

`board.c` is mostly a description, for example:

```c
const board_info_t BOARD = {
    .name = "amoled-2.16",
    .width = 480, .height = 480, .shape = SHAPE_SQUARE, .panel = PANEL_AMOLED,
    .has = { .knob = false, .haptics = false, .mic = true,
             .speaker = true, .motion = true, .battery = true },
    .screen = &co5300, .touch = &cst9217, .speaker = &es8311,
    .motion = &qmi8658, .power = &axp2101,
    .pins = { /* ... */ },
};
```

The UX reads these facts and adapts:

- **Screen:** It picks the layout for the shape and sizes it to the width and height.
- **Features:** It hides what the board doesn't have. A board without a speaker has no Sound row in Settings, and a board without a mic has no mic button.
- **Feedback:** It asks for a "tick" or an "alert" by name, and the board plays it with whatever it has.

## Steps

1. **Create the folder:** `tools/bento new-board <board>` copies `firmware/boards/_template/`.
2. **Fill in the facts:** Take the pins and chips from the board's schematic.
3. **Add any missing chip drivers** under `firmware/drivers/<chip>/`.
4. **Try it without hardware:** `tools/bento sim <board>` runs the real UX on the Mac and saves a screenshot of every screen at this board's size and shape.
5. **Prepare the board:** Back up its flash, and add its MAC address to `boards.local.md`, which git ignores.
6. **Build and flash, with the owner's go-ahead:** `tools/bento flash <board>` refuses to flash if the connected board's MAC doesn't match.
7. **Check it on the hardware:** Touch positions, rotation if it has a motion sensor, and frame rate.

The new board then needs nothing else from the desktop app, because it speaks the same Bluetooth protocol.

## Rules that keep this easy

- **The UX knows only `firmware/hal/`.** Code in `firmware/ux/` can't include drivers, board files or ESP-IDF headers. Compiling it on the Mac for the simulator enforces this.
- **No board names in the UX.** Code in `firmware/ux/` checks features, such as `BOARD.has.speaker`, and never which board it's running on.
- **One driver per chip, not per board.** Pins come from the board folder, so two boards with the same chip share its driver.
- **Modes don't know about boards.** Each mode lives in `firmware/ux/modes/` and asks its layout where things go.
