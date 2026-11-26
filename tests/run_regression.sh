#!/bin/bash
# Camera + injection regression suite. Run on the board as root.
set -uo pipefail
cd "$(dirname "$0")"
FAIL=0
say(){ printf '\n=== %s ===\n' "$*"; }

say "systemd units"
systemctl is-active g98-camd g98-mediamtx g98-hub g98-netup || FAIL=1

say "camd health"
/usr/bin/curl -sf http://127.0.0.1:8100/health || { echo "camd health FAILED"; FAIL=1; }

say "node char devices"
for n in $(seq 20 31); do
    [ -c "/dev/video$n" ] || { echo "/dev/video$n not a char device"; FAIL=1; }
done

say "dashboard material library API"
/usr/bin/curl -sf -H 'Cookie: g98k=g98panel' http://127.0.0.1:8099/api/cam/files >/dev/null \
    || { echo "files API FAILED"; FAIL=1; }

say "camera end-to-end (wx1..wx5)"
python3 verify_camera_e2e.py "$@" || FAIL=1

say "result"
[ "$FAIL" = 0 ] && echo "REGRESSION PASS" || echo "REGRESSION FAIL"
exit "$FAIL"
