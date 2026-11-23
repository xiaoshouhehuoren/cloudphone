#!/bin/bash
set -euo pipefail
N=${1:?usage: wx-start.sh <1-6>}
C=wx$N
CONF=/srv/redroid/wx-identities.conf
IMG=g98/redroid-panthor-rt:13-fuxi-v3
read -r S M P A < <(awk -v c="$C" '$1==c {print $2, $3, $4, $5}' "$CONF")
[ -n "${S:-}" ] && [ -n "${A:-}" ] || { echo "no identity for $C" >&2; exit 1; }
# Common profile: Xiaomi 13/fuxi + 1080x2400/440dpi/30fps.
W=1080; H=2400; DPI=440; FPS=30
MODEL=2211133C; DEVICE=fuxi; NAME=fuxi; FLAVOR=fuxi-user
FP=Xiaomi/fuxi/fuxi:13/TKQ1.221114.001/V14.0.9.0.TMCCNXM:user/release-keys
create() {
  mkdir -p "/srv/redroid/$C"
  docker run -d --name "$C" --hostname "$C" --privileged --security-opt label=disable --cap-add SYS_NICE --entrypoint /init --mac-address "$M" -v "/srv/redroid/$C:/data" --device /dev/dri "$IMG" \
    qemu=1 androidboot.hardware=mt6983 \
    androidboot.redroid_width="$W" androidboot.redroid_height="$H" androidboot.redroid_dpi="$DPI" androidboot.redroid_fps="$FPS" \
    androidboot.verifiedbootstate=green androidboot.flash.locked=1 androidboot.veritymode=enforcing androidboot.selinux=enforcing \
    ro.hardware.gralloc=g98 ro.hw_timeout_multiplier=10 \
    ro.product.model="$MODEL" ro.product.brand=Xiaomi ro.product.manufacturer=Xiaomi ro.product.device="$DEVICE" ro.product.name="$NAME" \
    ro.build.flavor="$FLAVOR" ro.build.product="$DEVICE" ro.build.type=user ro.build.tags=release-keys \
    ro.serialno="$S" ro.boot.serialno="$S" ro.build.fingerprint="$FP" >/dev/null
}
configure_camera() {
  systemctl start g98-camidle.service >/dev/null 2>&1 || true
  for d in /dev/video10 /dev/video11; do [ -e "$d" ] && chmod 666 "$d" || true; done
  # Wait for the idle producer to lock 640x480 YU12 before setting keep_format.
  for i in $(seq 1 20); do
    if grep -q 'YU12:' /sys/devices/virtual/video4linux/video10/format 2>/dev/null; then
      break
    fi
    sleep 0.5
  done
  /root/keepf /dev/video10 1 >/dev/null 2>&1 || true
  /root/keepf /dev/video11 1 >/dev/null 2>&1 || true
  docker exec "$C" setprop ctl.stop vendor.camera-provider-2-7-ext >/dev/null 2>&1 || true
  # The legacy provider must be restarted once after keep_format.  A plain
  # ctl.start leaves a stale HAL and Camera2 fails preview stream setup.
  docker exec "$C" setprop ctl.restart vendor.camera-provider-2-4-legacy >/dev/null 2>&1 || true
}
start_touch() {
  if [ -f /etc/systemd/system/g98-wx-touchd@.service ]; then
    systemctl start --no-block "g98-wx-touchd@$N.service" >/dev/null 2>&1 || true
  fi
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
# Harden: remove root/adb helpers and ADB keyboard; keep LatinIME for login.
docker exec "$C" sh -c 'for p in /system/xbin/su /system/bin/su /sbin/su /su/bin/su /system/sbin/su; do rm -f "$p"; done' || true
docker exec "$C" pm uninstall --user 0 com.android.adbkeyboard >/dev/null 2>&1 || true
docker exec "$C" settings put secure default_input_method com.android.inputmethod.latin/.LatinIME || true
docker exec "$C" settings put global adb_enabled 0 || true
docker exec "$C" setprop service.adb.tcp.port "" || true
docker exec "$C" setprop ctl.stop adbd || true
if ! docker exec "$C" pm path com.tencent.mm >/dev/null 2>&1; then
  docker exec "$C" mkdir -p /data/local/tmp
  docker cp /srv/redroid/apk/wechat879.apk "$C:/data/local/tmp/wechat.apk"
  docker exec "$C" pm install -r -g /data/local/tmp/wechat.apk >/dev/null
fi
# Direct egress: no Android global proxy until an upstream proxy service is provided.
docker exec "$C" ip route replace default via 172.17.0.1 dev eth0 2>/dev/null || true
docker exec "$C" settings put global http_proxy :0 2>/dev/null || true
docker exec "$C" settings delete global global_http_proxy_host 2>/dev/null || true
docker exec "$C" settings delete global global_http_proxy_port 2>/dev/null || true
docker exec "$C" settings delete global global_http_proxy_exclusion_list 2>/dev/null || true
docker exec "$C" settings put secure android_id "$A" 2>/dev/null || true
docker exec "$C" settings put global device_name "WX$N" 2>/dev/null || true
docker exec "$C" settings put secure bluetooth_name "WX$N" 2>/dev/null || true
docker exec "$C" settings put global adb_enabled 0 2>/dev/null || true
docker exec "$C" am force-stop com.android.camera2 >/dev/null 2>&1 || true
configure_camera
start_touch
sleep 2
docker exec "$C" am start -n com.tencent.mm/.ui.LauncherUI >/dev/null 2>&1 || true
echo "$C ready"
