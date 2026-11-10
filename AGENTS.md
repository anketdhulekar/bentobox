# AGENTS.md

Guidance for coding agents working on Bentobox (the Mac app) and Bento (the board firmware). Read `README.md` first, and `docs/ADDING-A-BOARD.md` before touching board code. If `AGENTS.local.md` exists, read it too: it holds notes for one person's machine and is never committed.

## Hard rules

- **Secrets stay on the Mac.** API keys live in the macOS Keychain, managed by the desktop app. No secret goes in this repo, in firmware or on a board. The one exception is the lock code, which is set on the Mac but must be checked with no Mac connected: a board stores only a salted hash of it, never the code. Never log a lock code.
- **Never log or show secrets or pairing codes.** A real pairing code appears only on the board's screen.
- **Treat voice transcripts as user data.** The mic can pick up conversations in the room.
- **Never deploy without the maintainer's go-ahead.** That covers flashing any board, installing hooks and loading the launchd agent. A yes covers only that step.
- **Flash each board only with its own firmware.** All the boards are ESP32-S3, so the chip alone proves nothing. Check the MAC with `esptool.py --port … read_mac` against `boards.local.md` (git-ignored). If that file is missing or doesn't list the MAC, ask which board it is.
- **Never overwrite a board's flash backup.** Back up a board before the first flash of new firmware, unless the maintainer says not to.
- **Everyday firmware has no debug interface.** Build with `--debug` only for the device tests or debugging, and reflash an everyday build afterwards.
- **Keep the agent privacy limits.** Hooks send only short, safe fields: no prompts, commands, file contents, full paths, transcript paths or tool output.
- **Keep the desktop app's local HTTP server on 127.0.0.1.** It refuses any request with an `Origin` header: only programs on the Mac may use it.
- **Trust only boards on the Mac's list.** An untrusted board gets only `state`, and nothing it sends is taken (`desktop/trust.py`).
- **Toggl is contacted only on user actions.** No polling and no automatic syncing: the free plan allows few requests.

## Design decisions

The maintainer chose these. Don't undo them without asking.

