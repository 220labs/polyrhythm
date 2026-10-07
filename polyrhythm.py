#!/usr/bin/env python3
"""polyrhythm.py — your AI bot in the iMessage chat you share with a friend.

Two people each run polyrhythm on their own Mac; each gets a bot of their own in the chat. A
line that starts with `ai:` or `ai,` is a command. By default your bot answers only your lines
(your friend's bot answers theirs), signed with the one sign-off every bot uses,
`🎵 polyrhythm · AI`. A bot never answers a bot. This one file is the whole program (`run`,
under launchd through polyrhythm-host); `scan()` also works as a library with hooks of your
own. stdlib only, Python 3.7+ (macOS's /usr/bin/python3 is 3.9).

The rules, in the order they are applied:

  - **One chat is the privacy boundary, in SQL.** Every query names one chat, resolved from
    chat.db by its members (and its name, for a group) — never typed, never predicted. Rows of
    any other thread are never fetched, so they are never decoded, logged or shown to a model.
    While the friend's lines are ignored (the default) only your own rows leave SQLite, and in
    `dm` mode (the existing 1:1 thread) only those that contain `ai` followed by ":" or ","
    (or the sign-off, for the read-back). `check` proves both on a synthetic chat.db.
  - **Rule 1 — a row ending in the sign-off is never a command**, whoever typed it. It also
    counts without its leading emoji (chat.db has dropped it before).
  - **Rule 2 — a command starts with the trigger**: `^ai[ \\t]?[:,]` on the first line, typed by
    you — or by your friend, with `POLYRHYTHM_PEER_ACCESS=musician`. Anything else is between
    the humans: not logged, not filed.
  - **Rule 3 — a bot never emits a line starting with `ai:`**, and always signs. Code enforces
    both before a send, not the prompt.
  - Backstops: `per_hour` reply-hook calls an hour (the rest filed, one "slowing down" line);
    `run_cap` replies in a row with no plain human line between them stops the bot until one
    appears; the reply hook gets 60 s and one attempt; a command older than 10 minutes is filed,
    not answered; one pass at a time per data folder.

There is no key and no name: a sender handle cannot tell a human from a bot — each bot sends
from its human's own account — so the sign-off is the only distinguisher, and a forged one only
earns silence. It is the same for every bot and never changes, so two friends swap nothing to
set up: every polyrhythm bot knows every other. It is a marker, not a secret.

Vocabulary, for what comes next: a mix is a chat, a musician a person in it, an instrument a
musician's bot. Stems — capability profiles (folder, tools, access, master prompt), several
per mix — are not built yet (TODO.md).

Reading chat.db needs Full Disk Access for the process launchd starts (polyrhythm-host); sending
needs Automation -> Messages for the same. README.md says why the launcher exists.

Commands (settings come from .env next to this file; `check` needs none):
  setup HANDLE [--group NAME] point .env at the chat with HANDLE (install.sh runs it);
                              --service RCS replies to HANDLE over RCS, not iMessage
  check                       the self-test on a synthetic chat.db, then .env and chat.db if present
  chats                       candidate chats, as names and counts — never a message
  scan [--dry-run]            one pass; --fixture/--reply/--from-rowid for trying it offline
  inbox [n]                   the last n commands this bot was given
  send "text" [--dry-run]     one signed line into the chat
  run                         the launchd loop: a pass every 30 s
"""
import argparse
import atexit
import contextlib
import datetime as dt
import errno
import fcntl
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from pathlib import Path

BUNDLE = Path(__file__).resolve().parent
CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = 978307200
DM, GROUP = 45, 43                  # chat.style: 45 a 1:1 thread, 43 a group
# One word starts every command and no bot is named: nothing to choose or swap at setup. `ai`,
# not a vendor's name — the reply command is anyone's model.
TRIGGER = "ai"
SEPS = ":,"                         # what may follow the trigger, in the regexes and the needles
# Every bot's last line, the same for all and fixed for good: it tells a bot from a person, and
# says the line is an AI's. Its emoji-less tail, `polyrhythm · AI`, is still one nobody types.
SIGNOFF = "🎵 polyrhythm · AI"
ACCESS = ("ignore", "musician")     # the friend's lines: dropped in SQL (default), or commands too
LATER = ("instrument", "both")      # the friend's bot as a commander: designed, not built (TODO.md)
MAX_REPLY = 600                     # characters of the answer, before the sign-off
REPLY_TIMEOUT = 60                  # one attempt; "no answer" is filed, not sent
SEND_WAIT = 15                      # seconds the read-back waits for is_sent = 1
MAX_AGE = 600                       # seconds: an older command is filed, not answered
PASS_EVERY = 30                     # `run`'s cadence, in seconds
OBJ = "￼"                      # the object-replacement character chat.db puts for an attachment

# Every column this file reads. `check` holds a real chat.db to it (names only, never rows),
# and the synthetic one below is built from it — so a macOS that renames one fails loudly.
COLUMNS = {
    "chat": ("ROWID", "guid", "style", "chat_identifier", "display_name", "service_name"),
    "message": ("ROWID", "date", "is_from_me", "text", "attributedBody", "handle_id", "is_sent",
                "is_finished", "error", "item_type", "associated_message_type", "service"),
    "handle": ("ROWID", "id"),
    "chat_handle_join": ("chat_id", "handle_id"),
    "chat_message_join": ("chat_id", "message_id"),
}


# ---------------------------------------------------------------- reading a message

def body_of(text, blob):
    """The message text. Newer macOS leaves `text` NULL and keeps the body in
    `attributedBody`, an NSAttributedString typedstream: after the NSString class name comes
    metadata up to a "+", then a length and the UTF-8 bytes. The length is one byte under 128;
    0x81 escapes to a 2-byte and 0x82 to a 4-byte little-endian integer (typedstream's int16 /
    int32). Reading 0x81 as a 1-byte length, as some decoders do, mis-reads every message of
    128 bytes or more; this one has read long Cyrillic messages in daily use since 2026-09-23.
    Kept in this file so it stands alone."""
    if text:
        return text
    if not blob:
        return None
    i = blob.find(b"NSString")
    if i < 0:
        return None
    i = blob.find(b"+", i + len(b"NSString"))
    if i < 0 or i + 1 >= len(blob):
        return None
    i += 1
    n = blob[i]
    i += 1
    if n == 0x81:
        n, i = int.from_bytes(blob[i:i + 2], "little"), i + 2
    elif n == 0x82:
        n, i = int.from_bytes(blob[i:i + 4], "little"), i + 4
    if i + n > len(blob):
        return None
    return blob[i:i + n].decode("utf-8", "replace")


_SAME = str.maketrans({"•": "·", "∙": "·", "⋅": "·"})   # NFKC covers NBSP, not these dots


def normalise(text):
    """What two copies of one line keep in common: NFKC (an NFD name composes), no variation
    selector (VS15/VS16 after ♯ or ♭), no invisible format character (a trailing zero-width
    space), one kind of middle dot, single spaces. Newlines stay."""
    t = unicodedata.normalize("NFKC", text or "").translate(_SAME)
    t = "".join(ch for ch in t if ch not in "\ufe0e\ufe0f" and unicodedata.category(ch) != "Cf")
    return re.sub(r"[ \t]+", " ", t)


def expand(signoffs):
    """Each sign-off, normalised, and each again without a leading emoji word — 🎵 is outside
    the BMP and a copy that lost it must still read as a bot's."""
    sigs = []
    for s in signoffs or []:
        # both forms: is_bot compares normalised text, but the SQL needles are byte-exact and
        # must match the sign-off as it was sent (the raw .env value)
        for form in ((s or "").strip(), normalise(s).strip()):
            if form and form not in sigs:
                sigs.append(form)
    bare = [p[1] for p in (s.split(" ", 1) for s in sigs)
            if len(p) == 2 and not any(ch.isalnum() for ch in p[0])]
    return sigs + [b for b in bare if b not in sigs]


SIGNOFFS = expand([SIGNOFF])        # expanded once: every pass asks this of every row it reads


def is_bot(text, signoffs=None):
    """Rule 1: the row ends in the sign-off. An empty list matches nothing; the default list is
    the one expanded at import."""
    t = normalise(text).rstrip()
    return any(t.endswith(s) for s in (SIGNOFFS if signoffs is None else expand(signoffs)))


