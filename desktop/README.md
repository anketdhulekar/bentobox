# Bentobox, the desktop app

Bentobox is the backend behind your Bento boards: one app for any number of them. It runs on your Mac and does everything the board can't do alone:

- **Agents:** it watches your Claude Code, Codex and Grok sessions and shows them on the board. Tapping a character brings its Rex pane to the front.
- **Toggl:** it turns the board's timer into Toggl entries. It contacts Toggl only when the board asks it to, so the free plan's small request limit isn't spent on syncing.
- **Voice:** it takes the clip the board recorded, sends it to speech-to-text, and returns the text.
- **Keys:** your API keys stay in the macOS Keychain and never reach the board.
- **Lock code:** you set the board's 4-digit code here.

The board and the Mac talk only over Bluetooth, using the messages in [`protocol/PROTOCOL.md`](../protocol/PROTOCOL.md).

## Set it up

You need Python 3.10 or newer (`brew install python@3.13`) and the Xcode command line tools (`xcode-select --install`). Run these from the repo root.

1. **Install what it needs.** This creates `desktop/.venv` with `bleak`, the Bluetooth library, and builds `desktop/build/Bentobox.app`:

   ```sh
   python3 desktop/install.py setup
   ```

2. **Create the config:**

   ```sh
   desktop/.venv/bin/python desktop/bentobox.py init
   ```

3. **Save your API keys.** Each command asks for the key and doesn't show what you paste:

   ```sh
   desktop/.venv/bin/python desktop/bentobox.py set-key toggl
   desktop/.venv/bin/python desktop/bentobox.py set-key speech
   ```

4. **Tell it your Toggl organization**, if it can't find it by itself. The id is the first number in `focus.toggl.com/<organization>/workspaces/...`:

   ```sh
   desktop/.venv/bin/python desktop/bentobox.py set-org 12345
   ```

5. **Run it:**

   ```sh
   desktop/.venv/bin/python desktop/bentobox.py run
   ```

6. **Pair the board.** On the board, choose Settings › Pair a new Mac. It shows a 6-digit code on its screen for two minutes. Type that code into the dialog macOS opens. Allow Bluetooth if macOS asks. You do this once for each Mac.

## Everyday commands

Use `desktop/.venv/bin/python desktop/bentobox.py` followed by:

| Command | What it does |
|---|---|
| `run` | Runs the app until you stop it |
| `run --no-display` | Runs only the agent tracking and local HTTP, with no board, Toggl or Keychain |
| `status` | Shows the board, Toggl, voice and your agents |
| `set-key toggl` or `set-key speech` | Saves an API key in the Keychain |
| `key-status` | Says which keys are set, never what they are |
| `set-org [ID]` | Saves the Toggl organization id |
| `lock-code set` | Asks for a new 4-digit code and sends it to the board |
| `lock-code remove` | Removes the code, which also unlocks the board |
| `init` | Creates the config |

## Run it in the background

These commands change your setup, so read what they'll do first. `--dry-run` prints the changes and makes none.

- **Hooks** tell the app about your agents the moment something happens. The app still finds your agents without them, a few seconds later.

  ```sh
  python3 desktop/install.py hooks --dry-run
  python3 desktop/install.py hooks
  ```

  This changes `~/.claude/settings.json` and, if you have them, `~/.codex/hooks.json` and `~/.grok/hooks/bentobox.json`. It backs up each changed file and keeps your other hooks. Afterwards, run `/hooks` once in Codex to trust the new hooks. Grok asks you to trust each folder.

- **launchd** starts the app when you log in and keeps it running. Click **Allow** if macOS asks whether Bentobox may use Bluetooth. The log is `~/Library/Logs/bentobox.log`.

  ```sh
  python3 desktop/install.py launchd --dry-run
  python3 desktop/install.py launchd
  ```

To undo both, run `python3 desktop/install.py uninstall`. Your keys and `~/.config/bentobox` stay. To delete a key, run `security delete-generic-password -s bento -a toggl` (or `-a speech`).


## How it behaves

**Agents**

- The board can show up to 512 agents, the oldest first. If you run more, the newest wait until there's room.
- When the board connects, it gets the whole list in pages, and fills up as they arrive. A list of 500 takes about 20 seconds. Agents that need you go at once, between the pages, so one that's new to the board can show out of order. Other changes wait until the list has finished.
- After that, the board gets only what changed.
- Agent updates use at most about 8 KB a second, so they don't hold up voice or Toggl. If more than that changes, the board gets the newest of each agent's changes. Ended agents go first, then agents that need you, then changes of status, then the rest, and everything gets its turn.
- If the board finds its count doesn't match the Mac's, it asks for the whole list again.

**Toggl**

