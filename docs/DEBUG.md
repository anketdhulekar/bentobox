# The debug interface

A plain-text interface for the test harness, compiled in when `BENTO_DEBUG_INTERFACE` is defined. It's the same text everywhere: USB serial on a board, and TCP on `127.0.0.1:7778` in the simulator (`firmware/sim/`). The code is in `firmware/ux/debug.c`, through `hal_debug_read` and `hal_debug_write`.

It never shows a real pairing code, a lock code or its hash, and it logs no transcripts. See [What it won't show](#what-it-wont-show).

## Which builds have it

The simulator always has it. A board's everyday build doesn't: `CONFIG_BENTO_DEBUG_INTERFACE` is off by default. Build it in with `--debug`, which uses its own build folder, `firmware/build-<board>-debug`, so the two never mix:

```sh
tools/bento flash knob-lcd-1.8 --debug     # for tools/device_test.py
tools/bento flash knob-lcd-1.8             # everyday: no debug interface
```

**A debug build is for development only.** Anyone with a USB cable can send `#unlock`, read the state, and play the Mac over `#link usb`, so a debug build gives no protection from its lock. Flash an everyday build before you leave a board anywhere.

The simulator's harness is `firmware/sim/tests/harness.py`, and `firmware/sim/tests/run_tests.py` shows every command in use.

## Single keys

Each key acts at once and needs no newline.

| Key | Effect |
|---|---|
| `s` | Tap the screen, away from the lists and the mic: start, pause, resume, dismiss time's up |
| `r` | Hold: reset on a clock; the details card in Agent Zoo; on a Mac in the list, ask to forget it |
| `+` `-` | Turn the knob clockwise or anticlockwise |
| `e` | Tap the task: past tasks |
| `o` | Tap the pill: projects |
| `m` | Tap the mic |
| `t` | Tap the middle of the screen: choose in a wheel list, or stop listening |
| `u` `p` | Swipe up or down |
| `<` `>` | Swipe left or right |
| `b` | The Menu button |
| `d` | Print `STATE {json}` |
| `c` | Send a screen capture |

Swipes go in as touches at the panel, so they turn with the screen like a finger's.

## `#` commands

One per line. Each answers `CMD ok <name> <args>` or `CMD error <name> <args>`.

### Input

| Command | Effect |
|---|---|
| `#tap X Y` | A tap at an upright position, in layout units (the preview's numbers) |
| `#rawtap X Y` | A tap at a panel pixel, before any rotation. This tests the touch mapping |
| `#hold [X Y]` | A hold, optionally at a position |
| `#swipe up\|down\|left\|right` | A swipe in the screen's direction |
| `#swipe X0 Y0 X1 Y1` | A swipe between two upright positions |
| `#turn N` | N knob clicks; negative is anticlockwise |
| `#button menu\|up\|down` | A button press |

### State

| Command | Effect |
|---|---|
| `#task TEXT` | Sets the task. `#task ` alone clears it. It clears the Toggl task link |
| `#task_id N` | Sets the Toggl task link |
| `#project ID [#RRGGBB NAME]` | Selects a project, with its color and name if it isn't in the list. `0` means none. Nothing goes to Toggl |
| `#set N` | Pomodoro length in minutes, while reset |
| `#session N` | The Timer's total in seconds, paused. Not while running |
| `#elapsed N` | The running Timer has been going this long |
| `#remaining N` | Pomodoro time left in seconds. Paused, unless it's running |
| `#mode KEY` | Opens a mode: `timer`, `pomodoro`, `zoo`, `clock` or `settings`. It counts as input, as a choice in the menu does, so Auto switch waits from here |
| `#theme mono\|inverted` | The color profile |
| `#bright 0..9` `#vol 0..10` | Brightness, 0 the dimmest, and volume |
| `#autoscroll on\|off` `#autorotate on\|off` | Those settings |
| `#lock` `#unlock` | Locks or unlocks. `#lock` needs a code to be set |
| `#lock_wait_end` | Ends the wait after wrong codes at once, as if its time had passed. The count of strikes stays, so the next wait is still longer |
| `#tilt DEG` | Overrides the motion sensor's tilt |
| `#tilt_off` | Back to the sensor |
| `#flat on\|off` | Lying flat, so gravity gives no direction |
| `#battery N [charging]` | Overrides the battery. `#battery off` goes back to the real reading |
| `#mac off\|on` | As if no Mac were connected. `off` shows "Mac offline" at once rather than after 30 s |
| `#link usb\|ble` | See [The link over this interface](#the-link-over-this-interface) |
| `#list` | Prints `PROJECTS [...]` and `TASKS [...]`, the cached lists |
| `#quiet` | Clears any hint or toast showing, so a test can look at what's underneath |
| `#reboot` | Saves the settings and restarts |

### Effects

| Command | Effect |
|---|---|
| `#fx done\|gone\|new\|idle\|working\|failed\|tap` | Plays that effect on the agent on screen, as if the Mac had just said so. Its state in the list doesn't change, and `gone` brings it back afterwards. With no agents, a stand-in called `fx-test` appears first, so that no Mac is needed. It answers `CMD error` when the Agents screen isn't showing, as an effect wouldn't play |
| `#fx clear` | Stops all effects, and removes the stand-in |
| `#fx seed N` | Effects use this fixed sequence rather than `hal_random`, so that two runs or two builds draw the same thing. `0` goes back to `hal_random` |
| `#fx step MS` | Moves the effects on by this long, in fixed steps. While `#anim` holds the animations still, effects hold still too, and only this moves them |

### Holding things still for a screenshot

| Command | Effect |
|---|---|
| `#anim SECONDS` | Freezes the animations at this time. `#anim off` lets them run |
| `#pulse 0..1` | Holds the last-minute pulse at this strength. `#pulse off` lets it beat |
| `#burnin X Y` | Holds the AMOLED burn-in shift, in layout units. `#burnin off` lets it drift. It does nothing on an LCD |

### Measuring and checking

| Command | Effect |
|---|---|
| `#telemetry on\|off` | `TELEMETRY {json}` every 5 s |
| `#perf start\|peek\|stop` | Times one action: `start` arms it, the next input starts the clock, `peek` reports as `PERF {json}`, and `stop` reports and disarms |
| `#why N` | For the next N frames that repaint, a `WHY {json}` line with what changed since the frame before |
| `#verify on\|off` | After every frame, repaints everything into a second buffer and compares. A band that differs logs `LOG verify_fail` and counts in `render.verify_fails` |

`#verify` is how the tests check that the renderer's band hashing never misses a change. It costs a full paint per frame, so it's for tests, and frame times mean little while it's on.

## `STATE {json}`

One line, after `d`. It covers every screen and everything a test needs:

| Field | Holds |
|---|---|
| `uptime_ms`, `board` | Time since the start, and the board's name |
| `shape`, `mount`, `base` | `round` or `square`; how far the panel is turned in its case, in degrees; the layout's size in layout units |
| `mode`, `screen` | The mode, and what's actually showing: `keypad`, `menu`, `list`, `card` or the mode |
| `theme`, `time`, `status`, `status_amber`, `hint`, `toast` | What the screen says |
| `task`, `task_id`, `project` | `project` has `id`, `name` and `color` |
| `timer` | `state`, `elapsed_s`, `rid`, and `of`, the board whose run it is (0 for this one's) |
| `pomo` | `state`, `remaining_s`, `set_min`, `total_s`, `fraction`, `rid`, `of` |
| `sync` | The clocks kept in step through the Mac: `pending` (a change made here is on its way), `mine` (what's shown is this board's change), `rev` (the Mac's latest it has) and `seq` (the last `clock` it sent) |
| `alarm`, `pulsing`, `running`, `syncing` | `running` is the key of the clock that runs |
| `daytime` | The time of day as the Clock and the menu show it: `time` ("9:05"), `ampm`, `date` ("7th November 2026"), `day` ("Saturday") and `short` ("Sat 7th Nov"). Empty until a Mac has said the time |
| `split` | Split view: `show` (`agents` or `clock`), `auto` (Auto switch) and `after_s` (how long it waits) |
| `menu` | `open`, and how many `options` it has |
| `settings` | `page` (`main`, `device`, `connect`, `agents`, `clocks`, `macs`, `pair` or `bright`), `scroll`, `key`, `from`, and `rows` (each a `label` and `value`) while Settings is open |
| `bright`, `autoscroll`, `vol`, `autorotate` | Settings |
| `list` | `open`, `kind`, `idx`, `scroll`, `sync`, and the cached `projects` and `tasks` counts |
| `agents`, `n_agents`, `n_needing`, `sel`, `sel_name`, `fling`, `card` | The first 32 agents' `id`, `name` and `st`, how many there are, how many need you, which is chosen and its name, and how fast a flick is still moving |
| `fx` | `particles` alive, the `peak` since the start, the pool's `cap`, how many it `refused` for lack of room, and how many of each of the `kinds` (`dust`, `confetti`, `smoke`, `wisp`, `sparkle`, `z`, `ring`, `hat`). Then `bubble`, the `last` event, `leaving` and `leaving_name` (the agent drawn from its copy), `poofed`, `dropping`, `hopping`, `shaking`, and `squash` (1 at rest). Off the Agents screen, all of it is at rest |
| `rotation` | `orient`, `turning`, `tilt`, `flat`, `wanted`, `face_down` |
| `lock` | `on`, `pad`, `code_set`, `entry_len`, `tries`, `strikes` (waits so far: each doubles the next, up to 15 minutes), `wait_s`, `message` |
| `power` | `percent`, `charging` |
| `link` | `uid`, `connected`, `ready`, `usb`, `pairing`, `pair_left`, `outbox`, `next_id`, `clock_known`, `toggl`, `voice_ok`, `lines_ok`, `lines_bad` |
| `voice` | `idle`, `listening`, `cleaning`, `sending` or `waiting` |
| `has` | The board's features |
| `render` | `frames`, `painted`, `verify_fails` |

## `CAPTURE`

After `c`: a line `CAPTURE w h`, then the screen as a person holding the board sees it, as base64 RGB565 (native byte order, row by row, 768 bytes a line before encoding), then `CAPTURE END`. It's the picture as the panel shows it, turned if the board is turned.

## `LOG`

Lines a test can wait on. The `detail` is never user text.

| Line | When |
|---|---|
| `LOG boot round\|square` | After a start |
| `LOG mode KEY` | A mode opened |
| `LOG menu open` | The menu opened |
| `LOG list projects\|tasks\|closed` | A list opened or closed |
| `LOG list_loaded projects\|tasks` | A list arrived and was cached |
| `LOG feedback KIND sound=0\|1` | Feedback, and whether sound was allowed. `sound=0` while locked or muted |
| `LOG lock locked\|unlocked\|pad\|wrong\|wait\|code_set\|code_removed` | The lock |
| `LOG fx done\|gone\|new\|idle\|working\|failed\|tap` | An effect started, on the agent on screen |
| `LOG voice listening\|sending\|sent\|transcript\|failed` | Voice |
| `LOG saved settings` | Settings written |
| `LOG card open` `LOG pair start` `LOG turn done` `LOG times_up` `LOG needs_you` | Those events |
| `LOG link hello\|closed` | The link |
| `LOG clock sent\|sent changed` | A `clock` went to the Mac: `sent changed` carries a change made here, `sent` only reports what's shown |
| `LOG clock applied\|kept\|taken` | A `clock` from the Mac: the latest change, made elsewhere, now shown; ignored, as a newer change made here is on its way; or this board's own change, confirmed |
| `LOG verify_fail stale_bands=N` | `#verify` found a band that should have been redrawn |

## `TELEMETRY {json}`

Every 5 s while it's on, covering the 5 s before: `uptime_s`, `frames`, `painted`, `frame_ms_avg`, `frame_ms_max`, `record_ms_avg`, `paint_ms_avg`, `present_ms_avg`, `drawn_ms_max`, `bands_avg`, `free_internal_kb`, `min_free_internal_kb`, `free_large_kb`.

`frame_ms_*` count every frame, including the many that have nothing to draw, so `paint_ms_avg` plus `present_ms_avg` is the cost of a frame that drew something, and `drawn_ms_max` is the worst single one. The memory fields come from `hal_memory`, and read 0 on a platform that doesn't define it.

## `PERF {json}`

The answer to `#perf peek` and `#perf stop`, for `tools/perf_test.py`. After `#perf start`, the clock starts at the next input: a tap, a swipe, a hold, a knob step or a button, by key or command, or a `LINK` line. Every frame counts from then on.

| Field | Meaning |
|---|---|
| `started` | An input has started the clock |
| `first_ms` | From the input to the end of the first frame that repainted something, or -1 if none has |
| `settle_ms` | To the end of the last frame that repainted something |
| `quiet_ms` | How long since then: an action has settled once this has grown for a while |
| `frames`, `painted`, `bands` | Frames since the input, those that repainted, and the 24-row bands they repainted |
| `work_ms`, `frame_ms_avg`, `frame_ms_max` | Every frame's time added up, its average, and the slowest |
| `logic_ms_avg`, `logic_ms_max` | The part of a frame before drawing: input, the link, the clocks and the rest |
| `record_ms_avg`, `paint_ms_avg`, `present_ms_avg` | Working out what changed, painting it, and sending it to the panel; the last two over the frames that repainted |
| `gap_ms_max` | The longest time between two frames starting. One is due every 20 ms, so much more means the board was held up |

## `WHY {json}`

The answer to `#why N`: why a frame repainted. The renderer draws every frame twice, first only to hash what each band would get, then to paint the bands whose hash changed. `#why` compares this frame's marks with the last frame's, in drawing order, and lists those that differ:

| Field | Meaning |
|---|---|
| `dirty` | The bands that repaint, as a hex bit mask |
| `marks`, `marks_before` | How many marks this frame and the last one made |
| `changed` | Each mark that differs: `at`, the code address that made it; `y0` and `y1`, its rows; `text`, the start of a text's string; `words`, what it hashed (a shape's coordinates in 1/64 pixels, or an icon's first three parts), and `before`, the same last frame |
| `ref` | The address of `gfx_mark_rows`, to place `at` in the binary: `xtensa-esp32s3-elf-addr2line -e firmware/build-<board>-debug/bento.elf <at>` on a board, or `atos` with the slide from `ref` in the simulator |

A screen that repaints while nothing on it moves shows up here at once. That's how the knob's idle screens were found repainting 33 to 50 times a second: the pill, the mic icon and a Settings row hashed float coordinates whose last bits changed from frame to frame.

The state dump's `fpu` holds the FPU's control register (its rounding), and the step of a frame after which it last changed, if it ever has.

## The link over this interface

`#link usb` carries the link's lines over this interface as `LINK {json}`, in both directions, so a test can play the Mac with no Bluetooth pairing:

```text
> #link usb
< CMD ok link usb
> LINK {"t":"hello","v":3,"host":"MacBook Pro"}
< LINK {"t":"board","v":3,"uid":"9c41f07a","name":"knob-lcd-1.8", ...}
> LINK {"t":"state","v":4,"seq":1,"now":1793826000,"utc":0,"toggl":"ok","voice":true,"n":0}
```

A `LINK` line goes through the same parser as a line from Bluetooth, up to 16 KB. On a board, pace long lines: the USB driver drops whatever arrives while its 4 KB receive buffer is full, and the board empties it once a frame. A line that loses its end leaves the board waiting for it, and it swallows the keys that follow. `tools/device_test.py` sends 256 bytes every 10 ms. The simulator's socket needs no pacing. `#link ble` goes back. `#mac off` still takes the link down in this mode, and after `#mac on` the harness must say hello again, as a Mac would.

## What it won't show

- **A pairing code.** `STATE` has `link.pairing`, and never the code. While a code is on screen, `CAPTURE` draws the screen again with `000 000` in its place, so the capture differs from what the panel shows.
- **A lock code or its hash.** `STATE` has `lock.code_set` and `lock.entry_len`, never the digits, the salt or the hash.
- **Transcripts in logs.** A transcript becomes the task, so it appears in `STATE` as `task`, and no `LOG` line carries it.

## In the simulator only

Lines that start with `%` are for the simulator itself. They never reach the UX, and a board doesn't have them.

| Command | Effect |
|---|---|
| `%panel PATH` | Writes the panel as a PPM file: what a person would see, pairing code included |
| `%pair on\|off` | A new Mac starts pairing, with the preview's sample code |
| `%mac NAME` | Adds a paired Mac |
| `%rounding flip\|steady` | The FPU rounds differently every other frame, as a board's did, or steadily again: idle screens must still not repaint |
| `%tilt DEG` `%flat 0\|1` | Moves the fake motion sensor, the real path through `hal_motion_read` |
| `%agents N` | With `--demo`: how many sample agents there are, up to 512 |
| `%touch down\|move\|up X Y` | A touch, as the mouse gives |
| `%knob N` | A knob step |
| `%window PATH` | With a window: saves the window's view as a PNG, controls and all |
| `%click X Y` | With a window: a mouse press and release at a panel pixel, wherever the tilt shows it |
| `%key K` | With a window: a key, or `right` or `left` for an arrow |
| `%scroll N` | With a window: N lines of scroll wheel |

Each answers `SIM ok ...` or `SIM error`.