def trigger_re(seps, tail, optional=False):
    """The trigger at the start of a line, one of the characters of `seps` after it (at most one
    space or tab between), then `tail`. All three shapes come from here, and the separators are
    escaped, so a change to SEPS reaches every shape and can break none of them. ASCII-only
    (re.A), as the SQL needles are: a Turkish dotless ı must not pass rule 2 unseen by
    SQLite."""
    cls = "[" + re.escape(seps) + "]" + ("?" if optional else "")
    return re.compile(r"^[ \t]*" + re.escape(TRIGGER) + r"[ \t]?" + cls + tail, re.I | re.S | re.A)


# The trigger and its ":" or "," on the first line; the command may run on over more lines.
COMMAND = trigger_re(SEPS, r"\s*(\S.*)")
# What rule 3 strips from an answer: the trigger with its colon. Not the comma: "AI, short for
# artificial intelligence, …" is an answer, and every bot already ignores a signed line (rule 1).
STRIP = trigger_re(":", r"\s*(\S.*)")
# Nothing but the trigger, separator or not: no answer at all ("AI" alone is one, see tidy()).
BARE = trigger_re(SEPS, r"\s*$", optional=True)


def parse_command(text):
    """Rule 2: the command when the text opens with the trigger and ":" or ","; else None."""
    m = COMMAND.match(text or "")
    return m.group(1).strip() if m else None


def variants(word):
    """The case variants the SQL looks for in the blob (instr is byte-exact, and LIKE ignores
    case only for ASCII). All four for the two-letter trigger, so the pre-filter lets out every
    line rule 2 takes."""
    out = []
    for v in (word, word.lower(), word.upper(), word[:1].upper() + word[1:].lower(),
              word[:1].lower() + word[1:].upper()):
        if v not in out:
            out.append(v)
    return out


GAPS = ("", " ", "\t")                 # what the regex allows between the trigger and its separator


def trigger_needles():
    """What a command must contain for the dm pre-filter to let it out of SQLite: the trigger,
    then one of SEPS, with the same gaps the regex allows — every case variant. The tab is in
    here because `ai\t: x` is a command: a needle short of the regex means such a line never
    leaves SQLite in dm mode while group mode, which has no pre-filter, answers it."""
    return [v + gap + c for v in variants(TRIGGER) for c in SEPS for gap in GAPS]


# ---------------------------------------------------------------- chat.db

class ConfigError(ValueError):
    """A missing or bad setting: reported as one line pointing at the README, never a traceback."""


class Unresolved(RuntimeError):
    """No chat, or more than one, matches: the scan refuses and lists what it saw."""


def open_db(path=None):
    return sqlite3.connect(f"file:{path or CHAT_DB}?mode=ro", uri=True, timeout=10)


def schema_problems(db):
    """Columns this file reads that chat.db lacks — names only, no row is read."""
    missing = []
    for table, cols in COLUMNS.items():
        have = {r[1] for r in db.execute(f"select * from pragma_table_info('{table}')")}
        missing += [f"{table}.{c}" for c in cols if c not in have]
    return missing


def candidates(db, handle, mode):
    """Chats with `handle` as a member: style 45 in dm mode (and no one else in it), style 43
    in group mode. Names and counts only."""
    rows = db.execute(
        """select c.ROWID, c.guid, c.chat_identifier, c.display_name, c.service_name,
                  (select count(distinct lower(h2.id)) from chat_handle_join x
                     join handle h2 on h2.ROWID = x.handle_id where x.chat_id = c.ROWID),
                  (select count(*) from chat_message_join y where y.chat_id = c.ROWID)
           from chat c
           where c.style = ? and exists (
               select 1 from chat_handle_join ch join handle h on h.ROWID = ch.handle_id
               where ch.chat_id = c.ROWID and lower(h.id) = lower(?))
           order by c.ROWID""", (DM if mode == "dm" else GROUP, handle)).fetchall()
    out = [{"rowid": r[0], "guid": r[1], "identifier": r[2], "name": r[3] or "", "service": r[4] or "",
            "members": r[5], "rows": r[6], "mode": mode} for r in rows]
    return [c for c in out if mode != "dm" or c["members"] == 1]


def describe(chat):
    """One line about a chat for a log — never its guid: a 1:1 guid carries the handle."""
    if chat["mode"] == "dm":
        return f"1:1 thread (chat {chat['rowid']}, {chat['rows']} rows)"
    return f"\"{chat['name']}\" (chat {chat['rowid']}, {chat['members']} other members, {chat['rows']} rows)"


def resolve_chat(db, handle, name, mode):
    """The one chat the link reads, from chat.db: in dm mode the 1:1 thread with `handle`, in
    group mode the group named `name` that `handle` is in. Exactly one, or Unresolved with the
    list — Messages may re-key a chat when members change, so this runs every pass."""
    if not handle:
        raise Unresolved("no peer handle configured")
    found = candidates(db, handle, mode)
    if mode == "group":
        found = [c for c in found if c["name"] == name]
    if len(found) == 1:
        return found[0]
    seen = "\n".join("  " + describe(c) for c in candidates(db, handle, mode)) or "  (none)"
    want = "the 1:1 thread" if mode == "dm" else f"a group named {name!r}"
    raise Unresolved(f"{len(found)} chats match {want} with the peer; candidates:\n{seen}")


def _like(s):
    return "%" + s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _contains_any(needles):
    """SQL and arguments: the row's text or its attributedBody contains one of `needles`.
    coalesce, because a NULL text would otherwise make NOT(...) NULL and lose the row."""
    parts, args = [], []
    for n in needles:
        parts.append("coalesce(m.text, '') like ? escape '\\' "
                     "or coalesce(instr(m.attributedBody, ?), 0) > 0")
        args += [_like(n), n.encode("utf-8")]
    return "(" + " or ".join(parts) + ")", args


# The services a row may come over, and the peer's send may go by (POLYRHYTHM_PEER_SERVICE). RCS
# is in (decided 2026-10-05); SMS stays out — its sender id is forgeable. Each name is an
# enumerator of Messages' `service type` (Messages.sdef on macOS 26.3.1 lists iMessage, SMS, RCS);
# a service added here needs its own row in the self-test and a live send.
SERVICES = ("iMessage", "RCS")
NORMAL = ("m.associated_message_type = 0 and m.item_type = 0 and m.service in ("
          + ", ".join(f"'{x}'" for x in SERVICES) + ")")


def select(db, guid, after_rowid, mode, needles=(), mine_only=False):
    """New rows of ONE chat, oldest first: {rowid, at, from_me, handle, text, sent, finished,
    error}. Never a tapback (associated_message_type 2000-2006), a group event (item_type != 0)
    or a row over a service outside SERVICES (an SMS sender id is forgeable). `mine_only`: rows
    this Apple ID sent, nobody else's. In dm mode only rows containing one of `needles` leave
    SQLite; the rest are never decoded."""
    q = f"""select m.ROWID, m.date, m.is_from_me, m.text, m.attributedBody, h.id,
                   m.is_sent, m.is_finished, m.error
            from chat c join chat_message_join cm on cm.chat_id = c.ROWID
            join message m on m.ROWID = cm.message_id
            left join handle h on h.ROWID = m.handle_id
            where c.guid = ? and m.ROWID > ? and {NORMAL}"""
    args = [guid, int(after_rowid)]
    if mine_only:
        q += " and m.is_from_me = 1"
    if mode == "dm":
        if not needles:
            raise ValueError("dm mode needs a pre-filter")
        sql, more = _contains_any(needles)
        q += " and " + sql
        args += more
    out = []
    for rowid, date, me, text, blob, handle, sent, finished, error in db.execute(
            q + " order by m.ROWID", args):
        t = (body_of(text, blob) or "").replace(OBJ, "").strip()
        if not t:
            continue
        secs = date / 1e9 if date and date > 1e11 else (date or 0)    # ns since 2001 on new macOS
        out.append({"rowid": rowid, "from_me": bool(me), "handle": handle or "", "text": t,
                    "sent": bool(sent), "finished": bool(finished), "error": error or 0,
                    "at": dt.datetime.fromtimestamp(secs + APPLE_EPOCH), "ts": secs + APPLE_EPOCH})
    return out


def newest(db, guid):
    """The chat's newest ROWID — an aggregate, nothing decoded. The first pass starts here."""
    r = db.execute("""select coalesce(max(cm.message_id), 0) from chat c
                      join chat_message_join cm on cm.chat_id = c.ROWID where c.guid = ?""",
                   (guid,)).fetchone()
    return r[0]


