#!/usr/bin/env python3
"""The self-test: everything polyrhythm.py promises, on a synthetic chat.db — the boundary, the
rules, the backstops, both modes. `python3 polyrhythm.py check` runs it (so do install.sh and
CI); it also runs on its own. It lives outside polyrhythm.py so the file a user reads before
granting Full Disk Access is the program alone: the launcher never runs this one.
"""
import contextlib
import datetime as dt
import errno
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
from pathlib import Path
from types import SimpleNamespace

if sys.flags.optimize:                  # -O strips every assert: a pass would prove nothing
    sys.exit("selftest: asserts are off (-O or PYTHONOPTIMIZE); run it without")
# beside polyrhythm.py, wherever it is run from — `-I` drops the script's folder from sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polyrhythm  # noqa: E402
from polyrhythm import *  # noqa: E402,F401,F403 — the test reads like the code it tests


def self_test():
    """Everything the link promises, on a synthetic chat.db: the boundary, the rules, the
    backstops, both modes. Raises AssertionError on the first broken promise. Leaves nothing
    in $TMPDIR: install.sh runs it on every install."""
    tmp = Path(tempfile.mkdtemp(prefix="polyrhythm-check-"))
    try:
        return _self_test(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test(tmp):
    quiet = lambda m: None  # noqa: E731
    dbp = tmp / "chat.db"
    ids = make_fixture(dbp)
    key_of = {v: k for k, v in ids.items()}
    spec = {r[0]: r for r in FIXTURE}
    db = open_db(dbp)
    assert not schema_problems(db), schema_problems(db)

    # the decoder: literal and escaped lengths, Cyrillic, the attachment character
    for s in ("Все готово", "x" * 127, "y" * 128, "Ж" * 300, "z" * 70000):
        assert body_of(None, blob_for(s)) == s, f"body_of: {len(s)} chars"
    assert body_of("plain", b"junk") == "plain" and body_of(None, b"no string here") is None

    # resolution: read back from chat.db, never predicted; exactly one or a refusal with a list
    dm = resolve_chat(db, PEER, None, "dm")
    grp = resolve_chat(db, PEER, "Bots", "group")
    assert (dm["guid"], dm["mode"]) == ("any;-;" + PEER, "dm"), dm
    assert (grp["guid"], grp["identifier"], grp["members"]) == ("any;+;chat111", "chat111", 2), grp
    assert PEER not in describe(dm), "a 1:1 chat's description carries the handle"
    for bad in (("Nope", "group"), (None, "group")):
        try:
            resolve_chat(db, PEER, *bad)
            raise AssertionError(f"resolved {bad}")
        except Unresolved as e:
            assert '"Bots"' in str(e) and '"Family"' in str(e), str(e)

    # the boundary: only the link's chat is ever fetched; in dm mode only rows with the trigger
    # (or, for the read-back, the sign-off) leave SQLite; while the friend is ignored, only my
    # own rows do — counted, and every decode recorded
    needles = trigger_needles()
    real, seen = polyrhythm.body_of, []

    def recording(text, blob):
        seen.append(text if text else blob)
        return real(text, blob)
    polyrhythm.body_of = recording
    try:
        got_dm = {key_of[r["rowid"]] for r in select(db, dm["guid"], 0, "dm", needles)}
        got_g = {key_of[r["rowid"]] for r in select(db, grp["guid"], 0, "group")}
        n_open = len(seen)
        mine_dm = {key_of[r["rowid"]] for r in select(db, dm["guid"], 0, "dm", needles, mine_only=True)}
        mine_g = {key_of[r["rowid"]] for r in select(db, grp["guid"], 0, "group", mine_only=True)}
    finally:
        polyrhythm.body_of = real
    want_dm = {k for k, r in spec.items() if r[1] == "dm" and r[6]}
    assert got_dm == want_dm, f"dm pre-filter: extra {got_dm - want_dm}, missing {want_dm - got_dm}"
    untagged = [r for r in FIXTURE if not (r[1] == "dm" and r[6]) and r[1] != "group"]
    raw = {r[3] if r[4] == "text" else blob_for(r[3]) for r in untagged}
    assert not [x for x in seen if x in raw], "a row outside the pre-filter or the chat was decoded"
    assert n_open == len(want_dm) + len(got_g), (n_open, len(want_dm), len(got_g))
    want_g = {k for k, r in spec.items() if r[1] == "group"}
    assert got_g == want_g, f"group: {got_g ^ want_g}"
    assert mine_dm == {k for k in want_dm if spec[k][2] == "me"}, f"mine only, dm: {mine_dm}"
    assert mine_g == {k for k in want_g if spec[k][2] == "me"}, f"mine only, group: {mine_g}"
    theirs = {r[3] if r[4] == "text" else blob_for(r[3]) for r in FIXTURE if r[2] != "me"}
    assert not [x for x in seen[n_open:] if x in theirs], "the friend ignored, yet a row of theirs was decoded"
    n_dm = db.execute("""select count(*) from chat_message_join cm join chat c on c.ROWID = cm.chat_id
                         where c.guid = ?""", (dm["guid"],)).fetchone()[0]
    assert n_dm > len(got_dm) >= len(mine_dm) >= 1, "the dm count proves nothing"

    # the rules, on what was fetched, with the friend ignored and with musician access
    for mode, chat in (("dm", dm), ("group", grp)):
        for access in ACCESS:
            s = fixture_settings(mode, tmp / mode, access)
            rows = select(db, chat["guid"], 0, mode, needles, mine_only=ignores_peer(s))
            got = {key_of[c["rowid"]]: c["who"] for c in commands(rows, s)}
            want = {k: r[7] for k, r in spec.items()
                    if r[1] == mode and r[7] and (access == "musician" or r[7] == "me")}
            assert got == want, f"{mode}, {access}: commands {got} != {want}"
    s = fixture_settings("dm", tmp / "x")
    assert parse_command("ai: hi") == "hi" and parse_command("AI, hi") == "hi"
    assert parse_command("  Ai :  hi") == "hi" and parse_command("ai:\nline two") == "line two"
    assert parse_command("aI: hi") == "hi" and parse_command("ai  : hi") is None
    # ASCII only, as the needles are: a Turkish dotless or dotted i would pass rule 2 unseen by SQLite
    assert parse_command("aı: hi") is None and parse_command("Aİ: hi") is None
    # every line rule 2 takes leaves SQLite in dm mode, the tab gap included
    for line in ("ai: x", "AI, x", "Ai :x", "aI ,x", "ai\t: x", "AI\t,x"):
        assert parse_command(line) and any(n in line for n in trigger_needles()), line
    for no in ("aide: hi", "Thai: x", "hey ai: hi", "ai hi", "ai:", "", "air, x", "Claude: hi"):
        assert parse_command(no) is None, no
    row = {"rowid": 1, "at": dt.datetime(2026, 10, 3), "handle": "", "from_me": True}
    assert command_of({**row, "text": "ai: hi"}, s)["who"] == "me", "is_from_me unmarked is the owner"
    for signed in ("ai: hi\n" + SIGNOFF, "ai: hi\npolyrhythm · AI", "ai: hi\n" + SIGNOFF + "  \n",
                   "ai: x\n🎵 polyrhythm • AI", "ai: x\n" + SIGNOFF + "​"):
        assert command_of({**row, "text": signed}, s) is None, f"a signed row taken: {signed!r}"
    peer_row = {**row, "from_me": False, "handle": PEER, "text": "ai: x"}
    assert command_of(peer_row, s)["who"] == "peer", "musician access: the friend's line"
    assert command_of(peer_row, fixture_settings("dm", tmp / "x", "ignore")) is None, "ignored, yet taken"
    assert command_of({**row, "from_me": False, "handle": THIRD, "text": "ai: x"}, s) is None
    assert not is_bot("anything", []) and expand([]) == [] and expand(["", " "]) == []
    assert not is_bot("what is a polyrhythm") and not is_bot("ai: polyrhythm · AI and more")

    # rule 3 in code
    assert tidy("ai: sure") == "sure" and tidy("AI: ai: x") == "x"
    assert tidy("AI, short for artificial intelligence.") == "AI, short for artificial intelligence.", \
        "rule 3 cut a word off an answer"
    assert tidy("ai:") == "" and tidy("  ") == "" and tidy("AI") == "AI", "the answer `AI` is one"
    assert tidy("Note: a colon is fine") == "Note: a colon is fine" and tidy("Thai: food") == "Thai: food"
    long = tidy("word " * 400)
    assert len(long) <= MAX_REPLY and long.endswith("…"), len(long)
    sign = sign_with()
    assert sign(sign("x")) == "x\n" + SIGNOFF, "sign: not idempotent"
    assert is_bot(sign("x")) and parse_command(SIGNOFF) is None, "the sign-off reads as a command"
    # the note generator, kept for a PIN: four note names from the 21
    pin = note_pin()
    assert len(pin.split()) == 4 and all(n in NOTES for n in pin.split()), pin

    def fresh(name):
        # each block on its own copy: replies one block sends would count in the next one's run
        p = tmp / f"{name}.db"
        make_fixture(p)
        return p

    # end to end, both modes, both accesses: stubbed reply, the send inserts what Messages would
    for mode, chat in (("dm", dm), ("group", grp)):
        for access in ACCESS:
            s = fixture_settings(mode, tmp / f"e2e-{mode}-{access}", access)
            n_cmds = sum(1 for r in FIXTURE if r[1] == mode and r[7]
                         and (access == "musician" or r[7] == "me"))
            sent, logged, told = [], [], []
            dbp = fresh(f"e2e-{mode}-{access}")
            hooks = e2e_hooks(dbp, chat, sent, logged, told, sign)
            assert scan(s, hooks, db_path=dbp, out=quiet) == [], "the first pass answered something"
            probe = open_db(dbp)
            assert load_state(s)["rowid"] == newest(probe, chat["guid"]), "watermark"
            probe.close()
            decoded = []
            polyrhythm.body_of = lambda text, blob: decoded.append(text or blob) or real(text, blob)  # noqa: E731
            try:
                done = scan(s, hooks, db_path=dbp, from_rowid=0, out=quiet)
            finally:
                polyrhythm.body_of = real
            assert len(done) == n_cmds == len(sent), (mode, access, len(done), len(sent))
            if access == "ignore":
                assert not [x for x in decoded if x in theirs], "the friend ignored, yet a pass decoded their row"
            kinds = [k for k, _ in logged]
            assert kinds.count("link.cmd") == n_cmds and kinds.count("link.reply") == n_cmds, kinds
            for c in done:
                i, j = kinds.index("link.cmd"), kinds.index("link.reply")
                assert i < j, "a command was not logged before its answer"
                kinds[i] = kinds[j] = "-"
            assert all(f["delivered"] == "sent" for k, f in logged if k == "link.reply"), logged
            assert all(t.endswith(SIGNOFF) and parse_command(t) is None for _, t in sent)
            assert {c["who"] for c in told} <= {"me", "peer"} and len(told) == n_cmds
            inbox = sorted((Path(s["data_dir"]) / "inbox").glob("*.md"))
            assert len(inbox) == n_cmds and "untrusted" in inbox[0].read_text() \
                and "reply (" in inbox[0].read_text(), inbox
            # the bot's own replies are in the chat now: the next pass answers nothing
            assert scan(s, hooks, db_path=dbp, out=quiet) == [] and len(sent) == n_cmds, "a bot answered itself"
            assert all(PEER not in json.dumps(f, ensure_ascii=False) for _, f in logged), "a handle in the log"

    # the backstops, in the 1:1 thread: the hourly cap with one "slowing down" line, the run cap
    s = {**fixture_settings("dm", tmp / "caps"), "per_hour": 2}
    sent, logged, told = [], [], []
    dbp = fresh("caps")
    hooks = e2e_hooks(dbp, dm, sent, logged, told, sign)
    scan(s, hooks, db_path=dbp, out=quiet)
    w = sqlite3.connect(dbp)
    for i in range(4):
        add_row(w, dm["rowid"], PEER, f"ai: number {i}")
    w.commit()
    scan(s, hooks, db_path=dbp, out=quiet)
    kinds = [k for k, _ in logged]
    assert kinds.count("link.reply") == 2 and kinds.count("link.capped") == 2, kinds
    assert len(sent) == 3 and "my limit" in sent[2][1], "one slowing line, not one per command"
    w.close()
    for access in ACCESS:
        s = fixture_settings("dm", tmp / f"run-{access}", access)
        sent, logged, told = [], [], []
        dbp = fresh(f"run-{access}")
        w = sqlite3.connect(dbp)
        hooks = e2e_hooks(dbp, dm, sent, logged, told, sign)
        scan(s, hooks, db_path=dbp, out=quiet)
        for i in range(7):              # command, reply, command, reply… with no plain line
            add_row(w, dm["rowid"], "me", f"ai: again {i}")
            # the friend's bot answering too: a bot's line, never a human one, nor ours
            add_row(w, dm["rowid"], PEER, f"Also {i}.\n" + SIGNOFF)
            w.commit()
            scan(s, hooks, db_path=dbp, out=quiet)
        kinds = [k for k, _ in logged]
        # ignored or not, the run builds the same: their bot's lines are signed, and a sign-off
        # is a needle, so SQL tells them from a person's line without a row being decoded.
        # Counting them as plain let an unsigned self-loop stop at PER_HOUR, never at RUN_CAP.
        assert kinds.count("link.reply") == 6 and kinds.count("link.stopped") == 1, (access, kinds)
        add_row(w, dm["rowid"], "me", "ok, thanks", "blob")     # a plain human line resumes it
        add_row(w, dm["rowid"], "me", "ai: one more")
        w.commit()
        scan(s, hooks, db_path=dbp, out=quiet)
        assert [k for k, _ in logged].count("link.reply") == 7, "a human line did not reset the run"
        w.close()

    # (a) letting the friend in answers nothing of theirs from before: while they were ignored
    # the watermark moved over my own rows only, so their `ai:` lines since are not a backlog.
    # My own command waiting in the same gap is still answered — the watermark does not jump.
    ign = fixture_settings("dm", tmp / "access", "ignore")
    mus = {**ign, "peer_access": "musician"}
    sent, logged, told = [], [], []
    dbp = fresh("access")
    hooks = e2e_hooks(dbp, dm, sent, logged, told, sign)
    scan(ign, hooks, db_path=dbp, out=quiet)                    # the first pass: the watermark
    w = sqlite3.connect(dbp)
    add_row(w, dm["rowid"], "me", "ai: mine")
    for i in range(3):
        add_row(w, dm["rowid"], PEER, f"ai: theirs {i}")
    w.commit()
    scan(ign, hooks, db_path=dbp, out=quiet)                    # answers mine, never sees theirs
    assert len(sent) == 1, sent
    add_row(w, dm["rowid"], "me", "ai: mine too")               # both waiting when access goes up
    add_row(w, dm["rowid"], PEER, "ai: theirs 3")
    w.commit()
    probe = open_db(dbp)
    last = newest(probe, dm["guid"])                            # the chat as it stands now
    done = scan(mus, hooks, db_path=dbp, out=quiet)
    assert [c["who"] for c in done] == ["me"] and len(sent) == 2, (done, sent)
    assert load_state(mus)["peer_from"] == last, "the floor for their rows"
    assert [f["was"] for k, f in logged if k == "link.access"] == ["ignore"], logged
    add_row(w, dm["rowid"], PEER, "ai: now")                    # theirs, after the switch
    w.commit()
    assert [c["who"] for c in scan(mus, hooks, db_path=dbp, out=quiet)] == ["peer"], "theirs dropped"
    assert len(sent) == 3 and scan(mus, hooks, db_path=dbp, out=quiet) == [], sent
    assert [k for k, _ in logged].count("link.access") == 1, "the access change was logged twice"
    # a state file written before the `access` key: an install already on `musician` must widen
    # once at its first pass after the update, not answer what stands in the chat
    st = json.loads(state_path(mus).read_text())
    del st["access"], st["peer_from"]
    local_write(state_path(mus), json.dumps(st))
    add_row(w, dm["rowid"], PEER, "ai: from before the update")
    w.commit()
    assert scan(mus, hooks, db_path=dbp, out=quiet) == [], "an old state file answered their backlog"
    assert load_state(mus)["access"] == "musician" and len(sent) == 3, load_state(mus)
    probe.close()
    w.close()

    # (c) a refusal at start-up: a wait is for macOS withholding Full Disk Access, and nothing
    # else — a chmod EPERM on a root-owned log.jsonl would wait for a grant already given
    eperm = PermissionError(errno.EPERM, "Operation not permitted", "log.jsonl")
    eacces = PermissionError(errno.EACCES, "Permission denied", "log.jsonl")
    locked = tmp / "locked.db"
    locked.write_text("not a db")
    os.chmod(locked, 0)
    assert fda_missing(dbp) is False, "a readable chat.db read as no Full Disk Access"
    assert fda_missing(tmp / "gone.db") is False, "a missing chat.db read as a grant to wait for"
    assert fda_missing(locked) is False, "EACCES read as a refusal by TCC"
    # chat.db's own EPERM waits even when the probe now reads it: the grant landing between the
    # two is the 30 s this job retries, and exit 0 would end it for good
    race = PermissionError(errno.EPERM, 'Operation not permitted', str(dbp))
    assert start_fail('chat.db', race, dbp)[0] == WAITING, 'chat.db EPERM exited instead of waiting'
    for what, e, db_ in (("settings", eperm, dbp), ("settings", eacces, dbp),
                         ("chat.db", eperm, tmp / "gone.db")):
        code, lines = start_fail(what, e, db_)
        assert code == 0 and f"FAIL {what}" in lines[0] and lines[-1].startswith(NOT_STARTING), \
            (code, lines)
    real_fda = polyrhythm.fda_missing
    polyrhythm.fda_missing = lambda *a: True        # what TCC looks like, without TCC
    try:
        code, lines = start_fail("chat.db", eperm, dbp)
    finally:
        polyrhythm.fda_missing = real_fda
    assert code == WAITING and len(lines) == 1 and "no Full Disk Access" in lines[0], (code, lines)
    assert "not starting" in (BUNDLE / "install.sh").read_text(), \
        "install.sh no longer stops waiting on the word a job that exited 0 prints"

    # a reply hook that fails or answers nothing: filed, never sent
    s = fixture_settings("dm", tmp / "fail")
    sent, logged, told = [], [], []
    dbp = fresh("fail")
    w = sqlite3.connect(dbp)
    hooks = {**e2e_hooks(dbp, dm, sent, logged, told, sign), "reply": lambda c: 1 / 0}
    scan(s, hooks, db_path=dbp, out=quiet)
    add_row(w, dm["rowid"], PEER, "ai: divide")
    w.commit()
    scan(s, hooks, db_path=dbp, out=quiet)
    hooks["reply"] = lambda c: "AI:"
    add_row(w, dm["rowid"], PEER, "ai: say nothing")
    w.commit()
    scan(s, hooks, db_path=dbp, out=quiet)
    assert not sent and [k for k, _ in logged].count("link.noanswer") == 2, logged
    w.close()

    # the sign-off survives what a keyboard or Messages does to it; a reply cannot smuggle the trigger
    for variant in (SIGNOFF.replace("🎵", "🎵️"), SIGNOFF.replace(" ", " "),
                    SIGNOFF.replace("·", "•"), SIGNOFF + "​", SIGNOFF.split(" ", 1)[1]):
        assert is_bot("ok\n" + variant), f"sign-off variant missed: {variant!r}"
    assert tidy(OBJ + "ai: x") == "x" and tidy("​AI: x") == "x", "tidy let the trigger through"
    assert tidy("⁦ai: x") == "x" and tidy("ai: ​ai: x") == "x", "tidy: Cf in front"

    # the hourly cap counts model calls: answers that cannot be sent still spend them
    s = {**fixture_settings("dm", tmp / "spend"), "per_hour": 2}
    sent, logged, told, calls = [], [], [], []
    dbp = fresh("spend")
    hooks = {**e2e_hooks(dbp, dm, sent, logged, told, sign), "reply": lambda c: calls.append(c) or "ai:"}
    scan(s, hooks, db_path=dbp, out=quiet)
    w = sqlite3.connect(dbp)
    for i in range(6):
        add_row(w, dm["rowid"], PEER, f"ai: spend {i}")
    w.commit()
    w.close()
    scan(s, hooks, db_path=dbp, out=quiet)
    assert len(calls) == 2, f"{len(calls)} model calls under a cap of 2"

    # an old command (a leftover state.json, a late sync) is filed, never answered
    s = {**fixture_settings("dm", tmp / "old"), "max_age": MAX_AGE}
    sent, logged, told = [], [], []
    dbp = fresh("old")
    scan(s, e2e_hooks(dbp, dm, sent, logged, told, sign), db_path=dbp, from_rowid=0, out=quiet)
    n_old = sum(1 for r in FIXTURE if r[1] == "dm" and r[7])
    assert not sent and [k for k, _ in logged].count("link.stale") == n_old, logged

    # an RCS peer: chat.db records the reply as an RCS row and the read-back finds it, so the
    # next pass answers nothing (rule 1 over RCS rows is the fixture's dm_rcs_signed)
    s = {**fixture_settings("dm", tmp / "rcs"), "peer_service": "RCS"}
    sent, logged, told = [], [], []
    dbp = fresh("rcs")
    hooks = e2e_hooks(dbp, dm, sent, logged, told, sign, service="RCS")
    scan(s, hooks, db_path=dbp, out=quiet)
    w = sqlite3.connect(dbp)
    add_row(w, dm["rowid"], PEER, "ai: an RCS question", service="RCS")
    w.commit()
    w.close()
    scan(s, hooks, db_path=dbp, out=quiet)
    assert len(sent) == 1 and [f["delivered"] for k, f in logged if k == "link.reply"] == ["sent"], logged
    assert scan(s, hooks, db_path=dbp, out=quiet) == [] and len(sent) == 1, "an RCS reply answered"
    assert buddy_script("RCS").count("service type = RCS") == 1 and "{service}" not in buddy_script("iMessage")
    for svc, peer, ok in (("rcs", PEER, "RCS"), ("", PEER, "iMessage"), ("  ", PEER, "iMessage"),
                          ("SMS", PEER, None), ("RCS; do shell script", PEER, None),
                          ("RCS", "bob@example.com", None), ("iMessage", "bob@example.com", "iMessage")):
        try:
            got = env_settings({"POLYRHYTHM_PEER": peer, "POLYRHYTHM_PEER_SERVICE": svc})["peer_service"]
        except ConfigError:
            got = None
        assert got == ok, f"POLYRHYTHM_PEER_SERVICE={svc!r} for {peer}: {got!r}"

    # the send read-back: only `error` is failed, is_sent still proves a send, and every verdict
    # carries the flags it rests on. The first live run (2026-10-06) logged `failed (error 0)`
    # for three replies the owner was reading in the chat — the row was finished before is_sent
    # flipped. So: an errored row is failed at once; a row whose is_sent arrives by the next look
    # is sent (the loop has to look twice — collapse it and this case breaks); one that stays
    # finished-and-unsent is `finished`, not failed, and not a whole `wait` spent waiting.
    real_sleep = polyrhythm.time.sleep
    for name, extra, state, flip in (("errored", {"is_sent": 0, "error": 1}, "failed", False),
                                     ("late", {"is_sent": 0}, "sent", True),
                                     ("unsent", {"is_sent": 0}, "finished", False)):
        s = {**fixture_settings("dm", tmp / name), "send_wait": 6}
        sent, logged, told = [], [], []
        dbp = fresh(name)
        hooks = e2e_hooks(dbp, dm, sent, logged, told, sign)

        def half_send(c, text, dbp=dbp, extra=extra, sent=sent):   # as Messages would, bar is_sent
            sent.append((c["guid"], text))
            w = sqlite3.connect(dbp)
            add_row(w, c["rowid"], "me", text, "blob", **extra)
            w.commit()
            w.close()

        def late(_, dbp=dbp, flip=flip):     # Messages marks the newest row sent between looks
            if flip:
                w = sqlite3.connect(dbp)
                w.execute("update message set is_sent = 1 where ROWID = (select max(ROWID) from message)")
                w.commit()
                w.close()
        hooks["send"] = half_send
        polyrhythm.time.sleep = late         # no test waits on the wall clock
        try:
            assert scan(s, hooks, db_path=dbp, from_rowid=0, out=quiet), f"{name}: nothing answered"
        finally:
            polyrhythm.time.sleep = real_sleep
        replies = [f for k, f in logged if k == "link.reply"]
        assert replies and all(f["delivered"] == state for f in replies), (name, state, logged)
        assert all(PEER not in json.dumps(f, ensure_ascii=False) for _, f in logged), "a handle in the log"
        if state == "sent":
            assert all(f["why"] == "" for f in replies), replies
            continue
        want = f"is_sent 0, is_finished 1, error {extra.get('error', 0)}"
        assert all(want in f["why"] for f in replies), (want, replies)
        # every file that holds a reply, not sorted()[0]: the names sort by rowid as text
        filed = [p.read_text() for p in (Path(s["data_dir"]) / "inbox").glob("*.md")]
        blocks = [t for t in filed if "reply (" in t]
        assert len(blocks) == len(replies), (len(blocks), len(replies))
        assert all(f"{state}: " in t and want in t for t in blocks), blocks[0]

    # the send path, osascript recorded: a 1:1 goes by the peer's service, a group's fallback by
    # the chat's own service_name from chat.db; the text and the handle only ever in argv
    calls, real_osa = [], polyrhythm.osascript

    def recorder(script, *argv, timeout=30):
        calls.append((script, argv))
        return ("error", "no such chat") if len(calls) == 1 and "chat id" in script else (None, None)
    polyrhythm.osascript = recorder
    try:
        for svc in SERVICES:
            calls.clear()
            s = {**fixture_settings("dm", tmp / f"send-{svc}"), "peer_service": svc}
            local_hooks(s, "cat", out=quiet)["send"](dm, "hi; do shell script \"x\"")
            (script, argv), = calls
            assert f"service type = {svc})" in script and argv == ("hi; do shell script \"x\"", PEER), calls
        w = sqlite3.connect(fresh("rcs-group"))
        w.execute("update chat set service_name = 'RCS' where ROWID = ?", (grp["rowid"],))
        w.commit()
        rcs_grp = resolve_chat(w, PEER, "Bots", "group")
        w.close()
        calls.clear()
        s = fixture_settings("group", tmp / "send-group")      # the peer's setting says iMessage
        local_hooks(s, "cat", out=quiet)["send"](rcs_grp, "hi")
        assert [a for _, a in calls] == [("hi", "any;+;chat111"), ("hi", "RCS;+;chat111")], calls
    finally:
        polyrhythm.osascript = real_osa

    # a peer under two handles (iMessage and SMS rows for one number) is one member
    dbp = fresh("handles")
    w = sqlite3.connect(dbp)
    sms = w.execute("select ROWID from handle where id = ? and service = 'SMS'", (PEER,)).fetchone()[0]
    w.execute("insert into chat_handle_join values (?, ?)", (dm["rowid"], sms))
    w.commit()
    assert resolve_chat(w, PEER, None, "dm")["rowid"] == dm["rowid"], "two handles, no 1:1 thread"
    w.close()

    # settings: the friend's handle is the one required; access is ignore unless set otherwise
    for env, ok in (({}, False), ({"POLYRHYTHM_PEER": PEER}, True),
                    ({"POLYRHYTHM_PEER": "+1 (555) 000-0003"}, True),
                    ({"POLYRHYTHM_PEER": "Friend@Example.com"}, True),
                    ({"POLYRHYTHM_PEER": "5550000003"}, False),
                    ({"POLYRHYTHM_PEER": PEER, "POLYRHYTHM_PEER_ACCESS": "musician"}, True),
                    ({"POLYRHYTHM_PEER": PEER, "POLYRHYTHM_PEER_ACCESS": "instrument"}, False),
                    ({"POLYRHYTHM_PEER": PEER, "POLYRHYTHM_PEER_ACCESS": "everyone"}, False),
                    ({"POLYRHYTHM_PEER": PEER, "POLYRHYTHM_MODE": "group"}, True)):
        try:
            got = env_settings(env)
        except ConfigError:
            got = None
        assert bool(got) == ok, f"env_settings with {env}: {'took' if got else 'refused'} it"
    assert handle_of("\u202d+1 (555) 000\u20110003\u202c") == PEER, "a number copied from Contacts"
    assert handle_of("+44 (0) 20 7946 0958") == "+442079460958", "the trunk 0 kept"
    got = env_settings({"POLYRHYTHM_PEER": "+1 (555) 000-0003"})
    assert got["peer_handle"] == PEER and got["peer_access"] == "ignore", got
    assert unused_keys({"POLYRHYTHM_PEER": PEER, "POLYRHYTHM_NAME": "x", "HOME": "/"}) == ["POLYRHYTHM_NAME"]
    assert unused_keys(parse_env((BUNDLE / ".env.example").read_text())) == [], ".env.example: a key check calls unused"

    # setup writes the handle in place: other lines kept, a symlinked file written through
    tpl, real_file, link = tmp / "example", tmp / "vault" / "settings", tmp / "settings-link"
    tpl.write_text("# keep me\nPOLYRHYTHM_PEER=\nPOLYRHYTHM_RUN_CAP=6\n")
    real_file.parent.mkdir()
    link.symlink_to(real_file)
    set_env(link, {"POLYRHYTHM_PEER": PEER}, template=tpl)
    assert link.is_symlink() and parse_env(real_file.read_text()) == {"POLYRHYTHM_PEER": PEER, "POLYRHYTHM_RUN_CAP": "6"}
    set_env(link, {"POLYRHYTHM_PEER": OTHER, "POLYRHYTHM_MODE": "group"})
    text = real_file.read_text()
    assert link.is_symlink() and text.startswith("# keep me\n") and text.count("POLYRHYTHM_PEER=") == 1, text
    assert parse_env(text)["POLYRHYTHM_PEER"] == OTHER and parse_env(text)["POLYRHYTHM_MODE"] == "group", text
    real_file.write_text(text + "POLYRHYTHM_PEER=+15550000004\n")     # a key twice, by hand
    set_env(link, {"POLYRHYTHM_PEER": PEER})
    assert parse_env(real_file.read_text())["POLYRHYTHM_PEER"] == PEER \
        and real_file.read_text().count("POLYRHYTHM_PEER=") == 1, "a doubled key kept its old value"
    assert real_file.stat().st_mode & 0o077 == 0, "the settings file is readable by others"
    try:
        set_env(link, {"POLYRHYTHM_CHAT_NAME": "Bots\nPOLYRHYTHM_REPLY=evil"})
        raise AssertionError("a value with a newline set another key")
    except ConfigError:
        pass

    # the setup command: the service is always written, like the mode, and RCS to an email is
    # refused before the file changes
    real_env, polyrhythm.ENV_FILE = polyrhythm.ENV_FILE, tmp / "setup-env"
    try:
        def setup(handle, service="iMessage", group=None):
            with contextlib.redirect_stdout(io.StringIO()):     # its "settings: …" line
                cmd_setup(SimpleNamespace(handle=handle, group=group, service=service, dry_run=False))
            return parse_env(polyrhythm.ENV_FILE.read_text())
        assert setup("+1 (555) 000-0003", "RCS")["POLYRHYTHM_PEER_SERVICE"] == "RCS"
        got = setup(PEER, group="Bots")
        assert (got["POLYRHYTHM_PEER_SERVICE"], got["POLYRHYTHM_MODE"]) == ("iMessage", "group"), got
        try:
            setup("bob@example.com", "RCS")
            raise AssertionError("setup wrote RCS to an email")
        except ConfigError:
            pass
        assert parse_env(polyrhythm.ENV_FILE.read_text())["POLYRHYTHM_PEER"] == PEER, "a refused setup wrote"
        try:
            setup(PEER, "RCS", group="Bots")
            raise AssertionError("setup wrote RCS for a group, which sends over its own service")
        except ConfigError:
            pass
        # a .env linked to a target not there yet (a cloud drive not mounted at login) waits,
        # never exits 0 into a job launchd will not restart
        # (the guard alone: calling cmd_run here would be a live bot the day the guard moves);
        # setup refuses to write a fresh file at the missing target
        polyrhythm.ENV_FILE = tmp / "dangling-env"
        polyrhythm.ENV_FILE.symlink_to(tmp / "not-mounted" / "polyrhythm.env")
        assert polyrhythm.env_link_missing(), "a dangling .env link read as no .env"
        try:
            setup(PEER)
            raise AssertionError("setup wrote through a dangling .env link")
        except ConfigError:
            pass
        assert not (tmp / "not-mounted").exists(), "setup made the missing target's folder"
    finally:
        polyrhythm.ENV_FILE = real_env

    # one pass at a time per data folder
    s = fixture_settings("dm", tmp / "lock")
    with pass_lock(s) as first:
        with pass_lock(s) as second:
            assert first and not second, "two passes held one data folder"

    # dry: nothing written, nothing sent, nothing logged
    s = fixture_settings("dm", tmp / "dry")
    sent, logged, told, lines = [], [], [], []
    dbp = fresh("dry")
    scan(s, e2e_hooks(dbp, dm, sent, logged, told, sign), dry=True, db_path=dbp, from_rowid=0,
         out=lines.append)
    assert not sent and not logged and not Path(s["data_dir"]).exists(), "dry wrote or sent"
    assert any(l.startswith("   would send:") for l in lines), lines
    db.close()
    return ("polyrhythm: decoder, chat resolution, the boundary (dm pre-filter: "
            f"{len(got_dm)} of {n_dm} rows left SQLite, {len(mine_dm)} with the friend ignored; "
            "other chats: none), rules 1-3, both modes and both accesses end to end, hourly and "
            "run caps, no-answer, sign-off variants, stale commands, RCS, two handles, the send "
            "read-back, letting the friend in, the start-up refusal, settings and setup, the "
            "pass lock, dry")


def e2e_hooks(dbp, chat, sent, logged, told, sign, service="iMessage"):
    """Stubs for the self-test: the reply echoes, the send writes the signed row as Messages
    would (is_sent = 1), the log and the notify are recorded."""
    def send(c, text):
        sent.append((c["guid"], text))
        w = sqlite3.connect(dbp)
        add_row(w, c["rowid"], "me", text, "blob", service=service)
        w.commit()
        w.close()
    return {"reply": lambda c: f"echo {c['cmd']}", "sign": sign, "send": send,
            "log": lambda kind, **f: logged.append((kind, f)), "write": local_write,
            "notify": lambda c, dry: told.append(c)}


if __name__ == "__main__":
    print(self_test())
