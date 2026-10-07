#!/bin/sh
# Set polyrhythm up, or update it: the settings, the launcher, the launchd job, then the two
# macOS grants. Anchored on its own folder, so it runs from anywhere; the job runs
# polyrhythm.py from here.
#   sh install.sh +15550000003    the first time: your friend's phone, or Apple ID email
#   sh install.sh                 again, after a pull: rebuild if needed, restart the job
#   sh install.sh … --group NAME  the group chat named NAME, not your 1:1 thread
#   sh install.sh +1… --rcs       reply to your friend over RCS, not iMessage (a phone only)
#   sh install.sh --check-only    say what it would do; change nothing
#   sh install.sh --remove        unload the job, delete the plist and the launcher
#   --wait | --no-wait            wait for the grants (default: when run from a terminal)
# Refuses while the self-test (selftest.py) fails or the settings are missing or wrong.
set -eu
mode=install
handle=
group=
service=iMessage
wait=auto
usage="usage: sh install.sh [<friend's phone or email>] [--group NAME] [--rcs] [--check-only | --remove] [--wait | --no-wait]"
while [ $# -gt 0 ]; do
  case "$1" in
    --check-only) mode=check ;;
    --remove) mode=remove ;;
    --wait) wait=yes ;;
    --no-wait) wait=no ;;
    --rcs) service=RCS ;;
    --group) [ $# -ge 2 ] || { echo "$usage" >&2; exit 2; }; group=$2; shift ;;
    -*) echo "$usage" >&2; exit 2 ;;
    *) [ -z "$handle" ] || { echo "$usage" >&2; exit 2; }; handle=$1 ;;
  esac
  shift
done
if [ "$wait" = auto ]; then
  if [ -t 0 ] && [ -t 1 ]; then wait=yes; else wait=no; fi
fi
dir="$(cd "$(dirname "$0")" && pwd)"
case "$dir" in *\"*|*\\*) echo "this folder's path contains a quote or a backslash: move it" >&2; exit 2 ;; esac
bin="$HOME/.local/bin/polyrhythm-host"
src="$dir/polyrhythm-host.c"
label=dev.220labs.polyrhythm
old_label=org.polyrhythm            # before 2026-10-05; same launcher path, so it must not run beside
old_dest="$HOME/Library/LaunchAgents/$old_label.plist"
dest="$HOME/Library/LaunchAgents/$label.plist"
stamp="$HOME/.local/share/polyrhythm-host.sha"
log="$HOME/Library/Logs/polyrhythm.log"
settings="$dir/.env"

if [ "$mode" = remove ]; then
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootout "gui/$(id -u)/$old_label" 2>/dev/null || true
  rm -f "$dest" "$old_dest" "$bin" "$stamp"
  echo "removed the job, $dest and $bin."
  echo "Kept: the inbox, state.json and log.jsonl (in POLYRHYTHM_DIR, this folder by default),"
  echo "  and $log. Delete them, and this folder, by hand if you are done."
  echo "By hand: remove polyrhythm-host from Full Disk Access and Automation in System Settings."
  exit 0
fi

# Command Line Tools first: cc builds the launcher, and /usr/bin/python3 is theirs too (without
# them that shim pops an installer and fails in its own words)
if ! xcode-select -p >/dev/null 2>&1; then
  echo "compiler: no Command Line Tools — run: xcode-select --install, then this again"
  exit 1
fi
# the friend's handle first, so the check below reads the settings it wrote
if [ -n "$handle" ]; then
  set -- "$handle"
  if [ -n "$group" ]; then set -- "$@" --group "$group"; fi
  set -- "$@" --service "$service"
  if [ "$mode" = check ]; then set -- "$@" --dry-run; fi
  /usr/bin/python3 -I "$dir/polyrhythm.py" setup "$@" || exit 2
