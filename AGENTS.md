# AGENTS.md

Guidance for coding agents working on Bento. Read `README.md` first, and `docs/ADDING-A-BOARD.md` before touching board code.

Bento starts fresh. The earlier projects, `toggl-pomodoro-timer` and `agent-zoo`, are references beside this repo. Copy only what the current work needs, and adapt it to Bento's layout. Bring along any lesson or constraint that still applies.

## Hard rules

- **Secrets stay on the Mac.** API keys live in the macOS Keychain, managed by the desktop app. No secret goes in this repo, in firmware or on a board. The one exception is the lock code, which is set on the Mac but must be checked with no Mac connected: a board stores only a salted hash of it, never the code. Never log a lock code.
- **Never read, print or copy `secrets.h` from the earlier projects.** It can hold the owner's Wi-Fi password and API keys. Exclude it from searches (`grep --exclude=secrets.h`).
- **Never log or show secrets or pairing codes.** A real pairing code appears only on the board's screen.
- **Treat voice transcripts as user data.** The mic can pick up conversations in the room.
- **Never deploy without the owner's go-ahead.** That covers flashing any board, installing hooks and loading the launchd agent. A yes covers only that step.
- **Flash each board only with its own firmware.** All the boards are ESP32-S3, so the chip alone proves nothing. Check the MAC with `esptool.py --port … read_mac` against `boards.local.md` (git-ignored). If that file is missing or doesn't list the MAC, ask the owner which board it is.
- **Never overwrite a board's flash backup.** Back up a board before the first flash of new firmware.
- **Keep the agent privacy limits.** Hooks send only short, safe fields: no prompts, commands, file contents, full paths, transcript paths or tool output.
- **Keep the desktop app's local HTTP server on 127.0.0.1,** with its Host and Origin checks.
- **Toggl is contacted only on user actions.** No polling and no automatic syncing: the free plan allows few requests.

## Owner's decisions (2026-11-06)

Don't undo these without asking.

- Swipe up opens a dial with Timer, Pomodoro, Settings, Agent Zoo and Lock. "Timer" is the Tracking mode, which logs to Toggl.
- In Agent Zoo mode, the knob moves between agents. Only one agent is visible at a time, with no neighbors beside it. There are no thumbnails or dock: status dots show the other agents.
- While the Timer or Pomodoro runs, it takes only the top arc of a round screen or the top third of a square one, and the agent gets the rest. It never takes half the screen.
- With a Pomodoro and agents on screen, the arc shows the share of this countdown that's left. It starts full at any length, such as 25 or 50 minutes, and approaches empty as the time reaches zero.
- The clock's section has no gray: only black, or white on the Inverted profile, with the ink and the project's color.
- The Inverted profile applies to every screen. It changes only in Settings. Swiping left or right never changes it, so gestures don't collide between screens.
- Bluetooth and battery sit side by side at the bottom, at six o'clock, on every screen except the dial.
- Lock is on the dial. Only choosing it locks the board: saving a code never does. With no code set, it says to set one on the Mac and doesn't lock.
- The lock code is set in the desktop app, not on the board. A new code from a paired Mac replaces a forgotten one, also while locked.
- A locked board keeps showing its normal screen, with a lock icon beside Bluetooth and battery, and clocks keep running. The first touch or knob turn only brings up the keypad, and the dial and Settings stay closed. With no input for 10 s, the keypad goes away and clears its digits.
- The code is entered by touch on a full keypad on every board. The knob doesn't enter digits.
- While locked, the board makes no sound, not even the time's-up alarm. Vibration stays on.
- The full Pomodoro screen's rim shows the time left out of 60 minutes. Only the arc above an agent is relative to this countdown.
- The Pomodoro warm-up colors the time, the status and the mic, also before it starts: from 10:00 they fade to Leuchtorange (#FF4D06) at 5:00, then to crimson (#E8143A) at 1:00, interpolated in OKLCH so a fade never sweeps through purple. In the last minute the time pulses with a glow.
- The project pill is always visible with a clock: on the Timer, on the Pomodoro and in the clock's section above an agent. Agent Zoo with no clock running doesn't show it.
- Settings follows the zoo's: brightness, which Mac connects, pairing, auto-scroll and similar options.
- On a board with a motion sensor, the whole screen and its touch input rotate when the board rotates. The screen waits until the board has stayed turned for a moment, under a second, then animates the turn.
- While a clock runs, its status line stays empty: the moving time already says it's running.
- Everything runs over Bluetooth, with no Wi-Fi on the boards. The desktop app makes the Toggl and speech-to-text calls.
- The Mac connects to one board at a time, and every board runs the same experience.
- The desktop app is the backend and the board is the frontend, and they talk only through the Bluetooth protocol.
- The UX stays apart from the hardware layer, so it can move to more AMOLED boards.
- The project will keep evolving, so names and structure stay open-ended. The dial's modes are a simple list.

## Words

- **Board** or **device:** the physical hardware.
- **Backend:** the desktop app on the Mac.
- **Frontend:** everything that runs on the board.
- **UX:** the part of the frontend that's the same on every board.
- **Hardware layer:** the part of the frontend that differs by board: `hal/`, `drivers/`, `boards/` and `platform/`.

## Layout rules

The full layout is in `README.md`.

- **`firmware/ux/`** includes only `firmware/hal/`. It must compile on the Mac with no ESP-IDF headers, and it never names a board: it checks features.
- **`firmware/drivers/`** has one folder per chip, and it takes its pins from the board.
- **`firmware/boards/`** has one folder per board, and it holds facts, not behavior.
- **`protocol/`** is the only contract between `desktop/` and `firmware/`. Both sides test against its samples.
- **Burn-in:** fixed elements on AMOLED screens, such as the timer arch, need a small pixel shift.

## Workflow

- **Preview first.** Design changes start in `preview/bento.html`. Get the owner's approval for visible changes before changing firmware, and keep the two in step.
- **Then the firmware,** checked in the simulator, then the desktop app with its tests, then the docs.
- **New behavior gets a test.**
- **Check the preview in a browser:** Playwright with system Chrome works.

## Until the switch-over

The installed hooks and launchd agent run from the old `agent-zoo` clone. Don't install them from here without a go-ahead.