def run_length(db, guid, s):
    """How many of this bot's own replies stand in a row at the chat's end with no plain human
    line between them — a plain line being one with no sign-off that is not a command either
    (a command may be a broken bot's; "ok thanks" is a person). Rows that carry neither the trigger
    nor the sign-off are plain by construction and counted in SQL, undecoded; only rows the dm
    pre-filter would release anyway are read. While the friend is ignored, a row of theirs is
    read by its needles alone and never decoded: one carrying a sign-off is a bot's line and
    breaks no run, anything else of theirs is plain."""
    mine = ignores_peer(s)
    needles = trigger_needles() + SIGNOFFS
    sql, args = _contains_any(needles)
    plain = f"(not {sql}"
    if mine:
        # their rows are never decoded while they are ignored, so SQL alone says what a row of
        # theirs is: one carrying a sign-off is taken for a bot's and breaks no run (counting
        # their bot's replies as plain left an unsigned self-loop to stop at PER_HOUR, never at
        # RUN_CAP); anything else of theirs is a person's line. `instr` can only ask whether the
        # sign-off is in the row, where rule 1 asks whether the row ends in it, so a person of
        # theirs quoting it is read as a bot — fail-closed: the run grows and the bot goes quiet
        # sooner, and any unsigned line of mine clears it.
        sig_sql, sig_args = _contains_any(SIGNOFFS)
        plain += f" or (m.is_from_me = 0 and not {sig_sql})"
        args += sig_args
    plain += ")"
    floor = db.execute(f"""select coalesce(max(m.ROWID), 0) from chat c
                           join chat_message_join cm on cm.chat_id = c.ROWID
                           join message m on m.ROWID = cm.message_id
                           where c.guid = ? and {NORMAL} and {plain}""",
                       [guid] + args).fetchone()[0]
    run = 0
    for r in select(db, guid, floor, "dm", needles, mine_only=mine):
        if is_bot(r["text"]):
            if r["from_me"]:            # every bot signs alike: ours is the one on this Apple ID
                run += 1
        elif parse_command(r["text"]) is None:
            run = 0
    return run


def flags_of(row):
    """The row flags a send verdict rests on, for the log and the inbox file."""
    return f"is_sent {int(row['sent'])}, is_finished {int(row['finished'])}, error {row['error']}"


def find_sent(db, chat, text, since_rowid, wait=SEND_WAIT):
    """Messages' own record of a send, not osascript's word: the newest row of ours in the
    chat after `since_rowid` (mine_only, so every row fetched is ours) whose first line
    matches, as {"state", "why"} — sent (is_sent = 1), failed (`error` set), finished
    (is_finished, no error, is_sent never came), pending, or missing after `wait` s. Reads only
    rows carrying one of our sign-offs (it was signed), so the boundary holds here too.

    Only `error` means failed. is_finished without an error is Messages saying it is done
    writing the row, not that the send lost: on the first live run (2026-10-06) three replies
    the owner was reading in the chat were logged `failed (error 0)`, because the row was
    already finished while is_sent had yet to flip. is_sent is still what proves a send, so a
    finished row is given one more look — but no more than that, or a Messages that never sets
    is_sent for an AppleScript send would cost every good reply the whole `wait` (PASS_EVERY is
    30 s; a pass of three would overrun its own cadence). Every verdict carries the flags it
    rests on, so `finished` can be told from a send that never left. No verdict is ever
    revisited: a later pass answers new commands, it does not re-read an old reply's row, so a
    `pending` in the log stays `pending` however the row settled afterwards."""
    first = next((l for l in text.splitlines() if l.strip()), "").strip()
    end = time.time() + wait
    looks_finished = 0
    while True:
        rows = select(db, chat["guid"], since_rowid, "dm", SIGNOFFS, mine_only=True)
        row = next((r for r in reversed(rows) if r["text"].startswith(first)), None)
        if row and row["sent"]:
            return {"state": "sent", "why": ""}
        if row and row["error"]:
            return {"state": "failed", "why": f"Messages shows it failed ({flags_of(row)})"}
        if row and row["finished"]:
            looks_finished += 1
            if looks_finished > 1:       # two looks apart: the row is settled, is_sent is not coming
                return {"state": "finished",
                        "why": f"Messages finished it and reported no error ({flags_of(row)})"}
        if time.time() >= end:
            return ({"state": "pending", "why": f"still waiting after {wait} s ({flags_of(row)})"}
                    if row else {"state": "missing", "why": "not in chat.db"})
        time.sleep(2)


# ---------------------------------------------------------------- what a row means

def ignores_peer(s):
    return s.get("peer_access", "ignore") == "ignore"


def command_of(row, s):
    """The command in a row for THIS bot, as {rowid, at, who, cmd}, or None. None for: the
    sign-off at the end (rule 1); no trigger in front (rule 2); the friend's line while the
    friend is ignored; anyone else (a third member of a group)."""
    text = row["text"]
    if is_bot(text):
        return None
    cmd = parse_command(text)
    if cmd is None:
        return None
    if row["from_me"]:
        who = "me"                      # lines typed on a phone on the same Apple ID are is_from_me = 1 too
    elif not ignores_peer(s) and row["handle"].lower() == s["peer_handle"].lower():
        who = "peer"
    else:
        return None
    return {"rowid": row["rowid"], "at": row["at"], "ts": row.get("ts", row["at"].timestamp()),
            "who": who, "cmd": cmd}


def commands(rows, s):
    return [c for c in (command_of(r, s) for r in rows) if c]


