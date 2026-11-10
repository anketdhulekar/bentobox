# Setting up Bentobox on a new Mac, with an agent

These steps are for a coding agent, such as Claude Code or Codex, that someone has asked to set up Bentobox on their Mac. To start, open the agent in this repo and say: "Set up Bentobox on this Mac by following docs/AGENT-SETUP.md."

You can follow them yourself too: every step is a command you can type.

The agent does the commands, and you do four things it must never do for you: type your API keys, type your lock code, type the board's pairing code into macOS, and say yes before each change to your setup.

## Rules for the agent

- **Go one step at a time.** Before each step, say in a sentence what it does. When you're unsure what's left, run `tools/bentobox check`: it lists every step, done or not, with the command for the next one, and it never shows a key or a code.
- **Never handle a secret.** The API keys and the lock code are typed by the person, in their own Terminal window, into commands that don't show what's typed. Never ask for a key or a code in the chat. Never put one on a command line, run a command that would show one, or look in the Keychain or `~/.config/bentobox` for one. If the person pastes a key into the chat anyway, don't use it: say so, and suggest they revoke it and make a new one.
- **Never ask for the pairing code.** It shows only on the board's screen, and the person types it into the dialog macOS opens.
- **Ask first, every time,** before you install software (the Xcode tools, Homebrew, Python), start the background service, install the hooks, or flash a board. A yes covers only the step it was given for. Where a command has `--dry-run`, run that first and show what it would change.
- **Leave what's there alone.** If `tools/bentobox check` finds another copy of Bentobox in launchd, or something else on Bentobox's port, stop and ask the person what they want.
- Read [`AGENTS.md`](../AGENTS.md) too: it has the project's other rules.

## The steps

Run every command from the repo's root folder.

### 1. Check the Mac

```sh
tools/bentobox check
```

On a new Mac, everything is still to do. Bentobox also needs:

