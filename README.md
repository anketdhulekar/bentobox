# Bentobox

Bentobox is a Mac app that drives **Bento** boards: small ESP32-S3 desk companions with a round knob or a square AMOLED screen. A Bento shows a Toggl time tracker, a Pomodoro, a clock, and your coding agents (Claude Code, Codex and Grok sessions) as little pixel characters. One Bentobox app runs any number of Bentos at once and keeps their timers in step.

Like a bento box, it has room for more sections.

## What a Bento does

- **Menu:** Swipe up for the Bento box: six compartments with color icons, for Timer, Pomodoro, Agents, Clock, Lock and Settings. Tap one to open it.
- **Timer and Pomodoro:** Track time in Toggl, or count down from 1 to 60 minutes. The project and task come from your Toggl lists, picked with the knob or a tap. From 10 minutes left, the Pomodoro warms from white to orange to crimson, and in the last minute the time pulses.
- **Agents:** See your coding agents one at a time, even hundreds of them. An endless strip of dots shows each one's status, with a count like `12 / 240`. The agent on screen celebrates when it finishes, goes up in a puff of smoke when it ends, and dozes when it's idle. Tap it to jump to its terminal pane.
- **Clock:** The time ("8:02 AM"), the date ("7th November 2026") and the day.
- **Split view:** While a Timer or Pomodoro runs, it can take just the top arc of the screen, with Agents or the Clock below: whichever you opened last. With Auto switch on, the full timer gives way to Split view after a while untouched.
- **Boards in step:** Start, pause or reset a timer on any Bento, and every other Bento shows the same. It stays one Toggl entry.
- **Voice:** Tap the mic and say what you're working on: it becomes the task, and a project you name becomes the project.
- **Lock:** Choose Lock to keep the screen to yourself. The board keeps showing its normal screen, and a 4-digit code, set on the Mac, unlocks it.
- **Settings:** Device (brightness in ten steps, dim enough for a bedroom, plus sound, color, auto-rotate, battery, firmware and MAC address), Connectivity, Agents, and Timer & Pomodoro.

## How it's split

```text
Bentobox (the Mac app)      Agent tracking, Toggl, speech-to-text, API keys
     │
     │ Bluetooth LE: the only connection between the two
     │
Bento boards
  ├─ UX                     The same on every board: screens, layouts, the timer's count
  └─ Hardware layer         Each board's chips: screen, touch, knob or buttons, vibration or sound
```

- **Secrets stay on the Mac.** API keys live in the macOS Keychain. A board has no Wi-Fi and no keys: it stores only its Bluetooth pairing keys and a salted hash of its lock code.
- **A Bento keeps counting without the Mac,** and saves its Toggl changes until the Mac is back.

The protocol between the two is in [protocol/PROTOCOL.md](protocol/PROTOCOL.md).

## What you need