elif [ -n "$group" ] || [ "$service" != iMessage ]; then
  echo "--group and --rcs go with your friend's handle: sh install.sh <phone or email> [--group NAME] [--rcs]" >&2
  exit 2
fi

ok=1
# --check-only with a handle: check the settings as setup would write them
if [ "$mode" = check ] && [ -n "$handle" ]; then
  export POLYRHYTHM_PEER="$handle" POLYRHYTHM_MODE=dm POLYRHYTHM_PEER_SERVICE="$service"
  if [ -n "$group" ]; then export POLYRHYTHM_MODE=group POLYRHYTHM_CHAT_NAME="$group"; fi
fi
if out=$(/usr/bin/python3 -I "$dir/polyrhythm.py" check 2>&1); then
  printf '%s\n' "$out" | sed 's/^/check: /'
else
  printf '%s\n' "$out" | tail -8 | sed 's/^/check: /'
  ok=0
fi
if [ -f "$old_dest" ]; then
  echo "old job: $old_dest (label $old_label) — the install unloads and deletes it"
fi
if [ ! -e "$settings" ] && ! { [ "$mode" = check ] && [ -n "$handle" ]; }; then
  echo ".env: missing — run: sh install.sh <your friend's phone (+1…) or Apple ID email>"
  ok=0
fi
if [ "$ok" = 0 ] && [ "$mode" = install ]; then
  echo "not installing: fix the lines above and run it again"
  exit 1
fi

# the script's path is compiled in, so a moved folder is a new build too
want=$( (cat "$src"; printf '%s' "$dir/polyrhythm.py") | shasum -a 256 | cut -d' ' -f1)
have=$(cat "$stamp" 2>/dev/null || true)
if [ "$want" != "$have" ] || [ ! -x "$bin" ]; then
  echo "polyrhythm-host: needs a build (source or folder changed, or missing) → $bin"
  if [ "$mode" = install ]; then
    mkdir -p "$(dirname "$bin")" "$(dirname "$stamp")"
    cc -O2 -Wall -DSCRIPT="\"$dir/polyrhythm.py\"" -o "$bin" "$src"
    codesign -s - -f -i dev.220labs.polyrhythm.host "$bin"
    echo "$want" > "$stamp"
    echo "polyrhythm-host: built — a new binary to macOS, so it needs both grants again."
  fi
else
  echo "polyrhythm-host: up to date"
fi

# PATH for the job: the reply hook's `claude` lives wherever Claude Code put it
path="/usr/bin:/bin:/usr/sbin:/sbin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"
if claude=$(command -v claude 2>/dev/null); then
  case ":$path:" in *":$(dirname "$claude"):"*) ;; *) path="$path:$(dirname "$claude")" ;; esac
else
  echo "claude: not on PATH — the default reply hook needs Claude Code (or set POLYRHYTHM_REPLY)"
fi
rendered=$(/usr/bin/python3 -I - "$dir/$label.plist" "$bin" "$path" "$log" <<'PY'
import sys
from xml.sax.saxutils import escape
t = open(sys.argv[1]).read()
for k, v in zip(("__HOST__", "__PATH__", "__LOG__"), sys.argv[2:]):
    t = t.replace(k, escape(v))
sys.stdout.write(t)
PY
)
if [ -f "$dest" ] && [ "$(cat "$dest")" = "$rendered" ]; then
  echo "plist: in sync"
else
  echo "plist: differs → $dest"
fi
# --check-only fails when a real install would refuse, so a script can rely on it
if [ "$mode" = check ]; then [ "$ok" = 1 ] && exit 0; exit 1; fi

mkdir -p "$(dirname "$dest")" "$(dirname "$log")"
launchctl bootout "gui/$(id -u)/$old_label" 2>/dev/null || true
rm -f "$old_dest"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
printf '%s\n' "$rendered" > "$dest"
since=$( { wc -c < "$log"; } 2>/dev/null || echo 0)
launchctl bootstrap "gui/$(id -u)" "$dest"
echo "loaded $label — log: $log"

