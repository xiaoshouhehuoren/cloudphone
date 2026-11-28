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

say "camera providers (legacy running, external crash loop disabled)"
python3 verify_camera_providers.py || FAIL=1

say "sensor stack (fake HAL, 10 h/w sensors per instance)"
python3 verify_sensors.py || FAIL=1

say "legacy camera API1 size lists (media_profiles VideoEncoderCap vs 1280x960 nodes)"
python3 verify_api1_sizes.py || FAIL=1

say "soft AVC encoder accepts NV12 byte buffers (WeChat face video)"
python3 verify_nv12_encoder.py wx1 || FAIL=1

say "phone sensor sync (ws -> live file -> HAL)"
python3 verify_sensor_sync.py wx1 || FAIL=1

say "legacy API1 preview at 640x480 (HAL scales native capture down)"
python3 verify_api1_preview.py wx1 || FAIL=1

say "camd injection looks like a real sensor (grain + AE breathing)"
python3 verify_texture.py || FAIL=1

say "rotated injection must not inherit the legacy ystretch compensation"
python3 verify_orient_default.py || FAIL=1

say "live phone injection is clean (no synthetic grain/AE/fps resampling)"
python3 verify_live_clean.py /root/g98-camd.py || FAIL=1

say "camd holds last frame when the rtsp source is silent"
python3 verify_hold.py || FAIL=1

say "camera end-to-end (wx1..wx5)"
python3 verify_camera_e2e.py "$@" || FAIL=1

say "geometry chain (source -> node -> stream -> screen)"
GEOM_MEDIA="${GEOM_MEDIA:-/root/camin/1795581891716.mp4}"
if [ -f "$GEOM_MEDIA" ]; then
    python3 verify_geometry.py wx1 /dev/video20 "$GEOM_MEDIA" contain 1.7778 || FAIL=1
    python3 verify_geometry.py wx1 /dev/video20 "$GEOM_MEDIA" cover 1.7778 || FAIL=1
else
    echo "skip: $GEOM_MEDIA not found"
fi
/usr/bin/curl -s -X POST http://127.0.0.1:8100/set -H 'Content-Type: application/json' \
    -d '{"node":"/dev/video20","kind":"idle"}' >/dev/null

say "result"
[ "$FAIL" = 0 ] && echo "REGRESSION PASS" || echo "REGRESSION FAIL"
exit "$FAIL"