- **The Xcode command-line tools:** `xcode-select -p` prints a folder when they're there. If not, ask the person to run `xcode-select --install` and finish the dialog it opens.
- **Python 3.10 or newer** for the app. The system's `python3` is older, which is fine for the scripts but not for the app. If `python3.13 --version` (or 3.12, 3.11, 3.10) finds none, ask before installing one, for example with `brew install python@3.13`. If there's no Homebrew, ask the person to install it from [brew.sh](https://brew.sh), or to install Python from [python.org](https://www.python.org/downloads/macos/).

### 2. Install the app

This makes the app's Python in `desktop/.venv`, with the Bluetooth library, and builds `desktop/build/Bentobox.app`. It changes nothing outside the repo.

```sh
python3 desktop/install.py setup --dry-run
python3 desktop/install.py setup
```

### 3. Create the config

This writes `~/.config/bentobox/config.json` with where it found `claude`, `rex`, `hc` and `herdr-app`. Nothing in it is secret.

```sh
desktop/.venv/bin/python desktop/bentobox.py init
```

### 4. The keys: the person types them

Both keys are optional. Ask which ones the person wants:

- **Toggl**, for logging the Timer and the Pomodoro to [Toggl](https://toggl.com/): an API key from their Toggl account.
- **Speech-to-text**, an OpenAI API key, for saying what you're working on into the board's mic.

Then ask them to open their own Terminal window, not this chat, and run each one from the repo's folder. Each asks for the key and doesn't show what's pasted. Give them the commands with the repo's full path in place of `<repo>`:

```sh
cd <repo>
desktop/.venv/bin/python desktop/bentobox.py set-key toggl
desktop/.venv/bin/python desktop/bentobox.py set-key speech
```

Don't run these yourself, and don't suggest a way to run them through you (such as Claude Code's `!` prefix): what reaches your shell, you can see. When they say they're done, check. This shows only "set" or "not set":

```sh
desktop/.venv/bin/python desktop/bentobox.py key-status
```

Bentobox finds the Toggl organization by itself. Only if Toggl doesn't work later (step 7), ask for its id, which isn't secret: the first number in `focus.toggl.com/<organization>/workspaces/...`. Then run `desktop/.venv/bin/python desktop/bentobox.py set-org <id>`.

### 5. Start Bentobox: ask first

Tell the person that Bentobox will run in the background, start when they log in, and restart if it stops, and ask whether to start it. With a yes:

```sh
tools/bentobox start --dry-run
tools/bentobox start
```

It waits until the app answers. The first time, macOS asks whether Bentobox may use Bluetooth: the person clicks **Allow**. Then:

```sh
tools/bentobox status
```

### 6. Pair a board

A board needs Bento's firmware first. One that's new from Waveshare doesn't have it: see step 10.

1. Ask the person to open **Settings › Connectivity › Pair a new Mac** on the board. It shows a 6-digit code for two minutes.
2. macOS opens a dialog: the person types the code there.
3. List the boards. The new one is waiting to be trusted:

   ```sh
   desktop/.venv/bin/python desktop/bentobox.py boards
   ```

4. Ask the person whether that's their board (its name says Knob or Square). Then trust it. With more than one waiting, name its uid:

   ```sh
   desktop/.venv/bin/python desktop/bentobox.py boards trust
   ```

`tools/bentobox status` should now say the board is connected. Repeat for each board.

### 7. Check that it works

```sh
tools/bentobox status
```

- **Toggl: ok** if the Toggl key is set. `no_key` means it isn't. Anything else: see "If something's wrong" below.
- **Voice: on** if the speech key is set.
- **Agents:** the Claude Code, Codex and Grok sessions running on this Mac, this one included if it's one of them.

### 8. Optional: the hooks, ask first

Without hooks, Bentobox finds agents every few seconds. With them, the board hears the moment something happens. Installing them changes `~/.claude/settings.json` and, if they're there, Codex's and Grok's settings. Each changed file is backed up first, and other hooks stay. Show the dry run, ask, then install:

```sh
python3 desktop/install.py hooks --dry-run
python3 desktop/install.py hooks
```

Afterwards, tell the person to run `/hooks` once in Codex to trust the new hooks. Grok asks them to trust each folder.

### 9. Optional: the lock code, the person types it

The lock keeps the board's screen to yourself. In their own Terminal, the person runs:

```sh
desktop/.venv/bin/python desktop/bentobox.py lock-code set
```

Every board gets it when it next connects. It shouldn't be a PIN they use anywhere else.

### 10. Only if asked: flash a board

Flashing is its own task, and it has its own rules in [`AGENTS.md`](../AGENTS.md): check the board's MAC against `boards.local.md`, back up its flash first, and ask before each flash. See "A board" in the [README](../README.md#first-steps).

### Finish

Run `tools/bentobox check` once more, and tell the person:

- what's set up, and which optional steps they skipped;
- that `tools/bentobox status` shows what Bentobox sees, `tools/bentobox restart` restarts it (after a `git pull`, say), `tools/bentobox stop` stops it until the next login, and `tools/bentobox logs` shows its log;
- that tapping an agent on the board brings its terminal to the front if it runs in Rex or Herdr.

## If something's wrong

| What you see | What to do |
|---|---|
| `tools/bentobox start` says it doesn't answer | Read the log it shows, or `tools/bentobox logs`. The log never holds keys or transcripts. |
| "Something already answers" on Bentobox's port | Another Bentobox runs, maybe in a terminal. Ask the person before stopping it. |
| "the launchd agent runs another copy" | An older clone of the repo is installed. Ask the person, then `tools/bentobox start --replace`. |
| The board never connects | macOS may have denied Bluetooth: System Settings › Privacy & Security › Bluetooth, allow Bentobox, then `tools/bentobox restart`. On the board, Settings › Connectivity shows the Macs it knows. |
| Toggl says `no_key` | The key isn't set: step 4. If it is, the log says why Toggl refused, or the app may need `set-org`. |
| A tap on an agent does nothing | It runs outside Rex and Herdr, or in the background. Installed Rex or Herdr after step 3? Run `desktop/.venv/bin/python desktop/bentobox.py init` and `tools/bentobox restart`. |

The [user guide](USER-GUIDE.md) shows every screen, and [`desktop/README.md`](../desktop/README.md) has more about the app.