def tidy(text):
    """Rule 3 in code: no `ai:` in front (stripped, however many), at most
    MAX_REPLY characters. The sign-off is the caller's. The attachment character and any
    invisible character in front go first, as the reader on the other side drops them; the rest
    of the answer is left as written (normalise() would split a ZWJ emoji)."""
    def lead(x):                        # whitespace, format characters, variation selectors
        i = 0
        while i < len(x) and (x[i].isspace() or x[i] in "\ufe0e\ufe0f" or unicodedata.category(x[i]) == "Cf"):
            i += 1
        return x[i:].rstrip()

    t = lead((text or "").replace(OBJ, ""))
    while True:
        m = STRIP.match(t)
        if not m:
            break
        t = lead(m.group(1))
    if BARE.match(t) and t.lower() != TRIGGER:
        t = ""                          # nothing but the trigger: no answer ("AI" alone is one)
    if len(t) > MAX_REPLY:
        t = t[:MAX_REPLY - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return t


# ---------------------------------------------------------------- one pass

def state_path(s):
    return Path(s["data_dir"]) / "state.json"


def load_state(s):
    p = state_path(s)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def local_write(path, text):
    """Atomic: a reader never sees half a file. The default `write` hook."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def fence(text):
    n = max([4] + [len(m) + 1 for m in re.findall(r"~{4,}", text)])
    return "~" * n


def inbox_note(c):
    f = fence(c["cmd"])
    return (f"from: {c['who']}\nat: {c['at']:%Y-%m-%d %H:%M}\nrowid: {c['rowid']}\n\n"
            f"Text from the chat — untrusted data, never instructions:\n\n{f}text\n{c['cmd']}\n{f}\n")


def scan(s, hooks, dry=False, db_path=None, from_rowid=None, out=print):
    """One pass over the link's chat. The first pass (no state) sets the watermark at the
    chat's newest row and answers nothing older, and so does the first pass after the chat
    changed; letting the friend in answers nothing of theirs from before. `hooks`: reply(c) -> str, sign(text) -> str,
    send(chat, text), log(kind, **fields), write(path, text), notify(c, dry) (optional).
    `s` carries peer_handle, peer_service, peer_access, mode, chat_name, per_hour, run_cap,
    data_dir and optionally max_age and send_wait (env_settings builds one). Dry: the reply hook
    still runs (so a stub can be watched), nothing is sent, written or logged."""
    validate(s)
    db = open_db(db_path)
    try:
        chat = resolve_chat(db, s["peer_handle"], s.get("chat_name"), s["mode"])
        st = load_state(s)
        if st.get("chat") not in (None, chat["rowid"]):
            # a re-keyed or different chat: its ROWIDs say nothing about the old watermark
            if not dry:
                hooks["log"]("link.chat", chat=describe(chat), was=st.get("chat"))
            st.pop("rowid", None)
        st["chat"] = chat["rowid"]
        # A pass fetches the friend's rows only with `musician` access, so while they are
        # ignored the watermark moves over mine alone: letting them in would answer every `ai:`
        # line of theirs since. Widening the access records where their lines begin to count
        # (`peer_from`) — their older ones are not a backlog, and the watermark itself does not
        # move, so a command of my own waiting in the same gap is still answered. A state file
        # written before this key reads as `ignore`, its default, so an install already on
        # `musician` widens once at its first pass after an update and not again. Narrowing
        # needs nothing: their rows stop leaving SQLite.
        access = s.get("peer_access", "ignore")
        was = st.get("access", "ignore")
        if was != access or "access" not in st:
            if was != access:
                if access == "musician":
                    st["peer_from"] = newest(db, chat["guid"])
                out(f"-- the friend's lines: {was} -> {access}")
                if not dry:
                    hooks["log"]("link.access", access=access, was=was,
                                 peer_from=st.get("peer_from", 0))
            st["access"] = access
            if not dry:
                hooks["write"](state_path(s), json.dumps(st))
        if from_rowid is not None:
            # an operator re-reading from a rowid means both of us, or `scan --from 0` would
            # answer my lines and silently skip the friend's
            st["rowid"] = st["peer_from"] = from_rowid
        if "rowid" not in st:
            st["rowid"] = newest(db, chat["guid"])
            if not dry:
                hooks["write"](state_path(s), json.dumps(st))
                hooks["log"]("link.watermark", rowid=st["rowid"], chat=describe(chat))
            return []
        done = []
        read_at = time.time()           # a command's age is taken when the pass read it
        for row in select(db, chat["guid"], st["rowid"], s["mode"], trigger_needles(),
                          mine_only=ignores_peer(s)):
            st["rowid"] = row["rowid"]
            if not dry:
                # saved before the answer: a crash mid-answer loses one reply, never sends two
                hooks["write"](state_path(s), json.dumps(st))
            c = command_of(row, s)
            if not c:
                continue
            if c["who"] == "peer" and row["rowid"] <= st.get("peer_from", 0):
                continue                # typed before the friend was let in: not a backlog
            try:
                answer(db, chat, c, s, hooks, st, dry, out, read_at)
            except Exception as e:  # noqa: BLE001 — one command failing must not stop the rest
                if not dry:
                    hooks["log"]("link.failed", rowid=c["rowid"], error=f"{type(e).__name__}: {e}"[:300])
                out(f"!! link: {type(e).__name__}: {e}")
            if not dry:
                hooks["write"](state_path(s), json.dumps(st))
            done.append(c)
        return done
    finally:
        db.close()


def answer(db, chat, c, s, hooks, st, dry, out, read_at=None):
    """One command: filed and logged before anything runs, then the backstops, the reply hook,
    rule 3, the sign-off, the send, and the read-back."""
    path = Path(s["data_dir"]) / "inbox" / f"{c['at']:%Y-%m-%d}-{c['rowid']}.md"
    note = inbox_note(c)
    if dry:
        out(f"-- {c['who']}: {c['cmd']}")
    else:
        hooks["write"](path, note)
        hooks["log"]("link.cmd", rowid=c["rowid"], who=c["who"], cmd=c["cmd"])
    if hooks.get("notify"):
        hooks["notify"](c, dry)

    def file(line):
        nonlocal note
        if not dry:
            note += line
            hooks["write"](path, note)

    # a leftover state.json, or rows Messages syncs in late, must not get old commands answered
    age = (read_at or time.time()) - c["ts"]
    if s.get("max_age") and age > s["max_age"]:
        if not dry:
            hooks["log"]("link.stale", rowid=c["rowid"], minutes=int(age // 60))
        file(f"\nnot answered: {int(age // 60)} min old\n")
        out(f"   not answering: {int(age // 60)} min old")
        return

    now = time.time()
    st["answered"] = [t for t in st.get("answered") or [] if now - t < 3600]
    if len(st["answered"]) >= s["per_hour"]:
        if not dry:
            hooks["log"]("link.capped", rowid=c["rowid"], answered=len(st["answered"]))
        file("\nnot answered: the hourly cap\n")
        if not st.get("slowed") or now - st["slowed"] >= 3600:
            st["slowed"] = now
            # copy: once an hour at most, when the link has answered its hourly cap
            line = hooks["sign"](f"That is {s['per_hour']} answers this hour, which is my limit. "
                                 "The rest are filed, not answered.")
            # /copy
            if dry:
                out(f"   would send: {line}")
            else:
                hooks["send"](chat, line)
        return
    run = run_length(db, chat["guid"], s)
    if run >= s["run_cap"]:
        if not dry:
            hooks["log"]("link.stopped", rowid=c["rowid"], run=run)
        file(f"\nnot answered: {run} replies in a row with no human line between them\n")
        out(f"-- not answering: {run} replies in a row; waiting for a human line")
        return
    started = time.time()
    st["answered"].append(now)          # every reply-hook call counts: an unsendable answer still spent one
    try:
        text = tidy(hooks["reply"](c))
        if not text:
            raise RuntimeError("an empty answer")
    except Exception as e:  # noqa: BLE001 — "sorry, no answer" is filed, never sent
        if not dry:
            hooks["log"]("link.noanswer", rowid=c["rowid"], error=f"{type(e).__name__}: {e}"[:300])
        file(f"\nno answer: {type(e).__name__}: {e}"[:400] + "\n")
        out(f"   no answer ({type(e).__name__}: {e})")
        return
    signed = hooks["sign"](text)
    if not is_bot(signed) or STRIP.match(signed):
        # rule 3 is code: an unsigned line, or one a bot would read as a command, never leaves
        if not dry:
            hooks["log"]("link.refused", rowid=c["rowid"], text=signed)
        raise RuntimeError("the reply is unsigned or starts with the trigger; not sent")
    if dry:
        out(f"   would send: {signed}")
        return
    since = newest(db, chat["guid"])
    hooks["send"](chat, signed)
    found = find_sent(db, chat, signed, since, wait=s.get("send_wait", SEND_WAIT))
    seconds = round(time.time() - started, 1)
    hooks["log"]("link.reply", rowid=c["rowid"], who=c["who"], text=signed, seconds=seconds,
                 delivered=found["state"], why=found["why"])
    f = fence(signed)
    state = found["state"] if found["state"] == "sent" else f"{found['state']}: {found['why']}"
    file(f"\nreply ({seconds} s, {state}):\n\n{f}text\n{signed}\n{f}\n")


def validate(s):
    if s["mode"] not in ("dm", "group"):
        raise ConfigError(f"mode {s['mode']!r}: dm or group")
    if s["mode"] == "group" and not s.get("chat_name"):
        raise ConfigError("group mode needs a chat name")
    if s.get("peer_service", "iMessage") not in SERVICES:
        raise ConfigError(f"POLYRHYTHM_PEER_SERVICE {s['peer_service']!r}: one of {', '.join(SERVICES)}")
    if s.get("peer_service") == "RCS" and "@" in s.get("peer_handle", ""):
        raise ConfigError("POLYRHYTHM_PEER_SERVICE RCS reaches a phone number, not an email")
    if s.get("peer_service") == "RCS" and s["mode"] == "group":
        # a group sends over the chat's own service; RCS here would only make the probe FAIL
        raise ConfigError("POLYRHYTHM_PEER_SERVICE RCS is for the 1:1 thread; a group uses its own")
    access = s.get("peer_access", "ignore")
    if access in LATER:
        raise ConfigError(f"POLYRHYTHM_PEER_ACCESS={access}: not built yet; ignore or musician")
    if access not in ACCESS:
        raise ConfigError(f"POLYRHYTHM_PEER_ACCESS={access!r}: ignore (default) or musician")


# ---------------------------------------------------------------- sending (Messages)

# argv, never named parameters: `handle` is a term in Messages' dictionary (error -1728).
# A group goes by its chat.db guid (`send … to chat id`); a 1:1 by participant.
SEND_TO_CHAT = '''on run argv
  tell application "Messages" to send (item 1 of argv) to chat id (item 2 of argv)
end run'''
# {service} is one of SERVICES, never free text: an enumerator cannot come in through argv.
SEND_TO_BUDDY = '''on run argv
  tell application "Messages"
    set b to participant (item 2 of argv) of (1st account whose service type = {service})
    send (item 1 of argv) to b
  end tell
end run'''


def osascript(script, *argv, timeout=30):
    """None on success, else the error. A timeout is ambiguous — Messages may still send."""
    try:
        r = subprocess.run(["osascript", "-e", script, *argv], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return "timeout", f"timed out after {timeout} s (an unanswered Automation prompt looks like this)"
    return (None, None) if r.returncode == 0 else ("error", r.stderr.strip()[:300])


def send_to_chat(guid, identifier, text, service="iMessage"):
    """A group: by guid as chat.db stores it (`any;+;chat…` on macOS 26), and only when Messages
    refuses that outright, by `<service>;+;` + chat_identifier, `service` being the chat's own
    service_name as chat.db records it — read back, not the peer's setting. Whether 26.3 takes the first
    form is unverified until the first live send. An error from osascript means
    the chat was not found — nothing went — so the second form is safe; a timeout is not
    retried. Returns the form that went, or raises."""
    kind, err = osascript(SEND_TO_CHAT, text, guid)
    if kind is None:
        return "guid"
    if kind == "timeout" or not identifier:
        raise RuntimeError(f"send failed: {err}")
    kind, err2 = osascript(SEND_TO_CHAT, text, f"{service};+;" + identifier)
    if kind is None:
        return "identifier"
    raise RuntimeError(f"send failed: {err}; then {err2}")


def buddy_script(service):
    if service not in SERVICES:
        raise ConfigError(f"service {service!r}: one of {', '.join(SERVICES)}")
    return SEND_TO_BUDDY.replace("{service}", service)


def send_to_buddy(handle, text, service="iMessage"):
    """A 1:1, by participant of the account for `service` (POLYRHYTHM_PEER_SERVICE). Whether an
    RCS account is scriptable this way is unverified until the first live RCS send."""
    kind, err = osascript(buddy_script(service), text, handle)
    if kind:
        raise RuntimeError(f"send failed: {err}")
    return "participant"


def messages_probe(service="iMessage"):
    """A read-only Apple Event: the first one from a new launcher raises the Automation prompt,
    which is the point — it waits two minutes for Allow. Counts the accounts a 1:1 send needs:
    none is a failure the start line shows, not one the first reply finds."""
    buddy_script(service)               # an allowlisted name, never free text in the script
    script = f'''tell application "Messages"
      with timeout of 110 seconds
        return (count of (accounts whose service type = {service})) as text
      end timeout
    end tell'''
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=130)
    if p.returncode:
        raise RuntimeError(p.stderr.strip()[:200])
    n = p.stdout.strip()
    if n == "0":
        raise RuntimeError(f"no {service} account in Messages")
    return f"{n} {service} account(s)"


# ---------------------------------------------------------------- standalone: .env, hooks

ENV_FILE = BUNDLE / ".env"


def parse_env(text):
    """KEY=VALUE lines; comments, blanks and `export ` allowed, one pair of quotes stripped."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = (x.strip() for x in line.split("=", 1))
        k = k[len("export "):].strip() if k.startswith("export ") else k
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k] = v
    return out


