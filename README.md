<!-- the lockup: copies of 220 Labs' identity/polyrhythm/lockup*.svg (source: its scripts/build_identity.py) -->
<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/lockup-dark.svg">
    <img alt="polyrhythm" src="assets/lockup.svg" width="276" height="60">
  </picture>
</h1>

<!-- the one-line description, the same as the repo's GitHub description: keep it one line -->
Your Claude bot and your friend's, in one iMessage chat; they can only answer.

**Status: alpha.** The first live round trip between two Macs worked on 2026-10-06, set up
with the one-command install. Group chats and RCS have not been tried live yet. Expect rough
edges, and read the code before you grant it anything.

You and a friend each run polyrhythm on your own Mac, and each of you gets an AI bot of your
own in the iMessage chat you already share. Start a line with `ai:` and your bot answers you in
the chat:

```
you:     ai: what's the capital of Peru
         Lima.
         🎵 polyrhythm · AI
friend:  ai: one word for a Monday
         Survival.
         🎵 polyrhythm · AI
```

The first answer is your bot's; the second, your friend's.

- **One chat.** The 1:1 thread you already have with your friend, or a group chat by its name.
- **`ai:` is the command.** A line that starts with `ai:` or `ai,` goes to the bot of whoever
  typed it. Anything else in the chat is between the two of you, and your friend's lines never
  leave the Messages database on your Mac.
- **Nothing to name, nothing to swap.** No bot names, no keys. Every bot signs with the same
  line, `🎵 polyrhythm · AI`, and never treats a signed line as a command — so the bots cannot
  talk to each other, or to themselves.
- **Answer-only.** By default your bot answers with Claude Code with every tool, MCP server and
  setting off: it talks and does nothing else. You can swap the reply command (Settings, below).

One stdlib Python file (`polyrhythm.py`) and its self-test (`selftest.py`, which the job never
runs), a small C launcher, and a launchd job. No dependencies to install.

## Set up — one command

You need macOS with Messages signed in to iMessage, and [Claude Code](https://claude.com/claude-code)
logged in. Then:

```sh
git clone https://github.com/220labs/polyrhythm.git ~/polyrhythm
sh ~/polyrhythm/install.sh +15550000003
```

Put your friend's phone number, or their Apple ID email, in place of `+15550000003`.

It writes your settings, checks everything, builds the launcher and starts the job. Then macOS
asks for two things, one after the other, and the installer waits for both:

1. **Full Disk Access** (to read the chat). System Settings opens on the list and Finder shows
   `polyrhythm-host`: drag it into the list and switch it on. No restart: the job notices within
   30 seconds.
2. **Messages** (to answer). macOS asks whether `polyrhythm-host` may control Messages: **Allow**.

When it says `ready`, type `ai: hello` in the chat. Your friend does the same on their Mac, with
your phone or email.

**Or let Claude do it:** `cd ~/polyrhythm && claude`, then `/setup`. It asks for your friend's
handle, runs the installer, and talks you through the two clicks.

A group chat instead of your 1:1 thread: add `--group "Its name"` (a group needs three people);
run it again without `--group` to go back to the 1:1 thread. Your bot replies to your friend over
iMessage; add `--rcs` to reply over RCS instead (a phone number only; not yet tried live).

## Who runs what

Each person runs their own copy, on their own Mac, logged in to their own Claude account. By
default your bot answers only you, on your account and your usage; your friend's bot answers
them. Nobody's bot runs on anybody else's login.

## Security model

Read this before you install.

- **What it is granted.** Reading the Messages database needs **Full Disk Access**, and sending
  needs **Automation → Messages**. macOS gives both to the program a launchd job starts. If that
  program were `/usr/bin/python3`, every Python script on your Mac would get both — **never
  grant them to python3, or to Terminal.** The launcher, `polyrhythm-host`, exists so the grant
  names one small binary. It runs exactly one command, `polyrhythm.py run`: the path to this
  folder's `polyrhythm.py` is compiled into it, it takes no arguments, it drops environment
  variables that would steer it, and Python runs isolated (`-I`: no `PYTHONPATH`, no user
  site-packages).
  Full Disk Access is still the whole disk, not just Messages.
- **What that means for this folder.** Anything that can write to this folder can change
  `polyrhythm.py`, and so run code with those grants. Keep the clone somewhere only you write,
  and read `git log -p` before you apply a pull.
- **The reply command inherits the grants.** It runs as a child of the launcher — and so does
  whatever `claude` the job finds first on its `PATH` (`~/.local/bin`, `/opt/homebrew/bin`, …):
  keep those folders yours alone. The default
  (Claude Code, every tool off) can only print text. If you set `POLYRHYTHM_REPLY` to something
  with tools, anyone whose lines your bot answers can steer a program that can read your disk
  and send iMessages as you.
- **Only you command your bot** — by default. With `POLYRHYTHM_PEER_ACCESS=musician` your friend
  can too: then it posts from your Apple ID and spends your Claude usage on their questions.
  Then your friend's `ai:` lines get two answers, one from each bot: their own bot always
  answers them.
  `POLYRHYTHM_PER_HOUR` (default 20, counting every answer attempted, sent or not) and
  `POLYRHYTHM_RUN_CAP` (default 6) are the backstops either way. A command more than 10 minutes
  old when the bot reads it is filed, never answered, and only one pass at a time runs on a data
  folder.
- **The sign-off is a marker, not a password.** It is the same for every bot and everyone in
  the chat sees it, so anyone can copy it — and a copied sign-off only makes that line ignored,
  never obeyed. Who may command your bot is decided by who sent the line (you, by default), not
  by the sign-off.
- **What is safe by construction.** Chat text reaches the reply command only on stdin, never in
  a shell string; it reaches AppleScript only as an argument, never spliced into the script;
  every SQL value is a bound parameter.

Report a vulnerability privately: [SECURITY.md](SECURITY.md).

## What it reads, and what it keeps

- It reads **one chat**: the 1:1 thread with `POLYRHYTHM_PEER` (or the group named
  `POLYRHYTHM_CHAT_NAME`), found by its members. Every query names that chat; no other thread is
  ever fetched. Within it, only messages that came over iMessage or RCS: an SMS sender can be
  faked, so SMS rows are never read. RCS has not had a live send yet.
- By default SQLite returns only **your own** rows in it, and in the 1:1 thread only those that
  contain `ai` followed by `:` or `,` — anywhere in the line, so `Thai, or pizza?` qualifies — or
  the sign-off. Those rows are read into memory; one that does not *start* with `ai:` is dropped
  there and is never logged, filed or shown to a model. Every other row, your friend's included,
  stays in the database.
- It keeps, in this folder (or `POLYRHYTHM_DIR`), readable only by you: `inbox/` (one file per
  command, the command and your bot's reply), `state.json` (where it got to), `log.jsonl` (what it
  did).
- Nothing leaves your Mac but your bot's replies in the chat, and the command text your reply
  command sends to its model (Claude, by default).

## Terms

Run your own bot, on your own Mac, logged in to your own Claude account. Never point it at
someone else's login, and never hand yours to it for someone else. Anthropic's consumer plans
assume ordinary, individual use, and the usage is yours: by default your bot answers only you.
If you let your friend in (`POLYRHYTHM_PEER_ACCESS=musician`) for more than an occasional
question, set `POLYRHYTHM_REPLY` to a command that uses your own API key. The 🎵 sign-off also
tells everyone in the chat they are talking to an AI, which Anthropic's usage policy asks for:
keep it.

## What you need

- macOS (developed on macOS 26; older versions untested) with Messages signed in to iMessage.
- The Command Line Tools: `xcode-select -p` prints a path; if not, `xcode-select --install`.
  They bring `cc` for the launcher and `/usr/bin/python3` (3.7 or later; tested on 3.9 and 3.12).
- Claude Code, logged in (`claude -p "hi"` answers), for the default reply. Each answered
  command is one model call on your account.
- A friend who does the same on their Mac, and the iMessage chat you share.

## Watch it

`tail -f ~/Library/Logs/polyrhythm.log` shows the start-up lines (`ok   chat.db: 1:1 thread …`,
`ok   messages`, `polyrhythm: listening`) and each command. `python3 polyrhythm.py inbox` shows
the last commands it was given.

Missed the Messages prompt: `launchctl kickstart -k gui/$(id -u)/dev.220labs.polyrhythm`
restarts the job, which asks again. Clicked Don't Allow: System Settings → Privacy & Security →
Automation → polyrhythm-host → Messages, on.

## Update

`cd ~/polyrhythm && git fetch && git log -p HEAD..origin/main` shows what changed; then
`git pull && sh install.sh`. This program reads your Messages database, so what changes in it is
your call. Your `.env`, `inbox/`, `state.json` and `log.jsonl` are not in the repo and a pull
never touches them. `install.sh` is safe to re-run: it rebuilds the launcher only if
`polyrhythm-host.c` or this folder's path changed (then both grants again — a rebuild is a new
binary to macOS) and restarts the job so new code takes effect.

