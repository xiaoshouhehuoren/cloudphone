#!/bin/bash
set -euo pipefail
N=${1:?usage: wx-start.sh <1-6>}
C=wx$N
CONF=/srv/redroid/wx-identities.conf
IMG=g98/redroid-panthor-rt:13-mt6983-camfix-v5
read -r S M P A < <(awk -v c="$C" '$1==c {print $2, $3, $4, $5}' "$CONF")
[ -n "${S:-}" ] && [ -n "${A:-}" ] || { echo "no identity for $C" >&2; exit 1; }
create() {
  mkdir -p "/srv/redroid/$C"
  docker run -d --name "$C" --hostname "$C" --privileged --security-opt label=disable --cap-add SYS_NICE --entrypoint /init --mac-address "$M" -v "/srv/redroid/$C:/data" --device /dev/dri "$IMG" qemu=1 androidboot.hardware=mt6983 androidboot.verifiedbootstate=green androidboot.flash.locked=1 androidboot.veritymode=enforcing androidboot.selinux=enforcing ro.hardware.gralloc=g98 ro.hw_timeout_multiplier=10 ro.product.model=23127PN0CC ro.product.brand=Xiaomi ro.product.manufacturer=Xiaomi ro.product.device=houji ro.product.name=houji ro.build.type=user ro.build.tags=release-keys ro.serialno="$S" ro.boot.serialno="$S" ro.build.fingerprint=Xiaomi/houji/houji:13/TKQ1.221114.001/V14.0.9.0.TMCCNXM:user/release-keys >/dev/null
}
if docker inspect "$C" >/dev/null 2>&1; then
  if [ "$(docker inspect -f '{{.State.Running}}' "$C")" != "true" ]; then
    docker start "$C" >/dev/null 2>&1 || { docker rm -f "$C" >/dev/null 2>&1 || true; create; }
  fi
else
  create
fi
B=""
for i in $(seq 1 60); do
  B=$(docker exec "$C" getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)
  [ "$B" = "1" ] && break
  sleep 3
done
[ "$B" = "1" ] || { echo "$C boot timeout" >&2; exit 1; }
if ! docker exec "$C" pm path com.tencent.mm >/dev/null 2>&1; then
  docker exec "$C" mkdir -p /data/local/tmp
  docker cp /srv/redroid/apk/wechat879.apk "$C:/data/local/tmp/wechat.apk"
  docker exec "$C" pm install -r -g /data/local/tmp/wechat.apk >/dev/null
fi
/srv/redroid/container-proxy.sh "$C" "$P" 172.17.0.1 || echo "WARN: proxy setup failed" >&2
docker exec "$C" settings put secure android_id "$A" 2>/dev/null || true
docker exec "$C" settings put global device_name "WX$N" 2>/dev/null || true
docker exec "$C" settings put secure bluetooth_name "WX$N" 2>/dev/null || true
docker exec "$C" settings put global adb_enabled 0 2>/dev/null || true
sleep 2
docker exec "$C" am start -n com.tencent.mm/.ui.LauncherUI >/dev/null 2>&1 || true
echo "$C ready"
