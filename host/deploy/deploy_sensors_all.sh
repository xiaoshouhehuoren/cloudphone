#!/bin/bash
# Deploy the Java sensor HAL (dex + init rc + launcher + real-device SENC trace)
# to every running redroid instance. Idempotent: safe to re-run.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ART="$(cd "$HERE/../../deploy/artifacts" 2>/dev/null && pwd || true)"
if [ -z "${ART:-}" ] || [ ! -f "$ART/g98-sensors.dex" ]; then
  echo "artifacts dir missing g98-sensors.dex (run deploy/collect_artifacts.sh first)"; exit 1
fi
START="$HERE/../g98-sensors-start.sh"
fail=0
for c in $(docker ps --format '{{.Names}}' | grep -E '^wx[0-9]+$'); do
  printf '== %s ' "$c"
  for f in g98-sensors.dex g98-sensors.rc; do :; done
  docker cp "$ART/g98-sensors.dex" "$c:/vendor/etc/g98-sensors.dex" >/dev/null 2>&1 || { echo "cp dex failed"; fail=1; continue; }
  docker cp "$ART/g98-sensors.rc" "$c:/vendor/etc/init/g98-sensors.rc" >/dev/null 2>&1 || { echo "cp rc failed"; fail=1; continue; }
  docker cp "$HERE/../g98-sensors.sh" "$c:/vendor/bin/g98-sensors.sh" >/dev/null 2>&1 || { echo "cp sh failed"; fail=1; continue; }
  if [ -f "$ART/g98_sensors_trace.bin.gz" ]; then
    docker cp "$ART/g98_sensors_trace.bin.gz" "$c:/vendor/etc/g98_sensors_trace.bin.gz" >/dev/null 2>&1 || echo -n "(trace cp failed) "
  fi
  docker cp "$START" "$c:/data/local/tmp/g98-sensors-start.sh" >/dev/null 2>&1 || { echo "cp start failed"; fail=1; continue; }
  docker cp "$HERE/../g98-vintf-patch.sh" "$c:/data/local/tmp/g98-vintf-patch.sh" >/dev/null 2>&1 || true
  docker exec "$c" /system/bin/sh /data/local/tmp/g98-vintf-patch.sh >/dev/null 2>&1 || true
  out=$(docker exec "$c" /system/bin/sh /data/local/tmp/g98-sensors-start.sh 2>&1 | tail -1)
  echo "$out"
  case "$out" in
    *fake_sensors=10*) ;;
    *) fail=1 ;;
  esac
done
[ $fail -eq 0 ] && echo "SENSORS DEPLOY OK" || echo "SENSORS DEPLOY FAILED"
exit $fail