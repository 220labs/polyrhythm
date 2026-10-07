# polyrhythm

Two people's AI bots in one iMessage chat, each answering its own human by default and never
a bot. One stdlib Python file (`polyrhythm.py`) runs on each Mac: it reads a single chat from
the Messages database, passes on only lines that start with `ai:` or `ai,`, and answers with a
tool-less `claude -p` call, signed with the one fixed sign-off `🎵 polyrhythm · AI`. Setup is
one command, `sh install.sh <friend's handle>` (or `/setup` in Claude Code). "Working" =
`python3 polyrhythm.py check` green and a live round trip. The README is the user's
documentation; this file is for coding agents.

Vocabulary (the owners', 2026-10-05): a **mix** is a chat, a **musician** a person in it, an
**instrument** a musician's bot, a **stem** a capability profile (folder, tools, access, master
prompt; several per mix). Only the first three exist in code; stems are TODO.md's design item.

## Session contract

- check: `python3 polyrhythm.py check` — the self-test on a synthetic chat.db. Where a `.env`
  exists it then validates the settings and resolves the chat in the real chat.db, read-only:
  table names, chat and handle rows, counts — never a message. CI runs it on macOS, with the
  launcher build and `sh install.sh --check-only`.
- live: a running bot is whatever its owner last pulled and installed. A change here reaches a
  bot only when its owner pulls and runs `sh install.sh`. Treat `main` as deployed: a merge is
  someone's next restart. **The checkout you are editing is probably not the bot**: the README
  installs into its own clone, and the launcher has that clone's script path compiled in — read
  it out of the binary (`strings ~/.local/bin/polyrhythm-host | grep polyrhythm.py`) before
  concluding anything from a log or a `check` run here. A change to the Python alone needs no
  rebuild and no new grants (the build stamp is `polyrhythm-host.c` plus the script path), so
  deploying it is a pull and `sh install.sh` in that clone (2026-10-06).
- physical: any real send into Messages, and any command that decodes messages from a real
  `~/Library/Messages/chat.db` (`scan`, `run`, `inbox`). Never without the Mac owner's yes in the
  session.
- open-items: `TODO.md`.

## Commands

```bash
python3 polyrhythm.py check                                   # the check
python3 polyrhythm.py scan --dry-run --fixture --reply cat    # every command on the fixture, signed, nothing sent
python3 polyrhythm.py chats | scan | inbox | run              # real chat.db: needs Full Disk Access
python3 polyrhythm.py setup +15550000003 --dry-run            # what install.sh <handle> writes to .env
sh install.sh --check-only                                    # what the installer would build; non-zero if it would refuse
```

## Layout

- `polyrhythm.py` — the whole protocol (decoder, chat resolution, SQL boundary, rules 1–3, caps,
  sends, the `run` loop, the synthetic chat.db that `scan --fixture` also uses). Standalone:
  stdlib only, imports nothing local.
- `selftest.py` — the self-test; it imports `polyrhythm.py`, never the other way. `check` runs
  it as a child Python with its own `-I` and `-W` flags. Kept out so the file a user reads before
  granting Full Disk Access is the program alone; a new promise gets its assert here.
- `polyrhythm-host.c`, `install.sh`, `dev.220labs.polyrhythm.plist` — the launcher, so TCC names
  one binary instead of every python3. `.env.example`, `reply-prompt.md`, `README.md` are what a
  new user reads; `install.sh <handle>` fills `.env` (through a symlink, if it is one).
- `.claude/skills/setup/SKILL.md` — `/setup` for a user's own Claude Code: asks the handle, runs
  install.sh, walks through the two grants. Keep it in step with install.sh's output lines.
- `assets/lockup*.svg` — the README's header, copies of 220 Labs' `identity/polyrhythm/`
  (regenerated there by `scripts/build_identity.py`; copy again, never link across repos).
- **Standalone.** Nothing here imports another repo, and every bot, the first one included, is
  an install of this repo with its own `.env`. No library API is promised: a repo that imports
  this file pins a commit and owns the breakage.
- `.github/main-protection.json` — the branch protection for `main`, applied by the repo owner:
  `gh api -X PUT repos/<owner>/polyrhythm/branches/main/protection --input .github/main-protection.json`.
