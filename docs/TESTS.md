# Tests

Bentobox has five sets of tests, from fastest to closest to real use, and a performance harness.

| Tests | What they run | Command |
|---|---|---|
| Desktop app | The backend on its own, with fake services | `desktop/.venv/bin/python -m unittest discover -s desktop/tests` |
| Tools | `tools/bento`'s checks, such as the MAC guard, and that the generated fonts and icons match their sources | `tools/.venv/bin/python -m unittest tools/test_bento.py` |
| Simulator | The real UX and link on the Mac, on both board sizes, under AddressSanitizer and UBSan, with no desktop app | `make -C firmware/sim asan && python3 firmware/sim/tests/run_tests.py` |
| Sharp window | The window's picture against the firmware's, screen by screen | `make -C firmware/sim all sharp && python3 firmware/sim/tests/test_sharp.py` |
| Device harness | The real desktop app and the UX together, in the simulator or on a board | `tools/bento test …` |
| Performance | Each user action timed by the firmware, against a baseline | `tools/bento perf …` |

## The simulator suite

`firmware/sim/tests/run_tests.py` runs every case on both boards, round and square, through the [debug interface](DEBUG.md) and the link, playing the Mac itself. `--only clocks,lock` runs a few, `--board knob-lcd-1.8` one board, and `--plain` the build without sanitizers. Captures go to `firmware/sim/out/`. Every case is a method of `Case` with the same name, and the list is `CASES` at the end of the file. A few of them, for the parts that are easiest to get wrong:

| Case | What it checks |
|---|---|
| `clocks` | The clocks kept in step through the Mac (`clock` in the protocol): what this board sends after a change, what it takes from the Mac, which Toggl runs it names with `of`, and a Mac time far in the past or future that must not overflow |
| `clock_edges` | A new connection's count, another board's Pomodoro, this board's own run replaced, a message with no time while a change made here is on its way, a time's-up dismissed elsewhere, a knob turn overtaken by another board's change, and a clock that never shows more than it was set to |
| `dayclock` | The Clock's words, from the same functions the screen draws with, against Python's calendar: 12-hour time, ordinals, noon and midnight, a leap day, time zones either side of UTC |
| `split`, `split_more` | Split view and Auto switch: from the Timer and the Pomodoro, the wait starting again on any input, never over the menu, a list or a check of Toggl, and the settings after a restart |
| `lock` | Codes, the keypad and the wait after wrong codes, which doubles with each wait (`lock.strikes`) up to 15 minutes; the strikes and the wait survive a restart |
| `fuzz_debug_link`, `fuzz_tcp` | Broken, hostile and odd lines for every message the board reads, `clock` included with times at the edges of what JSON can hold, over the debug interface and the real TCP link |
| `persistence` | The uid, the settings, the task and project, and the lock survive a restart |

## The device harness

The rest of this page is about the device harness, `tools/device_test.py`. It drives the board as a person would, through its [debug interface](DEBUG.md), and checks what the board shows and what really reached Toggl.

On a board it needs a debug build, which has the debug interface: `tools/bento flash <board> --debug`. An everyday build has none, so the harness can't reach it. See [Which builds have it](DEBUG.md#which-builds-have-it).

## Running it

```sh
# The simulator, with fake Toggl and speech-to-text. No hardware and no keys.
tools/bento test sim --board knob-lcd-1.8
tools/bento test sim --board amoled-2.16

# The same, in a window you can watch.
tools/bento test sim --board amoled-2.16 --show

# On a board: build, back up if there's no backup yet, flash, then test.
tools/bento test knob-lcd-1.8 --deploy

# The real services, with your keys from the Keychain.
tools/bento test sim --board knob-lcd-1.8 --apis real
tools/bento test knob-lcd-1.8 --apis real

# Over real Bluetooth. --pair runs the pairing first, and you type the code shown on the board.
tools/bento test knob-lcd-1.8 --link ble --pair

# A few cases, in a hurry.
tools/bento test sim --board amoled-2.16 --only T1,L1 --quick
```

`--list` prints every case. Results go to `test-output/<target>-<apis>-<link>/`, which is git-ignored: `report.json`, `device.log`, `desktop.log` and PNG captures. The command exits with 0 only if nothing failed.

## What's real and what isn't

| Part | Real | Stand-in |
|---|---|---|
| Desktop app | Always the real one, in a temporary home | Nothing touches your own config, hooks or launchd agent |
| Toggl and speech-to-text | `--apis real`, with your keys | `--apis fake` (the default): local fake servers and made-up keys |
| The link | `--link ble`, on a paired board | `--link usb` bridges the link's lines over the debug interface, with no pairing. `--link tcp` is the simulator's own |
| Board | A real one | `sim` |

With `--apis real`:

- Set your keys first: `desktop/.venv/bin/python desktop/bentobox.py set-key toggl`, `set-key speech`, then `set-org <id>`.
- It creates Toggl entries named `bentobox-test …`, and deletes them at the end.
- Toggl's free plan allows about 30 requests an hour, so it runs fewer cases. A rate limit skips the rest instead of failing them.
- V1 plays a spoken phrase through the Mac's speakers on a board with a mic.

Keys and lock codes never appear in the output, and transcripts aren't logged.

Flashing is covered by `AGENTS.md`'s rules: `--deploy` checks the connected chip's MAC against `boards.local.md`, and never overwrites a backup.

## The cases