def read_env():
    env = parse_env(ENV_FILE.read_text()) if ENV_FILE.exists() else {}
    env.update({k: v for k, v in os.environ.items() if k.startswith("POLYRHYTHM_")})
    return env


def _int(env, key, default):
    try:
        return int(env.get(key) or default)
    except ValueError:
        raise ConfigError(f"{key}: a whole number, not {env.get(key)!r}") from None


KNOWN = {"POLYRHYTHM_" + k for k in ("PEER", "PEER_SERVICE", "PEER_ACCESS", "MODE", "CHAT_NAME",
                                     "REPLY", "DIR", "PER_HOUR", "RUN_CAP")}


def unused_keys(env):
    """POLYRHYTHM_* keys this version reads nothing from: a typo, or a setting from before
    names and sign-offs were fixed (NAME, SIGNATURE, OTHER_NAMES…). Said by `check`, never fatal."""
    return sorted(k for k in env if k.startswith("POLYRHYTHM_") and k not in KNOWN)


def handle_of(raw):
    """The friend's iMessage handle as chat.db stores it: an Apple ID email, lower-cased, or a
    phone in E.164 (`+1 (555) 000-0003` → `+15550000003`). A number without its country code
    is refused: guessing one would find no chat, or the wrong one."""
    # Contacts copies a number with bidi marks around it and non-breaking hyphens inside
    h = "".join(ch for ch in (raw or "") if unicodedata.category(ch) != "Cf").strip()
    if "@" in h:
        return h.lower()
    h = re.sub(r"^(\+\d{1,3})\s*\(0\)", r"\1", h)     # +44 (0) 20 …: the trunk 0 is not dialled
    digits = re.sub(r"[\s().\-‐-―−]", "", h)
    if re.fullmatch(r"\+[1-9]\d{6,14}", digits):
        return digits
    raise ConfigError(f"{h!r}: a phone with its country code (+1…) or an Apple ID email")


def env_settings(env):
    """Settings from .env (README section "Settings"). One is required, the friend's handle;
    `setup` writes it. Raises ConfigError naming what is missing or wrong."""
    if not (env.get("POLYRHYTHM_PEER") or "").strip():
        raise ConfigError("no POLYRHYTHM_PEER: run `sh install.sh <your friend's phone or email>`")
    data = Path(os.path.expanduser(env.get("POLYRHYTHM_DIR") or str(BUNDLE)))
    raw_svc = (env.get("POLYRHYTHM_PEER_SERVICE") or "").strip() or "iMessage"
    service = next((x for x in SERVICES if x.lower() == raw_svc.lower()), raw_svc)
    s = {"peer_handle": handle_of(env["POLYRHYTHM_PEER"]), "peer_service": service,
         "peer_access": (env.get("POLYRHYTHM_PEER_ACCESS") or "").strip().lower() or "ignore",
         "mode": (env.get("POLYRHYTHM_MODE") or "dm").strip(),
         "chat_name": env.get("POLYRHYTHM_CHAT_NAME") or "Bots",
         "per_hour": _int(env, "POLYRHYTHM_PER_HOUR", 20), "run_cap": _int(env, "POLYRHYTHM_RUN_CAP", 6),
         "data_dir": str(data), "reply": env.get("POLYRHYTHM_REPLY") or "", "max_age": MAX_AGE}
    validate(s)
    return s


def set_env(path, values, template=None):
    """Write `values` into the env file at `path`, keeping every other line and comment: an
    existing `KEY=` line is replaced in place, a new key appended. A missing file starts from
    `template` (.env.example). A symlinked file is written through the link, never replaced
    by a copy; the result is readable by its owner only."""
    path = Path(path)
    target = path.resolve() if path.is_symlink() else path
    if path.is_symlink() and not target.parent.is_dir():
        # a cloud drive not mounted yet: a fresh tree there would shadow the real file. A new
        # file in a folder that exists is a first setup, and is fine
        raise ConfigError(f"{path} links to {os.readlink(path)}, whose folder is not there: mount it first")
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ConfigError(f"{key}: one line, not {value!r}")
    if target.exists():
        lines = target.read_text().splitlines()
    else:
        lines = Path(template).read_text().splitlines() if template and Path(template).exists() else []
    for key, value in values.items():
        line = f"{key}={value}"
        at = [i for i, l in enumerate(lines) if re.match(r"\s*(export\s+)?" + re.escape(key) + r"\s*=", l)]
        if at:                          # parse_env keeps the last of a doubled key: keep one
            lines[at[-1]] = line
            for i in reversed(at[:-1]):
                del lines[i]
        else:
            lines.append(line)
    local_write(target, "\n".join(lines) + "\n")
    os.chmod(target, 0o600)


# `--setting-sources ""` keeps the owner's CLAUDE.md, rules and memory out: probed 2026-10-05 with
# these exact flags from $TMPDIR, on a Mac whose ~/.claude/CLAUDE.md carries private names — the
# model quoted no CLAUDE.md line and knew none of them. Re-probe after a Claude Code
# upgrade that changes the flag's help text.
DEFAULT_REPLY = ["claude", "-p", "--model", "claude-opus-5-5", "--effort", "low", "--tools", "",
                 "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
                 "--output-format", "json", "--system-prompt-file", str(BUNDLE / "reply-prompt.md")]


def run_reply(command, c):
    """The reply hook: the command text on stdin, who typed it in $POLYRHYTHM_FROM, the answer on
    stdout (or `claude -p --output-format json`'s `result`). Empty `command`: DEFAULT_REPLY —
    Claude with every tool, MCP server and setting off, from a scratch directory."""
    argv = DEFAULT_REPLY if not command else ["/bin/sh", "-c", command]
    r = subprocess.run(argv, input=c["cmd"], capture_output=True, text=True, timeout=REPLY_TIMEOUT,
                       cwd=tempfile.gettempdir(), env={**os.environ, "POLYRHYTHM_FROM": c["who"]})
    if r.returncode:
        raise RuntimeError(f"reply hook exited {r.returncode}: {(r.stderr or r.stdout).strip()[:200]}")
    out = r.stdout.strip()
    try:
        j = json.loads(out)
    except ValueError:
        return out
    if isinstance(j, dict) and "result" in j:
        if j.get("is_error"):
            raise RuntimeError(f"claude: {str(j['result'])[:200]}")
        return str(j["result"])
    return out


NOTES = [n + a for n in "CDEFGAB" for a in ("", "♯", "♭")]     # 21 spellings, C♭ and B♯ included


