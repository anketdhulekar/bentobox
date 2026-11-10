# Bento protocol (version 4)

This is the only contract between the desktop app (backend) and a board (frontend). It extends Agent Zoo's protocol version 2. Both sides test against the samples in `samples/`.

## The link

A board is a **Bluetooth LE peripheral** named for its model, such as "Bento Knob" or "Bento Square". The desktop app is the central: it finds boards by their service UUID, connects and stays connected. A board has one Mac connected at a time, and a Mac can be connected to several boards at once: it keeps their clocks in step (see `clock`).

| | UUID | Properties |
|---|---|---|
| Service | `62656e74-6f00-0000-0000-000000000000` ("bento" in ASCII) | advertised |
| RX | `62656e74-6f00-0000-0000-000000000001` | write, write without response; the desktop app sends here |
| TX | `62656e74-6f00-0000-0000-000000000002` | notify; the board sends here |

- **Messages:** Each message is one JSON object on one line ending in `\n`, split into chunks that fit the link. A line is at most 16 KB, and a receiver drops the rest of a longer line. Receivers ignore fields and message types they don't know.
- **Pairing and encryption:** RX needs an encrypted, authenticated link, and the board checks this again before it parses anything. A paired Mac encrypts with its saved keys. A new Mac pairs with LE Secure Connections and passkey entry, with the 6-digit code shown on the board's screen only. A new Mac can pair only in pair mode (Settings › Pair a new Mac, two minutes). A board remembers up to four Macs.
- **One Mac at a time:** While a Mac is connected, the board doesn't advertise.
- **Several boards:** Each connection is its own session, with its own `hello`, `board` and agent list. Only `clock` and the Toggl runs are shared.
- **TCP, for tests:** The same lines can run over TCP. The simulator listens on `127.0.0.1:7777`, and the desktop app connects when its config says `"link": "tcp"`.

## Rules

- **The board is the source of truth for the clock's count.** It keeps counting with no Mac. It never calls Toggl or speech-to-text, and it holds no API keys.
- **The desktop app is the source of truth for Toggl.** It maps the board's runs to Toggl entries, handles retries and rate limits, and contacts Toggl only because of a message from the board. There's no polling.
- **The newest change to the clocks wins.** Every board shows the same Timer and Pomodoro. The Mac keeps the latest `clock`, passes each newer one to every other board, and settles a clash by when each change happened.
- **Nothing is lost when the Mac is away.** The board saves every `toggl` message in flash until it's acknowledged, and sends them in order after each connection. The desktop app must treat a repeated `id` as already handled.

## From the desktop app

### `hello` (first)

```json
{"t":"hello","v":4,"host":"MacBook Pro"}
```

Sent as a write with response, so the first one triggers pairing. The board answers with `board`, or with `busy` if this Mac may not use it now.

### `state` (when one of its fields changes, and at least every 10 s)

```json
{"t":"state","v":4,"seq":42,"now":1793826000,"utc":-28800,"toggl":"ok","voice":true,"n":240}
```