- **A Mac** with Bluetooth, and the Xcode command-line tools: `xcode-select --install`.
- **Python 3.10 or newer** for the app, for example `brew install python@3.13`.
- **A board:** Waveshare's ESP32-S3-Knob-Touch-LCD-1.8 or ESP32-S3-Touch-AMOLED-2.16. You can try everything in the simulator first.
- **To build firmware:** [ESP-IDF 5.5](https://docs.espressif.com/projects/esp-idf/), installed in `~/esp/esp-idf` or wherever `IDF_PATH` points. `idf-env.sh` loads it.
- **To regenerate fonts and icons** (only if you change them): `python3 -m venv tools/.venv && tools/.venv/bin/pip install -r tools/requirements.txt`, and Google Chrome (or set `CHROME` to another Chromium).
- **Optional:**
  - a [Toggl](https://toggl.com/) account with an API key, for time tracking;
  - an OpenAI API key, for speech-to-text;
  - Claude Code, Codex or Grok, for the agents to show;
  - Rex, a terminal app, for jumping to an agent's pane. Without it, agents still show.

## First steps

1. **The simulator.** The real board UX in a Mac window, with sample agents:

   ```sh
   tools/bento sim knob-lcd-1.8 --demo      # or amoled-2.16
   ```

2. **The Mac app.** Install it, set your keys and run it; [desktop/README.md](desktop/README.md) has the details:

   ```sh
   python3 desktop/install.py setup
   desktop/.venv/bin/python desktop/bentobox.py init
   desktop/.venv/bin/python desktop/bentobox.py set-key toggl    # and set-key speech
   desktop/.venv/bin/python desktop/bentobox.py run
   ```

3. **A board.** Every board is an ESP32-S3, so `tools/bento` flashes a board only if its MAC address matches the one listed for it in `boards.local.md` at the repo root (git ignores that file). The first `flash` without an entry prints how to read the MAC and add the line. Then back up the factory firmware once, flash, and pair:

   ```sh
   tools/bento backup knob-lcd-1.8          # once: the whole flash, to backup/
   tools/bento flash knob-lcd-1.8           # or amoled-2.16
   ```

   On the board, open **Settings › Connectivity › Pair a new Mac**, and type the 6-digit code it shows when macOS asks. Then trust it on the Mac: `desktop/.venv/bin/python desktop/bentobox.py boards trust`.

`tools/bento flash --debug` builds firmware with the USB debug interface that the device tests use. A debug build can be unlocked over USB, so don't leave one on a board you use every day.

## Security

The lock keeps out a passer-by; it isn't strong security, so don't reuse a real PIN. [SECURITY.md](SECURITY.md) explains what the app and the boards trust.

## Tests

```sh
desktop/.venv/bin/python -m unittest discover -s desktop/tests    # the Mac app, against fake Toggl and speech services
make -C firmware/sim all asan && python3 firmware/sim/tests/run_tests.py    # the board UX in the simulator, both boards
tools/bento test sim --board knob-lcd-1.8                         # end to end: the real app driving the simulator (--show to watch)
tools/.venv/bin/python -m unittest tools/test_bento.py            # the tools, and the generated assets
tools/bento perf sim                                              # each user action timed, against a baseline
```

The simulator needs ESP-IDF's cJSON: it finds it through `IDF_PATH`.

[docs/TESTS.md](docs/TESTS.md) covers the device tests on real boards, too.

## Project layout

```text
desktop/                 Bentobox, the Mac app
  agents/                tracks Claude Code, Codex and Grok sessions
  toggl/                 the Toggl client
  speech/                speech-to-text
  link/                  Bluetooth and TCP links to the boards
  tests/
protocol/                The contract: messages and samples that both sides test against
firmware/                Bento, the board firmware
  ux/
    modes/               one per menu option: timer, pomodoro, agents (zoo), clock, lock, settings
    layouts/             one per screen shape: round, square
    widgets/             the menu, lists, the rim, characters
    assets/              fonts, sprites, icons (generated)
  link/                  the protocol on the board
  hal/                   the interface between the UX and the hardware
  drivers/               one folder per chip, shared by every board that uses it
  boards/                one folder per board: its screen, features, pins and chips
  platform/esp32/        Bluetooth, flash storage, clock
  sim/                   runs the UX on the Mac at any board's size and shape
preview/                 the HTML design preview
perf/                    performance baselines and history, from tools/bento perf
tools/                   bento build, flash, sim, test and perf; the device and performance harnesses; asset generators
docs/
```

[Adding a board](docs/ADDING-A-BOARD.md) explains how a new board fits in.

## Boards

| | Knob | Square |
|---|---|---|
| Model | ESP32-S3-Knob-Touch-LCD-1.8 | ESP32-S3-Touch-AMOLED-2.16 |
| Screen | 360 × 360 round LCD | 480 × 480 square AMOLED |
| Touch | CST816 | CST9217 |
| Controls | Rotary knob | User, BOOT and PWR buttons |
| Feedback | Vibration motor (DRV2605) | Speaker (ES8311) |
| Microphone | PDM | Two analog mics on an ES7210 |
| Motion sensor | None | QMI8658 |
| Battery level | A voltage divider on GPIO1, which can't tell if it's charging | AXP2101 |

## License

MIT, in [LICENSE](LICENSE). The Manrope font and a few vendor set-up sequences come from others, under their own terms: see [THIRD_PARTY.md](THIRD_PARTY.md).

Bentobox is an independent project. It isn't affiliated with or endorsed by Anthropic, OpenAI, xAI, Toggl or Waveshare. The Claude and Grok characters follow those companies' mascots, which belong to them.
