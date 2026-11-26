#!/bin/bash
# g98-camd adopt: camd 先占住节点时，已经在跑的 provider 下次 open 会报
# "Could not open camera session: Device or resource busy (-16)"，必须重启一次
# provider 才能接手。顺带把每个容器的节点属性指向自己那一对。
set -u
sleep 3
for c in $(/usr/bin/docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^wx[0-9]+$'); do
  n=${c#wx}
  bp=$((20 + 2 * (n - 1)))
  fp=$((bp + 1))
  /usr/bin/docker exec "$c" setprop vendor.camera.redroid.nodes "/dev/video$bp,/dev/video$fp" >/dev/null 2>&1 || true
  /usr/bin/docker exec "$c" chmod 666 "/dev/video$bp" "/dev/video$fp" >/dev/null 2>&1 || true
  # 残留的 Camera error 对话框会让下一次打开立刻失败，先清掉相机 App。
  /usr/bin/docker exec "$c" am force-stop com.android.camera2 >/dev/null 2>&1 || true
  # 顺序和间隔都很重要：provider 和 cameraserver 同时重启会让 HAL 在服务尚未
  # 注册时被调用，留下 "Camera error" 对话框，并在 5 秒后触发 CameraServiceWatchdog。
  /usr/bin/docker exec "$c" setprop ctl.restart vendor.camera-provider-2-4-legacy >/dev/null 2>&1 || true
  sleep 2
  /usr/bin/docker exec "$c" setprop ctl.restart cameraserver >/dev/null 2>&1 || true
  sleep 1
done
exit 0