## Settings (`.env`)

`install.sh` writes `.env` from `.env.example`; only the first line is required. Change a
setting there, then `sh install.sh` again. Any `POLYRHYTHM_*` environment variable overrides
the same name in `.env` for commands you run by hand; the job ignores them.

| Name | What |
| --- | --- |
| `POLYRHYTHM_PEER` | your friend's handle: a phone with its country code, or for iMessage an Apple ID email (`install.sh <handle>` writes it) |
| `POLYRHYTHM_PEER_SERVICE` | how your bot replies to your friend in the 1:1 thread: `iMessage` (default) or `RCS` (a phone number only; `install.sh <phone> --rcs` writes it). Never guessed from the chat. Lines over either are read; SMS never is |
| `POLYRHYTHM_PEER_ACCESS` | `ignore` (default): your bot answers only you, and your friend's lines never leave the database; `musician`: your friend too. Changing it starts at the chat's end — lines typed before the change are not answered |
| `POLYRHYTHM_MODE` | `dm` (default) or `group` |
| `POLYRHYTHM_CHAT_NAME` | the group's name, in group mode |
| `POLYRHYTHM_REPLY` | any shell command that reads the instruction on stdin and prints the answer; `$POLYRHYTHM_FROM` says who typed it (`me` or `peer`). Empty: Claude Code, tools off, prompt in `reply-prompt.md` |
| `POLYRHYTHM_DIR` | where `inbox/`, `state.json` and `log.jsonl` live; empty: this folder |
| `POLYRHYTHM_PER_HOUR`, `POLYRHYTHM_RUN_CAP` | backstops: 20 answers an hour; 6 replies in a row with no human line stops the bot until one appears |

Try a reply command without sending anything: `python3 polyrhythm.py scan --fixture --reply 'cat'`
runs one pass over a made-up chat and prints what would be sent.

## Without the job

`python3 polyrhythm.py scan` runs one pass from Terminal — but only if Terminal has Full Disk
Access, which is the broad grant this launcher exists to avoid. Prefer the job.

## Remove

`sh install.sh --remove` unloads the job and deletes the launcher and the plist. Then remove
`polyrhythm-host` from Full Disk Access and Automation in System Settings, and delete
`~/Library/Logs/polyrhythm.log` and this folder (your `inbox/`, `state.json` and `log.jsonl` are
in it unless `POLYRHYTHM_DIR` says otherwise).

## License

MIT — see [LICENSE](LICENSE).

A 220 Labs tool
