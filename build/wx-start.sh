#!/bin/bash
set -euo pipefail
N=${1:?usage: wx-start.sh <1-6>}
C=wx$N
CONF=/srv/redroid/wx-identities.conf
IMG=g98/redroid-panthor-rt:13-fuxi-v3
# Application-facing product profile only. Keep the existing base image, display
# settings and mt6983 HAL ABI selector unchanged for both profiles.
PROFILE=${G98_DEVICE_PROFILE:-fuxi}
W=1080; H=2400; DPI=440; FPS=30
MANUFACTURER=Xiaomi
case "$PROFILE" in
  fuxi)
    MODEL=2211133C; DEVICE=fuxi; NAME=fuxi; FLAVOR=fuxi-user
    BRAND=Xiaomi
    FP=Xiaomi/fuxi/fuxi:13/TKQ1.221114.001/V14.0.9.0.TMCCNXM:user/release-keys
    ;;
  k50)
    MODEL=22041211AC; DEVICE=rubens; NAME=rubens; FLAVOR=rubens-user
    BRAND=Redmi
    # The supplied dump identifies mihal, while the requested device profile is
    # rubens. Never invent a fingerprint or mix fragments from different dumps.
    FP=${G98_K50_FINGERPRINT:-}
    if [ -z "$FP" ]; then
      echo "G98_DEVICE_PROFILE=k50 requires G98_K50_FINGERPRINT copied verbatim from an official stock build.prop; refusing to guess rubens/mihal" >&2
      exit 1
    fi
    if [[ ! "$FP" =~ ^[^/[:space:]]+/[^/[:space:]]+/[^[:space:]]+:13/[^/[:space:]]+/[^[:space:]]+:user/release-keys$ ]]; then
      echo "invalid G98_K50_FINGERPRINT: expected brand/device/product:13/build-id/incremental:user/release-keys" >&2
      exit 1
    fi
    ;;
  *)
    echo "unsupported G98_DEVICE_PROFILE=$PROFILE (expected fuxi or k50)" >&2
    exit 1
    ;;
esac
read -r S M P A < <(awk -v c="$C" '$1==c {print $2, $3, $4, $5}' "$CONF")
[ -n "${S:-}" ] && [ -n "${A:-}" ] || { echo "no identity for $C" >&2; exit 1; }
create() {
  mkdir -p "/srv/redroid/$C"
  # 内存上限：软线由 g98-memlimit.timer 每 5 分钟写 memory.high=2G；
  # 这里给 docker 一个硬上界（3G + 1G swap），防止单实例把整板拖进 OOM。
  docker run -d --name "$C" --hostname "$C" --privileged --security-opt label=disable --cap-add SYS_NICE --entrypoint /init --mac-address "$M" -v "/srv/redroid/$C:/data" --device /dev/dri --memory 3g --memory-swap 4g "$IMG" \
    qemu=1 androidboot.hardware=mt6983 \
    androidboot.redroid_width="$W" androidboot.redroid_height="$H" androidboot.redroid_dpi="$DPI" androidboot.redroid_fps="$FPS" \
    androidboot.verifiedbootstate=green androidboot.flash.locked=1 androidboot.veritymode=enforcing androidboot.selinux=enforcing \
    ro.hardware.gralloc=g98 ro.hw_timeout_multiplier=10 \
    ro.product.model="$MODEL" ro.product.brand="$BRAND" ro.product.manufacturer="$MANUFACTURER" ro.product.device="$DEVICE" ro.product.name="$NAME" \
    ro.build.flavor="$FLAVOR" ro.build.product="$DEVICE" ro.build.type=user ro.build.tags=release-keys \
    ro.serialno="$S" ro.boot.serialno="$S" ro.build.fingerprint="$FP" >/dev/null
}
ensure_excluded_hardware() {
  local XML=/root/artifacts/g98-excluded-hardware.xml
  local DST=/system/etc/permissions/g98-excluded-hardware.xml
  [ -f "$XML" ] || { echo "$C: missing $XML" >&2; return 1; }
  # PackageManager reads feature XML during boot. If this instance was already
  # booted, the next normal boot picks up a changed file; never force a restart.
  docker cp "$XML" "$C:$DST.new" >/dev/null
  docker exec "$C" sh -c '
    set -e
    dst=$1
    if [ -f "$dst" ] && cmp -s "$dst.new" "$dst"; then
      rm -f "$dst.new"
    else
      chmod 644 "$dst.new"
      mv -f "$dst.new" "$dst"
    fi
    chmod 644 "$dst"
  ' sh "$DST"
}

ensure_regional_settings() {
  local WANT_TZ=Asia/Shanghai WANT_LOCALE=zh-CN CURRENT
  CURRENT=$(docker exec "$C" getprop persist.sys.timezone 2>/dev/null | tr -d '\r' || true)
  if [ "$CURRENT" != "$WANT_TZ" ]; then
    docker exec "$C" setprop persist.sys.timezone "$WANT_TZ"
    docker exec "$C" settings put global auto_time_zone 0 >/dev/null 2>&1 || true
    docker exec "$C" service call alarm 3 s16 "$WANT_TZ" >/dev/null 2>&1 || true
  fi
  CURRENT=$(docker exec "$C" getprop persist.sys.locale 2>/dev/null | tr -d '\r' || true)
  [ "$CURRENT" = "$WANT_LOCALE" ] || docker exec "$C" setprop persist.sys.locale "$WANT_LOCALE"
  CURRENT=$(docker exec "$C" settings get global system_locales 2>/dev/null | tr -d '\r' || true)
  [ "$CURRENT" = "$WANT_LOCALE" ] || docker exec "$C" settings put global system_locales "$WANT_LOCALE"
}

