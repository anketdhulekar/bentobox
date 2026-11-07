# Bento

A small desk companion that keeps growing. It runs a Toggl time tracker, a Pomodoro timer and the Agent Zoo on round and square ESP32-S3 boards, with a Mac app behind them. Like a bento box, it has room for more sections.

## Status

Bento is at the design stage. So far it has only the preview, `preview/bento.html`, which you can open in a browser.

Bento starts fresh. It doesn't contain the two earlier projects, `toggl-pomodoro-timer` and `agent-zoo`. It uses what they taught us and copies only what earns its place. Both stay in use until Bento replaces them, and the installed Agent Zoo hooks and launchd agent still run from the old `agent-zoo` clone.

## What it does

- **Dial:** Swipe up to open a dial with Timer, Pomodoro, Settings, Agent Zoo and Lock. Its options are a simple list, so a new one doesn't need changes to the dial.
- **Agent Zoo:** See your coding agents, one at a time. A row of dots shows every agent's status. The knob moves between them, or you swipe or tap a dot on a board with no knob.
- **Timer and agents together:** While the Timer or Pomodoro runs, it takes only the top arc of a round screen or the top third of a square one, and the agent gets the rest. A Pomodoro's arc starts full at any length and empties as the time runs out.
- **The project stays with the clock:** The project pill shows on the Timer, on the Pomodoro and in the clock's section above an agent. Agent Zoo with no clock running doesn't show it.
- **Status:** Bluetooth and battery sit side by side at the bottom, at six o'clock, on every screen except the dial.
- **Lock:** Only choosing Lock on the dial locks the board. It keeps showing its normal screen with a lock icon, clocks keep running and sounds stay off. A touch or knob turn brings up a keypad, and you enter the 4-digit code by touch. The code is set in the desktop app, which can also replace a forgotten one.
- **Pomodoro warm-up:** From 10:00 the time, the status and the mic fade to orange at 5:00, then to crimson at 1:00. In the last minute the time pulses like a heartbeat.
- **Settings:** Brightness, auto-scroll, color, which Mac connects and pairing. Boards with a speaker add Sound, and boards with a motion sensor add Auto-rotate. The color profile, Mono or Inverted, applies to every screen and changes only here.
- **Auto-rotate:** On a board with a motion sensor, the whole screen and its touch input turn when you rotate the board. The screen waits for the board to settle, then animates the turn, so a brief wobble doesn't move it.
- **Any supported board:** The same experience runs on each one, and the Mac connects to one board at a time.

## How it's split

```text
Desktop app (backend)    Agent tracking, Toggl, speech-to-text, API keys
     │
     │ Bluetooth: the only connection between the two
     │
Board (frontend)
  ├─ UX                  The same on every board: dial, screens, layouts, timer count
  └─ Hardware layer      Each board's chips: screen, touch, knob or buttons, vibration or sound
```

- **Secrets stay on the Mac.** The desktop app keeps the API keys in the macOS Keychain. There's no Wi-Fi password, and a board stores only its Bluetooth pairing keys and a salted hash of its lock code.
- **The timer doesn't need the Mac to count.** While no Mac is connected, the board keeps counting and saves its changes for later.

## Project layout

This is the target layout. Only `preview/` and `docs/` exist so far.

```text
desktop/                 The backend: the Mac app
  agents/                tracks Claude Code, Codex and Grok sessions
  toggl/                 the Toggl client
  speech/                speech-to-text
  link/                  Bluetooth to the board
  tests/
protocol/                The contract: messages and samples that both sides test against
firmware/                The frontend: runs on the board
  ux/
    modes/               one per dial option: timer, pomodoro, settings, zoo, lock
    layouts/             one per screen shape: round, square
    widgets/             the dial, lists, the rim, characters
    assets/              fonts, sprites
  hal/                   the interface between the UX and the hardware
  drivers/               one folder per chip, shared by every board that uses it
  boards/                one folder per board: its screen, features, pins and chips
    _template/
    knob-lcd-1.8/
    amoled-2.16/
  platform/esp32/        Bluetooth, flash storage, clock
  sim/                   runs the UX on the Mac at any board's size and shape
preview/                 the HTML design preview
tools/                   bento build, flash, sim and new-board
docs/
```

[Adding a board](docs/ADDING-A-BOARD.md) explains how this keeps new boards simple.

## Boards

| | Knob | Square board |
|---|---|---|
| Model | ESP32-S3-Knob-Touch-LCD-1.8 | ESP32-S3-Touch-AMOLED-2.16 |
| Screen | 360 × 360 round LCD | 480 × 480 square AMOLED |
| Touch | CST816 | CST9217 |
| Controls | Rotary knob | User, BOOT and PWR buttons |
| Feedback | Vibration motor (DRV2605) | Speaker (ES8311) |
| Microphone | PDM | Dual microphones, not used yet; the schematic will show how they connect |
| Motion sensor | None in its firmware | QMI8658 |
| Battery level | Needs checking; its firmware doesn't read it | AXP2101 |

## What we take from the earlier projects

| Take | From | How |
|---|---|---|
| Characters and their animation | `agent-zoo` | Copy and adapt |
| Agent tracking and hooks | `agent-zoo` | Copy and adapt |
| Bluetooth link and pairing | `agent-zoo` | Copy and extend |
| Square board's chip drivers | `agent-zoo` | Split into `drivers/` and `boards/` |
| Knob's chip drivers | `toggl-pomodoro-timer` | Split into `drivers/` and `boards/` |
| Timer and Pomodoro behavior | `toggl-pomodoro-timer` | Rewrite in `ux/modes/` |
| Redrawing only what changed | `toggl-pomodoro-timer` | Use the approach in one new renderer |
| Toggl rules and limits | `toggl-pomodoro-timer` | Rewrite for the desktop app |
| Lessons about memory and speed | Both | Copy into the docs as they apply |

**Left behind:** Wi-Fi, TLS, `secrets.h`, and the code that calls Toggl and speech-to-text from the board.

## Plan

1. **Preview.** Design every screen on both boards, and get it approved. *(In review.)*
2. **Desktop app.** Bring over agent tracking, add Toggl and speech-to-text with keys in the Keychain, add setting the lock code, and define the protocol.
3. **Firmware foundation.** Build `hal/`, `drivers/`, `boards/` and the simulator, with one new renderer.
4. **Screens.** Build the modes and the dial in `ux/`, checking them in the simulator on both boards' sizes.
5. **On the hardware.** Run both boards over Bluetooth, save unsent Toggl changes to flash, and measure voice upload speed.
6. **Switch over.** With the owner's go-ahead, publish this repo, install the hooks and launchd agent from here, and retire the old repos.