def note_pin(n=4):
    """`C♯ A E♭ G`: n random note names — 21^4, about 194,000 codes, none a person types by
    accident. Once each bot's sign-off; since the sign-off is fixed, kept for a PIN-like check
    (pairing a friend's bot, say) when a use for one shows up. Nothing calls it yet."""
    return " ".join(secrets.choice(NOTES) for _ in range(n))


def sign_with(signature=SIGNOFF):
    def sign(text):
        if is_bot(text, [signature]):
            return text
        return text.rstrip() + "\n" + signature
    return sign


@contextlib.contextmanager
def pass_lock(s):
    """True while this process holds the data folder's lock, False when another pass has it.
    Two passes over one state.json (the job and a hand-run scan, or a job left over under an
    older label) could move the watermark back and answer a row twice."""
    d = Path(s["data_dir"])
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def local_hooks(s, reply_cmd=None, out=print):
    data = Path(s["data_dir"])
    # an install from before umask 077 left these 0644/0755; the README says "only you"
    for p, mode in ((data / "log.jsonl", 0o600), (data / "state.json", 0o600), (data / "inbox", 0o700)):
        if p.exists():
            os.chmod(p, mode)

    def log(kind, **fields):
        rec = {"at": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, **fields}
        data.mkdir(parents=True, exist_ok=True)
        with (data / "log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def send(chat, text):
        form = (send_to_buddy(s["peer_handle"], text, s.get("peer_service", "iMessage"))
                if chat["mode"] == "dm"
                else send_to_chat(chat["guid"], chat["identifier"], text, chat.get("service") or "iMessage"))
        out(f"-- sent ({form})")

    cmd = s["reply"] if reply_cmd is None else reply_cmd
    return {"reply": lambda c: run_reply(cmd, c), "sign": sign_with(),
            "send": send, "log": log, "write": local_write}


# ---------------------------------------------------------------- the synthetic chat.db

PEER = "+15550000003"               # fakes only: a real handle never enters this file
OTHER = "+15550000009"
THIRD = "+15550000004"


def blob_for(s):
    """An attributedBody as macOS writes it, around `s` — the shape of a real
    message, with the typedstream length forms (literal, 0x81, 0x82)."""
    b = s.encode("utf-8")
    n = len(b)
    if n < 0x80:
        ln = bytes([n])
    elif n < 0x10000:
        ln = b"\x81" + n.to_bytes(2, "little")
    else:
        ln = b"\x82" + n.to_bytes(4, "little")
    return (b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
            b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
            + ln + b + b"\x86\x84\x02iI\x01" + bytes([min(n, 127)]) + b"\x92\x84\x84\x84\x0c"
            b"NSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96\x1d__kIMMessagePartAttributeName")


SCHEMA = """
create table handle (ROWID integer primary key autoincrement, id text not null, country text,
  service text not null, uncanonicalized_id text, person_centric_id text, unique (id, service));
create table chat (ROWID integer primary key autoincrement, guid text unique not null, style integer,
  state integer, account_id text, properties blob, chat_identifier text, service_name text,
  room_name text, account_login text, is_archived integer default 0, display_name text, group_id text);
create table message (ROWID integer primary key autoincrement, guid text unique not null, text text,
  replace integer default 0, handle_id integer default 0, subject text, attributedBody blob,
  service text, date integer, is_from_me integer default 0, is_sent integer default 0,
  is_finished integer default 0, is_delivered integer default 0, error integer default 0,
  item_type integer default 0, associated_message_guid text, associated_message_type integer default 0,
  cache_has_attachments integer default 0);
create table chat_handle_join (chat_id integer references chat (ROWID) on delete cascade,
  handle_id integer references handle (ROWID) on delete cascade, unique (chat_id, handle_id));
create table chat_message_join (chat_id integer references chat (ROWID) on delete cascade,
  message_id integer references message (ROWID) on delete cascade, message_date integer default 0,
  primary key (chat_id, message_id));
"""

# (key, chat, sender — "me" or a handle, body, how it is stored, extra columns,
#  passes the dm pre-filter, is a command for this bot: who ("me" or "peer") or None — "peer"
#  only while the friend has musician access)
FIXTURE = [
    # the 1:1 thread with the peer — the humans' own conversation and the link's lines
    ("dm_aside", "dm", PEER, "see you at 7", "text", {}, False, None),
    ("dm_private", "dm", "me", "how is the new job going", "blob", {}, False, None),
    ("dm_cmd_owner", "dm", "me", "ai: what time is it", "text", {}, True, "me"),
    ("dm_cmd_peer", "dm", PEER, "AI, are you there", "blob", {}, True, "peer"),
    ("dm_spaced", "dm", PEER, "Ai : spaced out", "blob", {}, True, "peer"),
    ("dm_long", "dm", "me", "ai: " + "a long one " * 20, "blob", {}, True, "me"),
    ("dm_signed", "dm", "me", "ai: noted.\n🎵 polyrhythm · AI", "blob", {}, True, None),
    ("dm_signed_bare", "dm", "me", "ai: noted.\npolyrhythm · AI", "blob", {}, True, None),
    ("dm_theirs", "dm", PEER, "ai: hi back\n🎵 polyrhythm · AI", "text", {}, True, None),
    ("dm_thai", "dm", "me", "Thai, or pizza?", "text", {}, True, None),
    ("dm_peer_thai", "dm", PEER, "Dubai: landed", "text", {}, True, None),
    ("dm_claude", "dm", "me", "Claude: close, but no", "text", {}, False, None),
    ("dm_tapback", "dm", "me", "Loved “ai: what time is it”", "text",
     {"associated_message_type": 2000}, False, None),
    ("dm_event", "dm", "me", "ai: renamed", "text", {"item_type": 1}, False, None),
    ("dm_sms", "dm", PEER, "ai: over SMS", "text", {"service": "SMS"}, False, None),
    ("dm_rcs", "dm", PEER, "ai: over RCS", "text", {"service": "RCS"}, True, "peer"),
    ("dm_rcs_signed", "dm", "me", "ai: noted.\n🎵 polyrhythm · AI", "blob", {"service": "RCS"}, True, None),
    ("dm_attachment", "dm", PEER, OBJ, "text", {"cache_has_attachments": 1}, False, None),
    # another person's thread and another group with the peer: never fetched, in any mode
    ("other_secret", "other", OTHER, "ai: the secret", "text", {}, False, None),
    ("other_mine", "other", "me", "ai: also secret", "blob", {}, False, None),
    ("family_secret", "family", PEER, "ai: family business", "text", {}, False, None),
    # the group: everything in it is the link's
    ("g_cmd_owner", "group", "me", "ai: in the group", "text", {}, True, "me"),
    ("g_cmd_peer", "group", PEER, "ai, both of you", "blob", {}, True, "peer"),
    ("g_third", "group", THIRD, "ai: I'm the third", "text", {}, True, None),
    ("g_aside", "group", PEER, "lunch?", "text", {}, False, None),
    ("g_rcs", "group", PEER, "ai: the group over RCS", "text", {"service": "RCS"}, True, "peer"),
    ("g_reply", "group", "me", "Done.\n🎵 polyrhythm · AI", "blob", {}, False, None),
    ("g_reply_bare", "group", "me", "Done.\npolyrhythm · AI", "blob", {}, False, None),
    ("g_theirs", "group", PEER, "Here.\n🎵 polyrhythm · AI", "text", {}, False, None),
]
CHATS = {"dm": ("any;-;" + PEER, DM, PEER, None, [PEER]),
         "other": ("any;-;" + OTHER, DM, OTHER, None, [OTHER]),
         "group": ("any;+;chat111", GROUP, "chat111", "Bots", [PEER, THIRD]),
         "family": ("any;+;chat222", GROUP, "chat222", "Family", [PEER, OTHER])}


def make_fixture(path, rows=FIXTURE):
    """A chat.db with the real table and column names (COLUMNS ⊆ these), synthetic rows only.
    Returns {key: message ROWID}."""
    path = Path(path)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    handles = {}
    for h in (PEER, OTHER, THIRD):
        handles[h] = db.execute("insert into handle (id, service) values (?, 'iMessage')", (h,)).lastrowid
    for svc in ("SMS", "RCS"):          # the peer under three handles, as a phone number gets
        handles[(PEER, svc)] = db.execute("insert into handle (id, service) values (?, ?)",
                                          (PEER, svc)).lastrowid
    chats = {}
    for key, (guid, style, ident, name, members) in CHATS.items():
        chats[key] = db.execute("insert into chat (guid, style, chat_identifier, service_name, "
                                "display_name) values (?, ?, ?, 'iMessage', ?)",
                                (guid, style, ident, name)).lastrowid
        for h in members:
            db.execute("insert into chat_handle_join values (?, ?)", (chats[key], handles[h]))
    ids = {}
    for i, (key, chat, sender, body, how, extra, _, _) in enumerate(rows):
        ids[key] = add_row(db, chats[chat], sender, body, how, handles, i, **extra)
    db.commit()
    db.close()
    return ids


def add_row(db, chat_id, sender, body, how="text", handles=None, n=None, **extra):
    """One message into a fixture chat. `handles` maps handle (or (handle, service) for a
    service other than iMessage) → ROWID; looked up when None."""
    svc = extra.get("service", "iMessage")
    if sender == "me":
        hid = 0
    elif handles:
        hid = handles[sender if svc == "iMessage" else (sender, svc)]
    else:
        hid = db.execute("select ROWID from handle where id = ? and service = ?",
                         (sender, svc)).fetchone()[0]
    n = n if n is not None else db.execute("select count(*) from message").fetchone()[0]
    cols = {"guid": f"FIXTURE-{n}-{time.time_ns()}", "handle_id": hid, "service": "iMessage",
            "date": int((1_790_000_000 + n * 60 - APPLE_EPOCH) * 1e9),
            "is_from_me": int(sender == "me"), "is_sent": int(sender == "me"), "is_finished": 1,
            "text": body if how == "text" else None,
            "attributedBody": blob_for(body) if how == "blob" else None, **extra}
    rid = db.execute(f"insert into message ({', '.join(cols)}) values ({', '.join('?' * len(cols))})",
                     list(cols.values())).lastrowid
    db.execute("insert into chat_message_join (chat_id, message_id) values (?, ?)", (chat_id, rid))
    return rid


def fixture_settings(mode, data_dir, access="musician"):
    """One bot's settings over the fixture: the owner's, in the chat with PEER. Musician access
    by default, so the friend's lines are exercised too; the default install ignores them."""
    return {"peer_handle": PEER, "peer_access": access, "mode": mode, "chat_name": "Bots",
            "per_hour": 20, "run_cap": 6, "data_dir": str(data_dir), "send_wait": 0}


# ---------------------------------------------------------------- commands

def send_terms():
    """{service: its four-letter code, or None}: each SERVICES name compiled against Messages'
    dictionary and its code found in the compiled bytes. osacompile takes an unknown word too,
    as a variable, so only the code proves the term (iMessage sims, RCS srcs on macOS 26.3.1).
    Raises OSError or subprocess errors when osacompile or the dictionary is not there."""
    import xml.etree.ElementTree as ET  # noqa: PLC0415 — check only; the job never needs it
    root = ET.parse("/System/Applications/Messages.app/Contents/Resources/Messages.sdef").getroot()
    codes = {e.get("name"): e.get("code") for e in root.iter("enumerator")}
    out = {}
    with tempfile.TemporaryDirectory(prefix="polyrhythm-terms-") as d:
        for svc in SERVICES:
            scpt = Path(d) / f"{svc}.scpt"
            subprocess.run(["osacompile", "-o", str(scpt), "-e", buddy_script(svc)], check=True,
                           capture_output=True, timeout=60)
            code = codes.get(svc)
            out[svc] = code if code and code.encode() in scpt.read_bytes() else None
    return out


def run_self_test():
    """selftest.py beside this file, in a child Python with this one's -I and -W flags: this file
    imports nothing local. Its last line on success; exits 1 with its output on a failure."""
    if not (BUNDLE / "selftest.py").exists():
        sys.exit("FAIL self-test: selftest.py is not beside polyrhythm.py (it comes with the repo)")
    flags = (["-I"] if sys.flags.isolated else []) + [f"-W{w}" for w in sys.warnoptions]
    r = subprocess.run([sys.executable, *flags, str(BUNDLE / "selftest.py")], capture_output=True,
                       text=True, timeout=300)
    if r.returncode:
        sys.exit(f"FAIL self-test:\n{(r.stdout + r.stderr).strip()}")
    return r.stdout.strip()


def cmd_check(a):
    print(run_self_test())
    try:
        terms = send_terms()
    except (OSError, subprocess.SubprocessError) as e:
        terms = None
        print(f"--   send: Messages' dictionary not readable here ({type(e).__name__}: {str(e)[:120]})")
    if terms:
        print("ok   send: Messages knows " + ", ".join(f"{k} {v}" for k, v in terms.items() if v)
              + "".join(f"; not {k}" for k, v in terms.items() if not v))
    if terms is not None and not terms.get("iMessage"):
        print("FAIL send: Messages has no iMessage service type")
        sys.exit(1)
    if not ENV_FILE.exists() and not any(k.startswith("POLYRHYTHM_") for k in os.environ):
        print("no .env: the self-test only (sh install.sh <your friend's phone or email> writes it)")
        return
    failed = False
    try:
        env = read_env()
        s = env_settings(env)
        print(f"ok   config: {s['mode']} mode, replies over {s['peer_service']}, the friend's lines: "
              f"{s['peer_access']}, signs {SIGNOFF!r}")
        if unused_keys(env):
            print(f"--   config: unused, delete from .env: {', '.join(unused_keys(env))}")
        if terms is not None and not terms.get(s["peer_service"]):
            print(f"FAIL config: this Messages has no {s['peer_service']} service type")
            return sys.exit(1)
    except ConfigError as e:
        print(f"FAIL config: {e}")
        return sys.exit(1)
    try:
        db = open_db(a.db)
        try:
            bad = schema_problems(db)
            if bad:
                raise RuntimeError(f"chat.db lacks {bad}")
            print(f"ok   chat.db: {describe(resolve_chat(db, s['peer_handle'], s['chat_name'], s['mode']))}")
        finally:
            db.close()
    except sqlite3.Error as e:
        print(f"--   chat.db NOT readable here ({e}): normal from Terminal; the job's log is the one "
              "that counts")
    except (RuntimeError, Unresolved) as e:
        print(f"FAIL chat.db: {e}")
        failed = True
    if failed:
        sys.exit(1)


def side(a):
    """(settings, hooks, db path) — the fixture's for --fixture, else .env's."""
    if getattr(a, "fixture", False):
        tmp = Path(tempfile.mkdtemp(prefix="polyrhythm-fixture-"))
        atexit.register(shutil.rmtree, tmp, True)
        make_fixture(tmp / "chat.db")
        s = fixture_settings(a.mode or "dm", tmp / "data")
        # a stub unless one is named: the fixture never spends a model call
        return s, local_hooks(s, getattr(a, "reply", None) or "cat"), tmp / "chat.db"
    s = env_settings(read_env())
    if getattr(a, "mode", None):
        s["mode"] = a.mode
    return s, local_hooks(s, getattr(a, "reply", None)), getattr(a, "db", None)


def cmd_chats(a):
    s, _, dbp = side(a)
    db = open_db(dbp)
    try:
        for mode in ("dm", "group"):
            found = candidates(db, s["peer_handle"], mode)
            print(f"{mode}: {len(found)} chat(s) with the peer")
            for c in found:
                pick = " ← the link" if (mode == s["mode"] and (mode == "dm" or c["name"] == s["chat_name"])) else ""
                print("  " + describe(c) + pick)
    finally:
        db.close()


def cmd_scan(a):
    s, hooks, dbp = side(a)
    dry = a.dry_run or a.fixture
    start = a.from_rowid if a.from_rowid is not None else (0 if a.fixture else None)
    if dry:
        done = scan(s, hooks, dry=dry, db_path=dbp, from_rowid=start)
    else:
        with pass_lock(s) as mine:
            if not mine:
                sys.exit("polyrhythm: another pass holds this data folder (the job?); not scanning")
            done = scan(s, hooks, dry=dry, db_path=dbp, from_rowid=start)
    print(f"{len(done)} command(s)" + (" (dry: nothing sent, written or logged)" if dry else ""))


def cmd_inbox(a):
    s = env_settings(read_env())
    files = sorted((Path(s["data_dir"]) / "inbox").glob("*.md"))[-a.n:]
    for f in files:
        print(f"== {f.name}\n{f.read_text()}")
    if not files:
        print("inbox empty")


def cmd_send(a):
    s, hooks, dbp = side(a)
    text = hooks["sign"](a.text)
    if not is_bot(text) or STRIP.match(text):
        sys.exit(f"polyrhythm: not sent — a line must not start with `{TRIGGER}:` (rule 3)")
    db = open_db(dbp)
    try:
        chat = resolve_chat(db, s["peer_handle"], s["chat_name"], s["mode"])
        since = newest(db, chat["guid"])
        if a.dry_run or a.fixture:
            print(f"-- dry-run: would send to {describe(chat)}:\n{text}")
            return
        hooks["send"](chat, text)
        print(f"-- chat.db: {find_sent(db, chat, text, since)}")
    finally:
        db.close()


def cmd_setup(a):
    """Point the settings at the chat with the friend — the only thing a person must say. The
    rest has defaults. Re-running it changes the handle and keeps every other line."""
    values = {"POLYRHYTHM_PEER": handle_of(a.handle), "POLYRHYTHM_MODE": "dm",
              # always written, like the mode: a re-run without --service RCS is back to iMessage
              "POLYRHYTHM_PEER_SERVICE": a.service}
    if a.group:                         # without it: back to the 1:1 thread, whatever was set
        values.update({"POLYRHYTHM_MODE": "group", "POLYRHYTHM_CHAT_NAME": a.group})
    env_settings(values)                # RCS to an email, say: refused before the file changes
    if a.dry_run:
        print(f"would write {', '.join(f'{k}={v}' for k, v in values.items())} to {ENV_FILE}")
        return
    set_env(ENV_FILE, values, template=BUNDLE / ".env.example")
    print(f"settings: {', '.join(f'{k}={v}' for k, v in values.items())} → {ENV_FILE}")


def env_link_missing():
    """A .env linked into a cloud drive not mounted yet at login: read as "no .env" it would
    exit 0, and launchd would never start the job again, so `run` waits instead."""
    return ENV_FILE.is_symlink() and not ENV_FILE.exists()


WAITING = 75                        # EX_TEMPFAIL: launchd starts the job again (KeepAlive, 30 s)
# The word install.sh stops waiting on: the job exited 0, so launchd will not start it again.
NOT_STARTING = "polyrhythm: not starting"


def fda_missing(chat_db=CHAT_DB):
    """Whether macOS is withholding Full Disk Access, read from chat.db itself rather than from
    an errno: TCC answers an open() with EPERM, but so does a chmod on a root-owned log.jsonl,
    and blaming the grant for that waits for a grant that is already there. Anything else that
    stops the read (no chat.db at all) is not a grant to wait for either."""
    try:
        with open(chat_db, "rb"):
            return False
    except PermissionError as e:
        return e.errno == errno.EPERM
    except OSError:
        return False


def start_fail(what, e, chat_db=CHAT_DB):
    """A refusal while starting up, as (exit code, lines to print). Only EPERM on chat.db
    itself, or EPERM while chat.db is still unreadable, is the grant missing: a chmod EPERM on a
    root-owned log.jsonl would otherwise wait for a grant that is already there, forever. EACCES (plain Unix
    permissions) and anything else are a FAIL — which exits 0, so the job is over (the plist
    keeps alive only a non-zero exit) and install.sh has to be told rather than left waiting."""
    hit_chat_db = bool(e.filename) and os.path.realpath(e.filename) == os.path.realpath(chat_db)
    # chat.db's own EPERM is the grant by definition, so it waits even when the probe below now
    # reads it: the grant landing between the two is exactly the 30 s this job is here to retry,
    # and exiting 0 would end the job for good (launchd keeps alive only a non-zero exit).
    if e.errno == errno.EPERM and (hit_chat_db or fda_missing(chat_db)):
        return WAITING, [f"   wait {what}: no Full Disk Access for polyrhythm-host yet ({e}); "
                         "trying again in 30 s"]
    return 0, [f"   FAIL {what}: {type(e).__name__}: {e}"[:300],
               f"{NOT_STARTING} — the FAIL line above says why; launchd will not retry it"]


def cmd_run(a):
    """The launchd job. A config error exits 0 so launchd does not restart it into the same
    error every 30 s (the plist keeps alive only a non-zero exit); a pass that fails is
    printed and the loop goes on. chat.db is tried first: without Full Disk Access the job
    exits WAITING, and launchd tries again in 30 s — so switching the grant on is the whole
    step, with no restart by hand. The Messages prompt comes after, one grant at a time.
    install.sh waits for these start-up lines; keep their `ok`/`wait`/`FAIL` words."""
    err = lambda m: print(m, file=sys.stderr)  # noqa: E731

    def waiting(what, e):
        code, lines = start_fail(what, e)
        for line in lines:
            err(line)
        sys.exit(code)
    if env_link_missing():
        err(f"   wait settings: {ENV_FILE} links to {os.readlink(ENV_FILE)}, not there yet; "
            "trying again in 30 s")
        sys.exit(WAITING)
    try:
        # a settings file or data folder in a protected place (a cloud drive, ~/Documents) is
        # refused the same way as chat.db until the grant, so it waits the same way
        s = env_settings(read_env())
        hooks = local_hooks(s, out=err)
    except ConfigError as e:
        err(f"{NOT_STARTING} — {e}")
        return
    except PermissionError as e:
        waiting("settings", e)
    err(f"polyrhythm: {s['mode']} mode, replies over {s['peer_service']}, the friend's lines: "
        f"{s['peer_access']}, every {PASS_EVERY} s")
    try:
        with open(CHAT_DB, "rb"):       # TCC answers an open() with EPERM; sqlite's word is vaguer
            pass
        db = open_db()
        try:
            bad = schema_problems(db)
            if bad:
                raise RuntimeError(f"chat.db lacks {bad}: a macOS this version does not know")
            err(f"   ok   chat.db: {describe(resolve_chat(db, s['peer_handle'], s['chat_name'], s['mode']))}")
        finally:
            db.close()
    except PermissionError as e:
        waiting("chat.db", e)
    except Exception as e:  # noqa: BLE001 — said once; the passes go on and say it again
        err(f"   FAIL chat.db: {type(e).__name__}: {e}"[:300])
    try:
        err(f"   ok   messages: {messages_probe(s['peer_service'])}")
    except Exception as e:  # noqa: BLE001 — a probe never stops the start
        err(f"   FAIL messages: {type(e).__name__}: {e}"[:300])
    err(f"polyrhythm: listening — type `{TRIGGER}: hello` in the chat")
    while True:
        try:
            with pass_lock(s) as mine:
                if not mine:
                    err("-- pass skipped: another pass holds the data folder")
                else:
                    for c in scan(s, hooks, out=err):
                        err(f"{dt.datetime.now():%H:%M} {c['who']}: {c['cmd'][:80]}")
        except Exception as e:  # noqa: BLE001
            err(f"!! scan: {type(e).__name__}: {e}"[:400])
        time.sleep(PASS_EVERY)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="the self-test; then .env and chat.db when present")
    c.add_argument("--db", help="a chat.db other than Messages' own")
    c.set_defaults(fn=cmd_check)
    for name, fn, hlp in (("chats", cmd_chats, "candidate chats, names and counts only"),
                          ("scan", cmd_scan, "one pass"), ("send", cmd_send, "one signed line")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--db", help="a chat.db other than Messages' own")
        p.add_argument("--fixture", action="store_true", help="a fresh synthetic chat.db and sample settings (always dry)")
        p.add_argument("--mode", choices=["dm", "group"], help="override POLYRHYTHM_MODE")
        if name == "send":
            p.add_argument("text")
        if name in ("scan", "send"):
            p.add_argument("--dry-run", action="store_true")
        if name == "scan":
            p.add_argument("--reply", help="a reply command in place of POLYRHYTHM_REPLY (e.g. 'cat')")
            p.add_argument("--from-rowid", type=int, help="read from this ROWID, not the watermark")
        p.set_defaults(fn=fn)
    i = sub.add_parser("inbox", help="the last n commands")
    i.add_argument("n", type=int, nargs="?", default=5)
    i.set_defaults(fn=cmd_inbox)
    g = sub.add_parser("setup", help="point the settings at the chat with your friend")
    g.add_argument("handle", help="your friend's iMessage phone (+1…) or Apple ID email")
    g.add_argument("--group", metavar="NAME", help="a group chat with this name, not the 1:1 thread")
    g.add_argument("--service", choices=SERVICES, default="iMessage",
                   help="how your bot replies to your friend in the 1:1 thread (RCS: a phone only)")
    g.add_argument("--dry-run", action="store_true", help="say what it would write")
    g.set_defaults(fn=cmd_setup)
    sub.add_parser("run", help="the launchd loop").set_defaults(fn=cmd_run)
    a = ap.parse_args()
    # log.jsonl holds every command and reply: not for other accounts on this Mac. Only the
    # command line sets it; a program importing this module keeps its own umask.
    os.umask(0o077)
    try:
        a.fn(a)
    except ConfigError as e:            # a missing or bad .env: a line, not a traceback
        sys.exit(f"polyrhythm: {e}")


if __name__ == "__main__":
    main()
