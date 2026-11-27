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
ensure_sensors() {
  # 基础镜像没有传感器栈时，从板子上的 artifacts 补齐再重启一次：
  # dex + init rc + 启动器 + 真机 SENC trace + VINTF 声明。
  # 缺 VINTF 声明时 SensorService 会忽略已注册的 AIDL HAL（devInitCheck -19）。
  local A=/root/artifacts
  local DR="${G98_DATA_ROOT:-/srv/redroid}/$C"
  [ -f "$A/g98-sensors.dex" ] || return 0
  # 每台一个随机种子文件：HAL 读它决定 trace 相位偏移、各轴偏置与光线缩放，
  # 避免五台回放同一份真机 trace 完全一致。缺文件时 HAL 会退回序列号/固定值。
  mkdir -p "$DR/local/tmp" 2>/dev/null || true
  [ -s "$DR/local/tmp/g98-sensors-seed.txt" ] || printf '%s' "$C" > "$DR/local/tmp/g98-sensors-seed.txt" 2>/dev/null || true
  if docker exec "$C" test -f /vendor/etc/g98-sensors.dex 2>/dev/null; then
    # 已装过的实例也刷新 init rc：新版 rc 把启动时机从 "on boot" 改成
    # "on property:sys.boot_completed=1"，否则假 HAL 会在开机阶段把 system_server 卡住。
    docker cp "$A/g98-sensors.rc" "$C:/vendor/etc/init/g98-sensors.rc" >/dev/null 2>&1 || true
    docker cp "$A/g98-sensors.sh" "$C:/vendor/bin/g98-sensors.sh" >/dev/null 2>&1 || true
    return 0
  fi
  docker cp "$A/g98-sensors.dex" "$C:/vendor/etc/g98-sensors.dex" >/dev/null 2>&1 || return 0
  docker cp "$A/g98-sensors.rc" "$C:/vendor/etc/init/g98-sensors.rc" >/dev/null 2>&1 || true
  docker cp "$A/g98-sensors.sh" "$C:/vendor/bin/g98-sensors.sh" >/dev/null 2>&1 || true
  [ -f "$A/g98_sensors_trace.bin.gz" ] && docker cp "$A/g98_sensors_trace.bin.gz" "$C:/vendor/etc/g98_sensors_trace.bin.gz" >/dev/null 2>&1 || true
  [ -f "$A/vintf-manifest.xml" ] && docker cp "$A/vintf-manifest.xml" "$C:/vendor/etc/vintf/manifest.xml" >/dev/null 2>&1 || true
  # 只保留 sensors 的 AIDL 声明：声明了 gnss 却没人提供 IGnss 时 servicemanager 会
  # 每秒重试，system_server 卡在启动，boot_completed 永远为空。
  docker exec "$C" sh -c "if [ -f /vendor/etc/vintf/manifest.xml ] && grep -q android.hardware.gnss /vendor/etc/vintf/manifest.xml; then grep -v android.hardware.gnss /vendor/etc/vintf/manifest.xml > /data/local/tmp/m.xml && cat /data/local/tmp/m.xml > /vendor/etc/vintf/manifest.xml; fi" >/dev/null 2>&1 || true
  echo "$C: sensor stack installed, restarting to load it" >> /var/log/g98-wx-start.log
  docker restart "$C" >/dev/null 2>&1 || true
  local i
  for i in $(seq 1 60); do
    [ "$(docker exec "$C" getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && break
    sleep 2
  done
}

configure_camera() {
  # 每台云手机独占一对 v4l2loopback 节点：wx1=video20/21，wx2=22/23 … wx6=30/31。
  # 以前 5 台容器共用 video10/11，任何一台开相机都会和别人抢同一条 v4l2 队列。
  local BP=$((20 + 2 * (N - 1))) FP=$((BP + 1))
  local BN=/dev/video$BP FN=/dev/video$FP
  if ! systemctl is-active --quiet g98-camd; then
    systemctl start g98-camidle.service >/dev/null 2>&1 || true
  fi
  echo 0 > /sys/module/v4l2loopback/parameters/debug 2>/dev/null || true
  for d in "$BN" "$FN"; do [ -e "$d" ] && chmod 666 "$d" || true; done
  # Wait for the producer to lock 640x480 YU12 before setting keep_format.
  for i in $(seq 1 20); do
    if grep -q 'YU12:' "/sys/devices/virtual/video4linux/video$BP/format" 2>/dev/null; then
      break
    fi
    sleep 0.5
  done
  /root/keepf "$BN" 1 >/dev/null 2>&1 || true
  /root/keepf "$FN" 1 >/dev/null 2>&1 || true
  # The container gets its own /dev nodes at mode 0660 root:video; the camera
  # HAL runs unprivileged and then fails with "failed to open /dev/videoXX
  # (Permission denied)", so the provider enumerates 0 cameras after a reboot.
  docker exec "$C" chmod 666 "$BN" "$FN" >/dev/null 2>&1 || true
  # HAL 按该属性决定用哪一对节点（见 v4l2_camera_hal.cpp 的 vendor.camera.redroid.nodes）
  docker exec "$C" setprop vendor.camera.redroid.nodes "$BN,$FN" >/dev/null 2>&1 || true
  docker exec "$C" setprop ctl.stop vendor.camera-provider-2-7-ext >/dev/null 2>&1 || true
  # The legacy provider must be restarted once after keep_format.  A plain
  # ctl.start leaves a stale HAL and Camera2 fails preview stream setup.
  docker exec "$C" setprop ctl.restart vendor.camera-provider-2-4-legacy >/dev/null 2>&1 || true
}
geo_default() {
  # 默认定位：跟随板子当前所在位置（出口 IP 城市），人为改过就保留人为坐标。
  token=$(cat /etc/g98/cloudphone.token 2>/dev/null || echo "")
  [ -n "$token" ] || return 0
  for i in $(seq 1 60); do
    st=$(docker exec "$C" getprop sys.boot_completed 2>/dev/null | tr -d '\r')
    [ "$st" = "1" ] && break
    sleep 2
  done
  if docker exec "$C" test -f /data/local/tmp/g98_gnss_manual 2>/dev/null; then
    echo "$C geo: manual override kept" >> /var/log/g98-geo.log
    return 0
  fi
  out=$(curl -s -m 12 -X POST -H "Content-Type: application/json" -H "Cookie: g98k=$token" -d "{\"name\":\"$C\"}" http://127.0.0.1:8099/api/location/self 2>/dev/null)
  echo "$(date '+%F %T') $C geo-default $out" >> /var/log/g98-geo.log
}

start_touch() {
  if [ -f /etc/systemd/system/g98-wx-touchd@.service ]; then
    systemctl start --no-block "g98-wx-touchd@$N.service" >/dev/null 2>&1 || true
  fi
}

# Keep WeChat and the salespartner probe components alive across Doze/standby.
# Must never force-stop or restart the container here.
# Keep logd from being flooded by camera/v4l2 driver printk spam.
mute_noisy_logs() {
  for tag in V4L2Wrapper V4L2MetadataFactory V4L2Camera Metadata Camera; do
    docker exec "$C" setprop log.tag.$tag SUPPRESS >/dev/null 2>&1 || true
  done
  docker exec "$C" sh -c 'echo 1 > /proc/sys/kernel/printk' >/dev/null 2>&1 || true
}

harden_keepalive() {
  for p in com.tencent.mm com.salespartner.workphone.agent com.salespartner.workphone.wechatprobe; do
    docker exec "$C" cmd deviceidle whitelist +"$p" >/dev/null 2>&1 || true
    docker exec "$C" cmd appops set "$p" RUN_IN_BACKGROUND allow >/dev/null 2>&1 || true
    docker exec "$C" cmd appops set "$p" RUN_ANY_IN_BACKGROUND allow >/dev/null 2>&1 || true
    docker exec "$C" am set-standby-bucket "$p" active >/dev/null 2>&1 || true
  done
  docker exec "$C" settings put global settings_enable_monitor_phantom_procs false >/dev/null 2>&1 || true
  docker exec "$C" device_config put activity_manager max_phantom_processes 2147483647 >/dev/null 2>&1 || true
  docker exec "$C" device_config put activity_manager_native_boot max_phantom_processes 2147483647 >/dev/null 2>&1 || true
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
harden_keepalive
mute_noisy_logs
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
ensure_sensors
configure_camera
start_touch
sleep 2
docker exec "$C" am start -n com.tencent.mm/.ui.LauncherUI >/dev/null 2>&1 || true
( geo_default ) >/dev/null 2>&1 &
echo "$C ready"