- `.github/repo-settings.json` — the merge settings (auto-merge on, rebase only), applied the same way:
  `gh api -X PATCH repos/<owner>/polyrhythm --input .github/repo-settings.json`.
- `.github/workflows/automerge.yml` — **an agent's PR merges itself.** A `claude/*` PR from this
  repo, out of draft and without the `hold` label, merges (rebase) as soon as both required
  `check` jobs pass; nobody taps Merge, on either Mac. Draft = still being worked on, so a PR
  stays a draft until the branch is curated (`/finish` readies it). Label `hold` to keep one
  open. The check is the whole gate: a promise without an assert in `selftest.py` is not checked
  before it reaches `main`, and nothing here proves a live send (`physical` above).

## Traps

- **The body is in `attributedBody`, not `text`** (text is NULL on many rows, a bot's own
  included): match with `instr(attributedBody, CAST(? AS BLOB))`.
- **Lines typed on a phone signed in to the same Apple ID are `is_from_me = 1`**: direction is
  no filter.
- **A two-person "group" is the existing 1:1 thread**; a group needs three participants.
- **The dm pre-filter is a substring match**: `Thai, or pizza?` leaves SQLite because it
  contains `ai,`. Rule 2 drops it in Python; never log, file or show a row before rule 2.
- **The friend's rows stay in SQLite while they are ignored** (`POLYRHYTHM_PEER_ACCESS=ignore`,
  the default): every query that decodes rows passes `mine_only`, and the self-test records
  every decode to prove it. A new query does the same. So anything that must be decided about
  one of their rows is decided by its needles in SQL — never by direction (`run_length` counted
  every row of theirs as a human line that way, and an unsigned self-loop ran past `RUN_CAP`),
  and never with rule 1: `instr` can only ask whether the sign-off is *in* the row, so a person
  of theirs quoting it reads as a bot. Fail-closed is the test to apply.
- **A watermark only passes rows the pass fetched.** `ignore` fetches none of the friend's, so
  the stored `rowid` says nothing about theirs: letting them in reads every `ai:` line of theirs
  since. `scan` records the access and, on widening, a separate floor for their rows
  (`peer_from`) — a state file from before that key reads as `ignore`, the default, so an install
  already on `musician` widens once after an update (2026-10-06).
- **Never write a real handle or phone number in git** — tests and docs use +15550000003-style
  fakes; real handles come from each user's gitignored `.env`.
- **The sign-off is a marker, not a secret**, and it is fixed: `SIGNOFF`, the same for every
  bot, so nobody swaps anything at setup. Changing it makes every older install read the new
  one as a person — a breaking change for every pair. Never build authentication on it; who may
  command a bot is who sent the line (`is_from_me`, or the peer's handle). Our own replies are
  told from the friend's bot's by `is_from_me`, not by the sign-off. `note_pin()` (the old
  per-bot notes) is kept, unused, for a PIN when a use shows up.
- **`ai` is a common word**: "AI, honestly, is overhyped" typed by the owner is a command (the
  cost of D1 in docs/simple-setup-2026-10-05.md). Rule 3 therefore strips only `ai:` from an
  answer, never `ai,`: "AI, short for …" is an answer, and rule 1 keeps every signed line from
  being read as a command.
- **Both Macs update together.** An older install knows neither the trigger nor the fixed
  sign-off: a mixed pair can answer each other until the run cap.
- **One bot per Apple ID.** Two Macs on one Apple ID running polyrhythm both answer every `ai:`
  line of their owner's: both see it as `is_from_me`.
- **Never grant the launcher's Full Disk Access to `/usr/bin/python3`.** The launcher has the
  script path compiled in, takes no arguments (it always runs `run`), drops the environment
  variables that steer it (`POLYRHYTHM_*`, `DEVELOPER_DIR`, `SDKROOT`, `TOOLCHAINS`, `PYTHON*`,
  `DYLD_*`) and runs Python with `-I`; keep all four. Anything that starts it gets its grants.
- **A change to `polyrhythm-host.c` or the label is a new identity to macOS**: every user grants
  Full Disk Access and Automation again. Batch such changes.
- **No binaries in the repo** (the compiled launcher lives in `~/.local/bin`).