- Your timer starts in Toggl as soon as the board says so. A timer stopped within 10 seconds is deleted, in case the tap was an accident. A timer adopted from another device is never deleted this way.
- Changes wait 1.5 seconds, then go out together with only what changed, so a fast knob turn sends one update.
- Bentobox never stops a timer it didn't start. If a timer from your phone is running when you start one on the board, the phone's timer keeps running. The board's run waits, and Bento logs it as a finished entry when you stop it. Swiping down instead switches the board to the phone's timer.
- If Toggl can't be reached, the app keeps trying, more slowly each time, up to every 10 minutes. After a "too many requests" answer (429), it asks nothing until the wait is over.
- Everything the app has accepted is saved in `~/.config/bentobox/toggl.json` before the board is told, so it survives a restart.
- Each board is tracked by its `uid`, a random id it keeps in flash. A reflashed board gets a new one, so its restarted numbers aren't mistaken for old ones, and two boards of the same model don't get mixed up.

**Voice**

- If any part of a clip is missing, you get an error, not half a sentence.
- A silent clip isn't sent to the service, because that would cost money for nothing.
- Transcripts are never logged or saved. The mic can pick up conversations in the room.

**Privacy**

- Keys never appear on a command line, in a log, in a config file or in an error message.
- The lock code itself is never stored or logged. Only its salted hash is kept, and that stays out of the logs too.
- Hooks send only short, safe fields: never prompts, commands, file contents, full paths or tool output.
- Local HTTP listens only on `127.0.0.1`, and only programs on this Mac may use it: a request from any web page (anything with an `Origin` header, `null` included) is refused, and `/hook` takes only `application/json`.

## Files

| Where | What |
|---|---|
| `~/.config/bentobox/config.json` | Settings. Nothing secret. |
| `~/.config/bentobox/toggl.json` | Runs still on their way to Toggl |
| `~/.config/bentobox/projects.json` | The last project list, used to help speech-to-text spell project names |
| `~/.config/bentobox/lock.json` | The lock code's salt and hash |
| macOS Keychain, service `bentobox` | The `toggl` and `speech` API keys |

The settings you might change in `config.json`:

- `"link"`: `"ble"` for a real board, or `"tcp"` for the simulator.
- `"display_host"` and `"display_port"`: where the simulator listens, by default `127.0.0.1:7777`. `"display_port"` can be a list, such as `[7777, 7778]`, for several simulators at once: the app connects to each, as it does to every Bluetooth board, and keeps their clocks in step.
- `"http_port"`: the local HTTP port, by default 47824. If you change it, the hooks need `BENTOBOX_PORT` set to the same number.

## When something's wrong

| What you see | What to do |
|---|---|
| `no Bento board found yet` in the log | The board is off, too far away, or connected to another Mac. |
| `no connection`, every time | This Mac may not be paired. Choose Settings › Pair a new Mac on the board. |
| `Bluetooth needs bleak` | Run the app with `desktop/.venv/bin/python`. |
| `protocol error: its answer has no valid uid` | The board runs an old build. It still works, but all such boards count as one, so update its firmware. |
| `status` says Toggl: `no_key` | Set the key with `set-key toggl`. Also check the log: Toggl may have rejected the key, or the app may need `set-org`. |
| `status` says Toggl: `offline` | Toggl can't be reached. Nothing is lost, and the app keeps trying. |
| `status` says Toggl: `backoff` | Toggl asked it to slow down. It tries again when the wait ends. |
| `Port 47824 is in use` | Bentobox is already running. Check with `status`. |

## Work on it

Run the tests from the repo root. They use fake Toggl and speech-to-text servers on your Mac, and they never touch your real keys, hooks or Bluetooth.

```sh
desktop/.venv/bin/python -m unittest discover -s desktop/tests
```

To try the app without a board, run the fake board, which listens as the simulator does. Set `"link": "tcp"` in the config, then run the app:

```sh
python3 desktop/fake_board.py
```

Type a line of JSON and press Enter to send it to the app, as a board would. For example, `{"t":"resync"}` asks for the whole list of agents again. It makes up a new `uid` each time, like a freshly erased board. Use `--uid 9c41f07a` to keep one, or `--old-build` to answer with none.

To test beside a running copy, use a temp `HOME` and your own port in `<temp>/.config/bentobox/config.json`:

```sh
env HOME=<temp> desktop/.venv/bin/python desktop/bentobox.py run --no-display
```

| File | What it does |
|---|---|
| `bentobox.py` | The commands |
| `backend.py` | Routes messages between the board and everything else |
| `agents/` | Agent tracking, Rex, the hook script, and what each connection's board holds of the agents |
| `toggl/` | The Toggl client and the run manager |
| `speech/` | Joins and decodes audio, and calls speech-to-text |
| `link/` | Bluetooth and TCP links, and the message format |
| `secrets_store.py` | The Keychain |
| `lockcode.py` | The lock code's hash and sending it once |
| `localhttp.py` | The server on `127.0.0.1` |
| `install.py`, `macos/` | Setup, hooks, launchd and the app wrapper |
| `fake_board.py` | A stand-in for a board |
