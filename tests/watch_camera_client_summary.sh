#!/bin/bash
# watch_camera_client_summary.sh <outdir> - distill a capture into summary.txt
set -u
OUT=${1:?usage: watch_camera_client_summary.sh <outdir>}
L="$OUT/logcat.all.txt"
S="$OUT/summary.txt"
{
  echo "== capture: $OUT =="
  date
  echo
  echo "-- camera connects / disconnects --"
  grep -aE 'CameraService::connect|Camera2ClientBase: Camera .* (Opened|Disconnected)|CameraService: disconnect' "$L" | tail -20
  echo
  echo "-- stream configuration --"
  grep -aE 'setupStreams|configureStreams|Capturing at native' "$L" | tail -12
  echo
  echo "-- fatal camera errors --"
  grep -aE 'generated preview size list is empty|unable to build defaults|connectHelper|Requested preview size' "$L" | tail -12
  echo
  echo "-- HAL errors --"
  grep -aE ' E V4L2Camera| E Camera |timestamp shouldn' "$L" | tail -12
  echo
  echo "-- client SDK (youtu / face) --"
  grep -aE 'youtu|YTAG|net_fetch|FaceFlash|action seq|globalInit|globalRelease|UFA::Init' "$L" | tail -40
  echo
  echo "-- injection health --"
  tail -6 "$OUT/camd.log" 2>/dev/null
  echo "frames captured: $(ls "$OUT"/frame_*.png 2>/dev/null | wc -l)"
  echo "screens captured: $(ls "$OUT"/screen_*.png 2>/dev/null | wc -l)"
} > "$S"
cat "$S"
