---
name: setup
description: Set polyrhythm up on this Mac, or update it after a pull — asks for the friend's iMessage handle, runs install.sh, and walks the owner through the two macOS grants until the bot is listening. Use when the user says setup, install, get started, or "update polyrhythm".
---

# Set up polyrhythm

The user is the Mac's owner, in a clone of this repo. Setup is one command,
`sh install.sh <friend's handle>`; your job is to ask the one question it needs, run it, and
stay with the user through the two clicks macOS insists on. Keep every message short.

Never open `.env` with a file tool, and never print a message from the chat: `check` and
install.sh report what matters without either.

## 1. Before anything

- `uname -s` is `Darwin`; otherwise stop: polyrhythm is macOS-only.
- `xcode-select -p` prints a path. If not: ask the user to run `xcode-select --install`,
  finish the installer, and tell you.
- `claude -p "say ok"` answers. The bot replies through Claude Code on this account; if it
  is not logged in, the user runs `claude` once and logs in.

## 2. One question

Run `python3 polyrhythm.py check`. If it prints `ok   config`, the settings exist: this is an
update — ask only the yes below (it restarts the job), then step 3 with no handle.

Otherwise ask, in one message:

- **Your friend's iMessage handle** — a phone with its country code (`+15550000003`) or
  their Apple ID email. Pass it to install.sh quoted, or without spaces. If the user says
  their friend is not on iMessage, add `--rcs` (a phone only): the bot then replies over RCS,
  which has not had a live send yet — say so.
- **Which chat** — your 1:1 thread with them (the default), or a group chat by its name.
- **The yes:** this installs a background job that reads that one chat and sends replies,
  signed `🎵 polyrhythm · AI`, from the user's Apple ID. Installing it needs their yes, and
  their answer to this message is it.

## 3. Install

Run, in the background if you can (it waits up to 10 minutes for the grants):

```
sh install.sh <handle> --wait            # a group: add --group "<name>"; RCS: add --rcs
```

Leave out `<handle>` for an update. If it stops at `not installing`, relay the lines above it
and fix what they say. Then tell the user, as soon as `loaded` shows:

1. **Full Disk Access.** System Settings opens on the list and Finder shows
   `polyrhythm-host`: drag it into the list and switch it on. (Fallback: `+`, ⇧⌘G,
   `~/.local/bin/polyrhythm-host`, Open.) Within 30 seconds the job notices — no restart.
2. **Messages.** macOS then asks whether `polyrhythm-host` may control Messages: **Allow**.

Never suggest granting Full Disk Access to Terminal, python3, or Claude Code: the launcher
exists so that exactly one small binary holds it.

## 4. Done is what the user sees

When install.sh ends, relay its `job:` lines and last line.

- `ready` — ask the user to type `ai: hello` in the chat and say whether an answer came,
  signed. Their eyes are the check; the log is not.
- Anything else — the line names the fix (Automation switch, a wrong handle, no account for
  the service it replies over). Apply it, then
  `sh install.sh` again.

Last, one line: their friend runs the same setup with the user's handle, and from then on each
`ai: …` gets an answer from the bot of whoever typed it. Mention `POLYRHYTHM_PEER_ACCESS=musician`
(the bot answers the friend too, on the user's account) only if the user asks for it.