# The grants. The job reads chat.db first; without Full Disk Access it says `wait chat.db`,
# exits, and launchd starts it again every 30 s — so switching the grant on is the whole step.
# Then its first Apple Event raises the Messages prompt. These are its start-up lines
# (polyrhythm.py, cmd_run): this install's own, never an older run's.
fresh() { tail -c +"$((since + 1))" "$log" 2>/dev/null || true; }
startup() { fresh | grep -E '^   (ok|wait|FAIL) +(chat\.db|messages|settings):|^polyrhythm: (listening|not starting)' || true; }
i=0
while [ $i -lt 20 ] && [ -z "$(startup)" ]; do sleep 1; i=$((i + 1)); done
if startup | grep -qE 'not starting|^   FAIL settings'; then  # the job exited 0: no retry
  startup | sed 's/^/job: /'
  exit 1
fi
shown=no
show_grants() {          # once, whenever the job first says it waits — a slow first start included
  [ "$shown" = no ] || return 0
  startup | grep -q "^   wait .*no Full Disk Access" || return 0
  shown=yes
  echo
  echo "Full Disk Access — System Settings opens on the list, and Finder shows polyrhythm-host:"
  echo "  drag polyrhythm-host into the list and switch it on."
  echo "  (Or: + in the list, ⇧⌘G, type ~/.local/bin/polyrhythm-host, Open.)"
  # the pane's URL has held since macOS 13; if it opens elsewhere, the line above still holds
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
  open -R "$bin" 2>/dev/null || true
  echo "  Already in the list from an older build? Remove it (−) and add it again: a rebuild is a"
  echo "  new binary to macOS, and the old entry no longer counts."
  echo "Then macOS asks whether polyrhythm-host may control Messages: Allow."
}
show_grants
if [ "$wait" = no ]; then
  echo "Watch it: tail -f $log — ready when it says \`polyrhythm: listening\`."
  exit 0
fi
echo "waiting for the grants (up to 10 minutes; Ctrl-C stops the wait, not the job)…"
i=0
while [ $i -lt 300 ] && ! startup | grep -q '^polyrhythm: listening'; do show_grants; sleep 2; i=$((i + 1)); done
last=$(fresh | awk '/^polyrhythm: .* mode,/ { block = "" } { block = block $0 "\n" } END { printf "%s", block }' \
  | grep -E '^   (ok|FAIL) ' || true)
printf '%s\n' "$last" | sed '/^$/d; s/^/job: /'
if ! startup | grep -q '^polyrhythm: listening'; then
  echo "not ready after 10 minutes: Full Disk Access is still off (see the lines above)."
  exit 1
fi
if printf '%s\n' "$last" | grep -q 'FAIL messages: RuntimeError: no RCS account'; then
  echo "Messages on this Mac has no RCS account: sh $dir/install.sh <your friend's phone> without"
  echo "  --rcs replies over iMessage"
  exit 1
fi
if printf '%s\n' "$last" | grep -q 'FAIL messages: RuntimeError: no iMessage account'; then
  echo "Messages on this Mac is not signed in to iMessage: Messages → Settings → iMessage, sign in;"
  echo "  then: sh $dir/install.sh"
  exit 1
fi
if printf '%s\n' "$last" | grep -q 'FAIL messages'; then
  echo "Messages refused: System Settings → Privacy & Security → Automation → polyrhythm-host → Messages, on;"
  echo "  then: launchctl kickstart -k gui/$(id -u)/$label"
  exit 1
fi
if printf '%s\n' "$last" | grep -q 'FAIL chat.db'; then
  echo "chat.db: the job's FAIL line above says why — a wrong handle (it lists the chats it saw: then"
  echo "  sh $dir/install.sh <your friend's phone or email>), or no Messages database yet"
  exit 1
fi
echo "ready: type \`ai: hello\` in the chat."
