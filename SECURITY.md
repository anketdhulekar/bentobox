# Security

Bentobox runs on your Mac and talks to your boards. Here's what it trusts, and what it doesn't.

## Your keys

- The Toggl and speech-to-text API keys live in the macOS Keychain, under the service `bentobox`. They're never written to a file, a log or a board.
- The app stores them with the `security` command-line tool, without a per-app access list. Any program running as you can read them through that tool without a prompt, as it can read the rest of your login keychain.

## The local web server

- It listens only on `127.0.0.1`, and only programs on your Mac may use it: the agent hooks and the `bentobox.py` commands.
- A request from any web page is refused: anything with an `Origin` header, `null` included. `/hook` takes only JSON.
- Hook events are checked field by field, cut to size and capped in number, so a bad event can't stop the app.

## Bluetooth

- A board accepts a Mac only after pairing with a 6-digit code shown on its own screen, and only in pair mode (two minutes, started from the board's Settings). After that, the link is encrypted.
- The Mac can't check a board's side of that, so it keeps its own list of trusted boards in `~/.config/bentobox/boards.json`. A board not on the list gets only the time. Nothing it sends is taken: no Toggl entry, no clock change, no voice clip. Add a board with `bentobox.py boards trust`.
- Simulators over local TCP are trusted without the list, and the app refuses a TCP link to another machine unless the config allows it.

## The lock

The lock is a casual one, to keep the board's screen to yourself, not a vault:

- The code is 4 digits. A board stores only a salted PBKDF2 hash of it, but anyone who reads the board's flash can try all 10,000 codes quickly.
- After wrong tries, the board makes you wait, longer each time.
- Don't use a code that matters elsewhere, such as a bank PIN.

## The debug interface

Firmware built for everyday use has no debug interface. A build made for development or the device tests (`tools/bento build --debug`) can be driven over USB, and anyone with a cable can then change its state, so don't use debug builds day to day.

## Reporting a problem

Please open a GitHub issue, or, for anything sensitive, contact the maintainer through GitHub before publishing details.