ensure_adbkeyboard() {
  # 容器是 docker run 重建的，装过的 IME 会丢：从 /srv/redroid/apk 补装。
  # 只安装 + enable，不改默认输入法（改了 WeChat 侧容易看出被接管）。
  local APK="${G98_APK_DIR:-/srv/redroid/apk}/ADBKeyboard.apk"
  docker exec "$C" ime list -s 2>/dev/null | grep -q adbkeyboard && return 0
  [ -f "$APK" ] || return 0
  docker cp "$APK" "$C:/data/local/tmp/ADBKeyboard.apk" >/dev/null 2>&1 || return 0
  docker exec "$C" pm install -r /data/local/tmp/ADBKeyboard.apk >/dev/null 2>&1 || return 0
  docker exec "$C" ime enable com.android.adbkeyboard/.AdbIME >/dev/null 2>&1 || true
  echo "adbkeyboard installed for $C"
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
    # 已装过的实例也刷新 init rc / 启动器（rc 里是 "on boot start" + boot_completed
    # 幂等兜底：只用 boot_completed 会与 system_server 启动时同步等 ISensors 互相死锁）。
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

ensure_media_profiles() {
  # 旧版相机 API（API1：微信人脸验证、扫码）的预览/录像尺寸上限来自
  # media_profiles 的 <VideoEncoderCap maxFrameWidth/maxFrameHeight>；原厂是 1280x720，
  # 小于我们的 1280x960 节点 → getFilteredSizes 返回空 → "generated preview size list
  # is empty!!" (-22) → App 看到 Failed to initialize camera。补丁版放宽到 1920x1440。
  local A=/root/artifacts
  [ -f "$A/media_profiles_V1_0.xml" ] || return 0
  docker cp "$A/media_profiles_V1_0.xml" "$C:/vendor/etc/media_profiles_V1_0.xml" >/dev/null 2>&1 || true
  docker exec "$C" chmod 644 /vendor/etc/media_profiles_V1_0.xml >/dev/null 2>&1 || true
}

ensure_soft_avcenc() {
  # 微信人脸用 NV12 喂 OMX.google.h264.encoder；原厂 SoftAVCEnc 在 NV12 下第一次
  # ive_api_function 就失败，fdv_*.mp4 只有 262B，微信在本地判 Network error（不上传）。
  # 补丁版仅在 bytebuffer NV12 时先转 I420；I420 与 surface/meta 输入路径不变。
  # ctl.restart media.codec 在 redroid 上会被拒绝，只能 kill 后由 init 拉起。
  local A=/root/artifacts
  [ -f "$A/libstagefright_soft_avcenc.so" ] || return 0
  docker cp "$A/libstagefright_soft_avcenc.so" "$C:/vendor/lib/libstagefright_soft_avcenc.so" >/dev/null 2>&1 || true
  docker exec "$C" chmod 644 /vendor/lib/libstagefright_soft_avcenc.so >/dev/null 2>&1 || true
  docker exec "$C" sh -c 'kill $(pidof media.codec) 2>/dev/null' >/dev/null 2>&1 || true
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
ensure_excluded_hardware
B=""
for i in $(seq 1 60); do
  B=$(docker exec "$C" getprop sys.boot_completed 2>/dev/null | tr -d '\r' || true)
  [ "$B" = "1" ] && break
  sleep 3
done
[ "$B" = "1" ] || { echo "$C boot timeout" >&2; exit 1; }
ensure_regional_settings
# Harden: remove root/adb helpers and ADB keyboard; keep LatinIME for login.
docker exec "$C" sh -c 'for p in /system/xbin/su /system/bin/su /sbin/su /su/bin/su /system/sbin/su; do rm -f "$p"; done' || true
# ADBKeyboard：控制台用广播打中文（input text 只支持 ASCII）。
# 默认输入法保持 LatinIME，只有 /api/text 遇到非 ASCII 时才临时切过去再切回来，
# 因此它平时是“装但不启用”的状态。想彻底不留痕迹可设 G98_DROP_ADB_IME=1。
if [ "${G98_DROP_ADB_IME:-0}" = "1" ]; then
  docker exec "$C" pm uninstall --user 0 com.android.adbkeyboard >/dev/null 2>&1 || true
fi
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
ensure_adbkeyboard
ensure_sensors
ensure_media_profiles
ensure_soft_avcenc
configure_camera
start_touch
sleep 2
docker exec "$C" am start -n com.tencent.mm/.ui.LauncherUI >/dev/null 2>&1 || true
( geo_default ) >/dev/null 2>&1 &
echo "$C ready"