| Case | What it checks |
|---|---|
| **Boot** | |
| B1 | It starts, and knows what the board has |
| B2 | The desktop app connects, and the board learns the clock and its status |
| B3 | The uid and the settings survive a restart |
| B9 | Frame times and memory stay within budget |
| B10 | The heaviest effect stays within the frame budget: confetti and smoke, on the Inverted profile too, and confetti under a Pomodoro's last minute |
| **UX** | |
| U1 | The menu's compartments each open what they say, by touch |
| U2 | A running clock can't switch to the other clock, but can open Agent Zoo |
| U3 | The color profile changes only in Settings, and applies to every screen |
| U4 | The Pomodoro warms up at 10:00, 5:00 and 1:00 |
| U5 | The arc above an agent is relative to this countdown |
| U6 | About shows the battery and Bluetooth, and the battery reports |
| U7 | Rotating the board turns the screen, after a wait, with touch following |
| U8 | The screen never shows stale bands |
| U9 | With no knob, dragging sideways on a clock does what the knob does |
| **Agents** | |
| A1 | An agent's hook events reach the board, one agent showing at a time |
| A2 | An agent that needs you shows on the board |
| A3 | A tap on the character reaches the desktop app |
| A4 | An agent that ends goes away |
| A5 | Hundreds of agents reach the board, and it can move through them |
| **Toggl** | |
| T1 | Starting and pausing makes one entry in Toggl |
| T2 | A start stopped within 10 s leaves nothing in Toggl |
| T3 | The knob moves the start, and sends one small update |
| T4 | Sync from Toggl reloads a list with one request |
| T5 | Picking a past task while tracking switches entries |
| T6 | A swipe down adopts a timer from elsewhere, and notices when it stops |
| T7 | Bento never stops a timer it didn't start |
| T8 | A run with no Mac is logged when the Mac comes back, with its real times |
| T9 | Unsent runs survive a restart |
| T10 | A run Toggl refuses shows "not saved" |
| T11 | After a 429 the app waits, and says so |
| T12 | With Toggl down, the app retries and the board says so |
| T13 | A run from before the board ever knew the time keeps its length |
| P1 | The Pomodoro logs to Toggl, and time's up stops the entry |
| V1 | A spoken task becomes the task, with a project it names |
| **Lock** | |
| L1 | With no code, Lock asks for one on the Mac, and saving one doesn't lock |
| L2 | A locked board keeps its screen, with an icon, until touched |
| L3 | A knob turn shows the keypad, and wrong codes don't unlock |
| L4 | Wrong codes stay locked; the right one unlocks; nothing else opens |
| L5 | A locked board makes no sound, not even the alarm |
| L6 | The lock survives a restart, and a new code from the Mac replaces a forgotten one |
| **Bluetooth** | |
| Z1 | A paired Mac connects over an encrypted link |

Two more checks run across a whole run: `CRASH` fails if the device's log shows a crash, and `LEAK` fails if a key or lock code appears in the logs.

Some cases need a feature, such as a knob or a speaker, or a particular link or API mode. A case that doesn't apply reports `skip` with the reason.

## Performance

`tools/perf_test.py` does the same user actions every time and lets the firmware time each one (`#perf` in [DEBUG.md](DEBUG.md)). An action starts from a screen set up through the debug interface, then happens as a person would do it: a swipe, a tap, a knob step or a quick turn of five, a drag, or a message from the Mac. `tools/bento perf sim --list` lists them: the menu, the Timer, the lists, the Pomodoro, the agents, Settings, the lock's keypad, messages from the Mac, and two seconds of each screen that changes by itself.

```sh
tools/bento perf sim                       # the simulator, both boards
tools/bento perf knob-lcd-1.8              # a board with a debug build
tools/bento perf sim --baseline            # make this run the baseline
tools/bento perf sim --only list --runs 3  # some actions, fewer times
```

Each action runs five times, and the report shows the medians:

| Column | What it is |
|---|---|
| response | From the input to the end of the first frame that shows a change: what you feel as lag |
| settled | To the end of the last frame that changed, once 300 ms pass with nothing new: how long it animates |
| worst | The slowest single frame in that time |
| work | Every frame's time added up: how busy the board was |
| bands, frames | How much of the screen it repainted, and in how many frames |
| stall | The longest time between two frames starting. One is due every 20 ms |

Every run is compared with the target's baseline in `perf/baselines/`. A time that grew by more than 25 % and by more than a few milliseconds is marked ▲ slower, and so is a count that grew by more than 10 %. The run then exits with 1. On a board, a response over 100 ms or a frame over 45 ms is marked `!` whatever the baseline says. Each run adds a line to `perf/history/<target>.jsonl`, so a trend shows over time, and every sample of the last run is in `test-output/perf/`.

The simulator's times are the Mac's: many times faster than a board's, and moved by whatever else the Mac is doing. Compare them only with a baseline from the same Mac. Bands and frames are the firmware's own, and carry over to a board.

On a board, the harness stands in for the Mac over USB. It answers the board's clock changes and acknowledges its Toggl messages, so nothing reaches Toggl or the other boards. It uses the board's own lists and lock code and never sends new ones. At the end it resets the clocks it used, switches back to Bluetooth and restarts the board, which then takes the real Mac's clocks again. `#perf` is part of the debug interface, so a board needs a debug build (`tools/bento flash <board> --debug`) for the run, and an everyday build afterwards.

## The simulator's two kinds of pixels

`bento-sim` and `bento-sim-asan` draw at the board's real resolution, so their pixels are the firmware's. The headless tests use these. `bento-sim-sharp` draws the same UX at twice the resolution, with fonts generated at those sizes, because a Mac screen shows a 360-pixel panel either tiny or soft. `tools/bento sim` and `--show` use it. Its proportions are the real ones, but its frame times and text edges aren't, so check those on `bento-sim` or on a board.