| Field | Meaning |
|---|---|
| `now`, `utc` | The Mac's time in Unix seconds, and its offset from UTC in seconds. The board sets its clock from these. |
| `toggl` | `ok`, `no_key` (not set up), `offline` (can't reach Toggl) or `backoff` (rate limited). |
| `voice` | `true` when a speech-to-text key is set, so the mic button can show. |
| `n` | How many agents the Mac has, at most 512. A board that holds a different number on two `state` messages in a row, with no full list arriving, asks for the list again with `resync`. |

### `agents` (the agents, in pieces)

There can be hundreds of agents, and a Bluetooth link can't carry them all on every change. So the Mac sends the whole list once, in pages, and after that only what changes.

The whole list, after `board` on each connection and after `resync`:

```json
{"t":"agents","page":0,"pages":12,"rows":[
  {"id":"a1b2c3d4","name":"api-refactor-12","proj":"api-server","kind":"i","st":"working","act":"Editing main.c","note":"","age":34,"up":3120,"pane":1,"where":"Rex session “work”","subs":[{"id":"9f8e7d6c","type":"Explore","act":"Reading docs"}]}
]}
```

What changed, at any time after that:

```json
{"t":"agents","rows":[{"id":"a1b2c3d4","name":"api-refactor-12","st":"needs","act":"Waiting for your answer","age":0,"up":3180}],"gone":["77aa01ff"]}
```

| Field | Meaning |
|---|---|
| `page`, `pages` | Only in the whole list. Page 0 begins it, and when the last page has arrived, any agent the list didn't name is gone. Rows take effect as they arrive, so the board fills up as it goes. |
| `rows` | Whole agents, each replacing what the board held under that `id`. An `id` it hasn't seen goes at the end. |
| `gone` | The `id`s of agents that ended. Only in changes. |

A row's fields: `id` is 8 hex digits and picks the costume. `kind` is `i` or `b`. `st` is `working`, `needs`, `done`, `idle` or `failed`. `name` is at most 40 characters and `act` at most 48. `age` and `up` are seconds, as of when the row is sent. `pane` is `1` if a tap can bring the agent's terminal pane to the front, in Rex or Herdr. A board also takes `rex`, its name from Mac apps before Herdr. `where` says where it runs, such as `Rex session “work”` or `Herdr “bentobox / 1”`. `agent` is `"codex"` or `"grok"`, and is missing for Claude. `subs` holds at most 6. Any field that's empty, zero or missing means the same thing, so a sender can leave them out.

Rules for the Mac:

- **Order:** Oldest first, by when the agent started. New agents are always the newest, so a board's order stays the Mac's.
- **Size:** A line is at most 12 KB here, leaving room in the link's 16 KB, so a page holds about 40 agents.
- **Pace:** At most about 8 KB of `agents` lines each second, so that voice and Toggl aren't held up. If more changes than that, a later change to an agent replaces an earlier one still waiting. Agents that need you go first, then changes of `st`, then the rest. `gone` is sent before any row.
- **What counts as a change:** Any field but `age` and `up`, which move by themselves.

A board keeps the first 512 agents and ignores the rest.

### `ack`

```json
{"t":"ack","id":17}
```

The desktop app has taken responsibility for this `toggl` message, and the board can delete it from flash. Send it once the message is saved or handled, not once Toggl has answered.

### `entry_status`

```json
{"t":"entry_status","rid":812,"status":"rejected"}
```

Toggl turned this run down for good, so the board shows "not saved in Toggl". Nothing is sent for success.

### `projects` and `tasks`

The answer to `fetch`. Each is one message per page, to stay under the line limit. The board replaces its cached list when the last page arrives, and stores it in flash.

```json
{"t":"projects","req":5,"page":0,"pages":1,"rows":[{"id":123,"name":"Deep work","color":"#4FB3F6"}]}
{"t":"tasks","req":6,"page":0,"pages":1,"rows":[{"desc":"Write the Q4 plan","task_id":0,"project":{"id":123,"name":"Deep work","color":"#4FB3F6"}}]}
{"t":"fetch_failed","req":6,"what":"tasks"}
```

`projects` is sorted by name, and `tasks` holds the last 20 distinct (description, project) pairs, newest first. `project` is `null` for a task with no project. The app sends both once per `uid` each time it starts, with `req` 0, because that's what fills an empty cache. After that it sends them only for a `fetch`, so the free plan's request limit isn't spent on syncing.

### `sync_result`

The answer to `sync`.

```json
{"t":"sync_result","req":7,"result":"adopted","rid":813,"desc":"From my phone","task_id":0,"project":{"id":123,"name":"Deep work","color":"#4FB3F6"},"start":1793825000}
{"t":"sync_result","req":7,"result":"stopped"}
{"t":"sync_result","req":7,"result":"in_sync"}
{"t":"sync_result","req":7,"result":"failed"}
```

An adopted timer's `rid` is chosen by the app, one above the highest it has seen from this `uid`. The board uses it for that run, and moves its own counter past it. `project` is `null` if the timer has none.

### `transcript`

The answer to a voice clip. `project` is the id of a project named in the text, if any.

```json
{"t":"transcript","vid":3,"text":"Acme design review","project":123}
{"t":"transcript","vid":3,"error":"no_speech"}
{"t":"transcript","vid":3,"error":"failed"}
```

### `lock_code`

```json
{"t":"lock_code","salt":"000102030405060708090a0b0c0d0e0f","iter":2000,"hash":"…"}
{"t":"lock_code","clear":true}
```

Sent to each board once: when the code is set or removed in the desktop app, or at that board's next connection. One code is set for every board. The Mac stores the same salted hash, never the code. `hash` is PBKDF2-HMAC-SHA256 of the 4 digits, 32 bytes, in hex. A new code replaces the old one, also while the board is locked. It doesn't lock or unlock the board, except that removing the code unlocks it. `samples/vectors.json` holds a test value.

### `clock` (the clocks every board shows)

```json
{"t":"clock","rev":42,"from":"9c41f07a","seq":17,"for":17,"now":1793826000250,"at":1793825990000,"task":{"desc":"Write the Q4 plan","task_id":0,"project":{"id":123,"name":"Deep work","color":"#4FB3F6"}},"timer":{"st":"running","acc":0,"start":1793825940000,"rid":812,"of":"9c41f07a"},"pomo":{"st":"idle","set":25,"total":1500000,"left":1500000,"rid":0,"of":"9c41f07a"}}
```

The latest change to the clocks, from whichever board made it: `from` is that board's `uid` and `seq` its number for the change. The Mac sends it:

- after `board`, on each connection, if it has one, and before the lists, so a board's first `clock` answer can't be overtaken by a change it makes after an unasked list load;
- in answer to every `clock` from a board, with that board's `seq` in `for`: either the board's own change, taken, or a newer one that beat it;
- to every other connected board, when it takes a change.

`rev` counts the changes the Mac has taken. Within one connection it only grows, so a board drops a `clock` with a lower `rev` than one it already has. The fields are as in the board's `clock` below, with times in the Mac's Unix milliseconds, and `of` always a `uid`. A board shows what arrives, except while a change of its own is still on its way: then it waits for the Mac's answer to that change, and takes only a `clock` that's newer than it. The Mac's answer to one of the board's earlier changes is never newer, whatever its `at` says: the Mac stamps `now` before the trip, so over Bluetooth an old answer can look newer than a change made since.

## From the board

### `board` (the answer to `hello`)

```json
{"t":"board","v":4,"uid":"9c41f07a","name":"knob-lcd-1.8","fw":"0.1.0","w":360,"h":360,"has":["knob","haptics","mic"],"lock":true}
```

- `uid` is 8 hex digits. A board makes it up at random the first time it starts, and keeps it in flash. `id` and `rid` are only unique within a `uid`, so the desktop app tracks them per `uid`. Erasing a board gives it a new `uid`, so its restarted numbers can't be mistaken for ones already handled, and two boards of the same model can't collide.
- `lock` says whether a lock code is set.
- After `board`, the board sends its saved `toggl` messages in order.

`title`, if present, is the board's own name for people, such as "Bento Knob": the same as its Bluetooth name.

### `busy`

```json
{"t":"busy","reason":"another Mac is chosen"}
```

The board disconnects right after, and the app waits 30 s before it tries again. `reason` can also be `"pairing"`.

### `resync`

```json
{"t":"resync"}
```

The board's list of agents doesn't match `n` in `state`, so it asks for the whole list again. It waits at least 30 s before asking again.

### `tap`

```json
{"t":"tap","id":"a1b2c3d4"}
```

A tap on a character: bring that agent's terminal pane to the front, in Rex or Herdr. With no Herdr window open, the Mac opens Herdr first. Only ids from the app's own list are accepted, and they reach `rex` or `herdr-app` as arguments, never through a shell.

### `toggl`

Every change to a run. `id` counts up and never repeats on a board, and it's what `ack` names. `rid` names one running stretch of a clock, and is the number of the board that started it. `at` and `start` are Unix seconds.

```json
{"t":"toggl","id":17,"op":"start","rid":812,"at":1793826000,"desc":"Write the Q4 plan","task_id":0,"project":123}
{"t":"toggl","id":18,"op":"update","rid":812,"desc":"Acme review","task_id":0,"project":123}
{"t":"toggl","id":19,"op":"shift","rid":812,"start":1793825940}
{"t":"toggl","id":20,"op":"stop","rid":812,"at":1793826600}
{"t":"toggl","id":21,"op":"stop","rid":77,"of":"5be0e1d2","at":1793826900}
```

- **`of`:** In `update`, `shift` and `stop`, the run belongs to the board with this `uid`: another board started it, and this one shows it through `clock`. With no `of`, the run is the sender's own.

- **`start`:** Begin an entry. If the app knows of an earlier run still open, it stops that one where this one starts. A start that arrives late, after a run that began later, is logged as ending where that run began.
- **`update`:** Change the running entry. Only the fields that changed are present. `project` 0 means no project. The app sends the changes as one `PATCH` once they settle (1.5 s).
- **`shift`:** The knob moved the run's start.
- **`stop`:** End the entry. A run that has already stopped stays as it is, so every board showing a run can send its stop. Before stopping, the app checks that the running timer is still this one, because Toggl's stop ends whatever is running. A run shorter than 10 s is deleted instead, as a guard against accidental taps, unless it was adopted through `sync`.

A board with no Mac time since it started holds its messages until `state` arrives, then fills in `at` from how long ago each one happened.

### `fetch`

```json
{"t":"fetch","req":6,"what":"tasks"}
```

"Sync from Toggl" on a list. `what` is `projects` or `tasks`. One Toggl request for that list.

### `sync`

```json
{"t":"sync","req":7,"rid":812}
```

Swipe down on the Timer. `rid` is the board's running stretch, or 0. A timer started elsewhere is adopted, and the knob's own timer stopped elsewhere comes back as `stopped`.

### `clock`

```json
{"t":"clock","seq":17,"changed":true,"now":81234567,"at":81230000,"task":{"desc":"Write the Q4 plan","task_id":0,"project":{"id":123,"name":"Deep work","color":"#4FB3F6"}},"timer":{"st":"running","acc":0,"start":81174567,"rid":812},"pomo":{"st":"idle","set":25,"total":1500000,"left":1500000,"rid":0}}
```

What the board's clocks show. It's sent after `board` on each connection, and after every change made on the board: a tap, a hold, the knob or a drag, a new task or project, or a swipe-down sync. A clock that runs on needs no message.

- **`seq`:** The board's number for this message. It grows by one each time.
- **`changed`:** `true` if the board changed its clocks since the last `clock` the Mac sent it, or since it started. Only a change can win over the Mac's latest. An unchanged board takes the Mac's, and its own is taken only if the Mac has none.
- **`now`, `at`, `start`:** Times in milliseconds by the board's own clock, which needs no Mac time: `now` is when it sent the message, `at` when the clocks last changed, and `start` when the Timer's running stretch began. The Mac reads each one as that long before it got the message.
- **`task`:** The task, its Toggl task id (0 for none) and the project. A project `id` of 0 is "No project", and -1 means none was ever chosen.
- **`timer`:** `st` is `idle`, `running` or `paused`. `acc` is the milliseconds counted before the running stretch, and `start` is only there while it runs.
- **`pomo`:** `st` is `idle`, `running`, `paused` or `done`. `set` is the length in minutes, `total` the length of this countdown in milliseconds, which grows if the knob adds time while it's paused, and `left` what was left at `at`.
- **`rid`, `of`:** The Toggl run of the stretch that runs, or of the last one. `of` is the `uid` of the board that started it, and there's none if it was this board.

A board that shows another board's run sends `toggl` messages with `of` for it: it stops it on a tap or a hold, and changes its task, project or start. Starting again is always a new run of its own. When the Mac's `clock` replaces a run the board had started itself, the board stops that run where the change happened.

### Voice

```json
{"t":"voice_start","vid":3,"rate":16000}
{"t":"audio","vid":3,"seq":0,"p":0,"i":0,"n":1600,"b64":"…"}
{"t":"voice_end","vid":3,"packets":12}
{"t":"voice_cancel","vid":3}
```

The board records first and sends afterwards, as fast as the link allows. Audio is 16 kHz mono, IMA ADPCM (4 bits a sample, low nibble first). Each `audio` packet carries its own decoder state, `p` (predictor) and `i` (step index), so a packet decodes on its own. `n` is its sample count. `samples/vectors.json` holds a test value, and `gen_vectors.py` is the reference.

## Limits and timeouts

- The board shows "Mac offline" when no Mac is connected and it has heard nothing for 30 s.
- The app reconnects with a backoff from 2 s up to 8 s, so a Mac pairing for the first time is caught during pair mode's two minutes, and it looks for boards every 10 s (5 s, with a pause, while one is connected).
- `clock` lines are at most 1 KB: a task is at most 96 bytes and a project name 48.
- The board keeps at most 64 unacknowledged `toggl` messages. When full, it drops the oldest `update`, then the oldest of any kind, and shows "not saved in Toggl".