- Swipe up opens the menu: Timer, Pomodoro, Agents, Clock, Lock and Settings. "Timer" is the Tracking mode, which logs to Toggl. "Agents" is the menu's name for Agent Zoo. "Clock" is the time of day.
- The menu is the Bento box: fixed compartments with color icons, opened by a tap. Nothing rotates, and the knob doesn't move through it. On a round screen, six compartments clockwise from the upper left: Timer, Pomodoro, Agents, Clock, Lock, Settings, with Timer and Pomodoro side by side on top and the time in the middle. On a square screen, two by three: Timer and Pomodoro, Agents and Clock, Settings and Lock. (Decided 2026-11-07, adding Clock.)
- The Clock shows the time in 12 hours with no leading zero and a larger AM or PM ("8:02 AM"), then the date as "7th November 2026", then the day. Alone it fills the screen; under a running clock it takes the space below the arc, as the agent does. The menu's middle shows "8:02 AM" over "Sat 7th Nov".
- Split view is a running clock in the top arc with Agents or the Clock below. Which of the two is the last one opened from the menu, and Settings shows and changes the same choice. With Auto switch on, the Timer or Pomodoro screen left untouched for the wait (5 s to 5 min, 15 s by default) moves to Split view by itself; with it off, that screen stays until you leave it. Each board keeps its own choice.
- In Agent Zoo mode, the knob moves between agents. Only one agent is visible at a time, with no neighbors beside it. There are no thumbnails or dock: status dots show the other agents.
- Bento must handle hundreds of agents. The dots are an endless strip: the chosen dot stays in the middle, the strip slides under it and wraps from the last agent to the first. A small "x / y" count sits over the chosen dot.
- While the Timer or Pomodoro runs, it takes only the top arc of a round screen or the top of a square one, a little over a third, and the agent gets the rest, drawn smaller. It never takes half the screen. On every board, that arc is as wide as the full rim.
- With a Pomodoro and agents on screen, the arc shows the share of this countdown that's left. It starts full at any length, such as 25 or 50 minutes, and approaches empty as the time reaches zero. The part already used stays as a faint shade of the rim, as on the full Pomodoro screen, not a line.
- The clock's section has no gray: only black, or white on the Inverted profile, with the ink and the project's color.
- The Inverted profile applies to every screen. It changes only in Settings. Swiping left or right never changes it, so gestures don't collide between screens.
- Bluetooth and battery aren't on the main screens, on any board. The battery is in Settings › Device, and the Mac that's connected in Settings › Connectivity.
- Lock is on the menu. Only choosing it locks the board: saving a code never does. With no code set, it says to set one on the Mac and doesn't lock.
- The lock code is set in the desktop app, not on the board, once for every board: each board gets it when it next connects. A new code from a paired Mac replaces a forgotten one, also while locked.
- A locked board keeps showing its normal screen, with a lock icon at twelve o'clock, and clocks keep running. On a rim it has no pill: it's black on the solid part and white on the shaded part. The keypad shows no lock icon. The first touch or knob turn only brings up the keypad, and the menu and Settings stay closed. With no input for 10 s, the keypad goes away and clears its digits.
- The code is entered by touch on a full keypad on every board. The knob doesn't enter digits.
- The rim and the arc are continuous, with no notches.
- Pressing on a clock shows a thin ring filling from twelve o'clock, as the earlier timer did, and holding until it closes resets the clock. With nothing to reset, no ring shows.
- On a board with no knob, dragging sideways on a clock does what turning the knob does, such as setting the Pomodoro's length.
- While locked, the board makes no sound, not even the time's-up alarm. Vibration stays on.
- The full Pomodoro screen's rim shows the time left out of 60 minutes. Only the arc above an agent is relative to this countdown.
- The Pomodoro warm-up colors the time, the status and the mic, also before it starts: from 10:00 they fade to Leuchtorange (#FF4D06) at 5:00, then to crimson (#E8143A) at 1:00, interpolated in OKLCH so a fade never sweeps through purple. In the last minute the time pulses with a glow.
- The project pill is always visible with a clock: on the Timer, on the Pomodoro and in the clock's section above an agent. Agent Zoo with no clock running doesn't show it.
- Effects, such as confetti, a poof, a drop or Z's, play only for the agent on screen, on the Agents screen with no menu, list or details card open. Nothing runs for the others: the dots show them, and switching to an agent doesn't replay what happened to it. Under a running clock, effects scale with the smaller character and stay out of the clock's section. An agent that ends leaves the list at once and is drawn from a copy until its effect ends. A locked board plays no sounds for them, but vibration stays.
- Brightness has ten levels, dim enough for a dark bedroom. Its page has ten bars, set by the knob or a tap, and it never wraps round.
- Settings is in groups, with nothing loose: Device (Brightness, Sound, Color, Auto-rotate, Battery, Firmware, MAC address), Connectivity (Connected to, which opens the Mac list; Pair a new Mac), Agents (Auto-scroll), and Timer & Pomodoro (Split view, Auto switch, and Switch after only while Auto switch is on). A board shows only the rows it has what they need for.
- A list shows at most five rows on any screen. On a square screen the rows keep a readable size and a longer list scrolls under the finger, with a thin bar to say where; at its top, a pull down still goes back.
- On a board with a motion sensor, the whole screen and its touch input rotate when the board rotates. The screen waits until the board has stayed turned for a moment, under a second, then animates the turn.
- While a clock runs, its status line stays empty: the moving time already says it's running.
- Everything runs over Bluetooth, with no Wi-Fi on the boards. The desktop app makes the Toggl and speech-to-text calls.
- The Mac connects to every paired board at once, and every board runs the same experience. (Decided 2026-11-07, replacing "one board at a time".)
- The boards' clocks are kept in step through the Mac: the Timer, the Pomodoro, the task and the project are the same on every board, started, paused, reset or changed from any of them. The newest change wins, also after a board was away. It stays one Toggl entry. Brightness and the other settings, the agent on screen, the menus and the lock stay per board.
- The desktop app is the backend and the board is the frontend, and they talk only through the Bluetooth protocol.
- The UX stays apart from the hardware layer, so it can move to more AMOLED boards.
- The project will keep evolving, so names and structure stay open-ended.

## Words

- **Bentobox:** the desktop app on the Mac, and the project as a whole. One Bentobox runs any number of boards.
- **Bento:** a board, and its firmware. Boards keep the name: "Bento Knob", "Bento Square", the Bluetooth service and the board's storage are all "bento".
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

- **Setting up a Mac:** follow `docs/AGENT-SETUP.md`. The person types every key and code themselves, in their own Terminal.
- **Run the Mac app with `tools/bentobox`,** the script people use too: `check`, `start`, `stop`, `restart`, `status`, `logs`. `start` loads the launchd agent, so it needs the go-ahead.
- **Preview first.** Design changes start in `preview/bento.html`. Get the maintainer's approval for visible changes before changing firmware, and keep the two in step.
- **Then the firmware,** checked in the simulator, then the desktop app with its tests, then the docs.
- **New behavior gets a test.**
- **Watch performance.** After a change to drawing, input or the link, run `tools/bento perf sim` and compare with the baseline. A slower action needs a reason or a fix. Update a baseline (`--baseline`) only when the maintainer agrees that the new numbers are the expected ones.
- **Check the preview in a browser:** Playwright with system Chrome works.
