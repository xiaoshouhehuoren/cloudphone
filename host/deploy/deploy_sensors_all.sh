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
DATA_ROOT="${G98_DATA_ROOT:-/srv/redroid}"
fail=0
for c in $(docker ps --format '{{.Names}}' | grep -E '^wx[0-9]+$'); do
  printf '== %s ' "$c"
  # rc 以 host/g98-sensors.rc 为准（artifacts/ 被 .gitignore 忽略，里面的旧版可能还是
  # "on boot start"，会让假 HAL 把 system_server 卡在启动阶段）。
  RC="$HERE/../g98-sensors.rc"
  [ -f "$RC" ] || RC="$ART/g98-sensors.rc"
  docker cp "$ART/g98-sensors.dex" "$c:/vendor/etc/g98-sensors.dex" >/dev/null 2>&1 || { echo "cp dex failed"; fail=1; continue; }
  docker cp "$RC" "$c:/vendor/etc/init/g98-sensors.rc" >/dev/null 2>&1 || { echo "cp rc failed"; fail=1; continue; }
  docker cp "$HERE/../g98-sensors.sh" "$c:/vendor/bin/g98-sensors.sh" >/dev/null 2>&1 || { echo "cp sh failed"; fail=1; continue; }
  if [ -f "$ART/g98_sensors_trace.bin.gz" ]; then
    docker cp "$ART/g98_sensors_trace.bin.gz" "$c:/vendor/etc/g98_sensors_trace.bin.gz" >/dev/null 2>&1 || echo -n "(trace cp failed) "
  fi
  docker cp "$START" "$c:/data/local/tmp/g98-sensors-start.sh" >/dev/null 2>&1 || { echo "cp start failed"; fail=1; continue; }
  # 每台独立的传感器随机种子：HAL 用它决定 trace 相位、各轴偏置与光线缩放，
  # 这样五台回放同一份真机 trace 也不会完全一致。容器 /data = 宿主 $DATA_ROOT/$c。
  mkdir -p "$DATA_ROOT/$c/local/tmp" 2>/dev/null || true
  printf '%s' "$c" > "$DATA_ROOT/$c/local/tmp/g98-sensors-seed.txt" 2>/dev/null || true
  chmod 644 "$DATA_ROOT/$c/local/tmp/g98-sensors-seed.txt" 2>/dev/null || true
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