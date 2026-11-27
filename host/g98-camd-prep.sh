#!/bin/bash
# g98-camd prep: make sure the per-instance v4l2loopback nodes exist as real
# character devices with a pinned YU12 640x480 format.  camd writes raw frames
# with plain write(); the format must already be YU12 or the Android side
# decodes raw YUV as JPEG (torn green/purple output).
set -u
NODES="${G98_CAMD_NODES:-/dev/video20,/dev/video21,/dev/video22,/dev/video23,/dev/video24,/dev/video25,/dev/video26,/dev/video27,/dev/video28,/dev/video29}"

# 1. Make sure the module owns the 20..31 range.  Reusing a module loaded with
#    the old 10..15 layout silently gives us no per-instance nodes, so check the
#    configured video_nr list instead of just "is the module loaded".
want=20,21,22,23,24,25,26,27,28,29,30,31
have=$(cat /sys/module/v4l2loopback/parameters/video_nr 2>/dev/null | head -c 120)
case "$have" in
  $want*) ;;
  *)
    if lsmod | grep -q '^v4l2loopback'; then
      /sbin/rmmod v4l2loopback 2>/dev/null || true
      sleep 0.5
    fi
    /sbin/modprobe v4l2loopback devices=12 video_nr=$want exclusive_caps=1 max_buffers=8 2>/dev/null || true
    sleep 1
    ;;
esac

# 2. Repair /dev nodes.  If a writer ever ran while the module was unloaded it
#    created regular files at these paths; devtmpfs then cannot replace them and
#    every later frame is appended to a huge file in /dev instead of a camera.
for n in 20 21 22 23 24 25 26 27 28 29 30 31; do
  sysfs=/sys/class/video4linux/video$n
  [ -e "$sysfs/dev" ] || continue
  if [ ! -c "/dev/video$n" ]; then
    rm -f "/dev/video$n"
    mj=$(cut -d: -f1 "$sysfs/dev")
    mn=$(cut -d: -f2 "$sysfs/dev")
    mknod "/dev/video$n" c "$mj" "$mn" 2>/dev/null || true
    echo "g98-camd-prep: repaired /dev/video$n ($mj:$mn)"
  fi
  # devtmpfs 新建的节点默认 0600：容器里（privileged，共享宿主 /dev）打开会 EACCES，
  # 表现就是 Camera 卡在 starting、e2e 超时。每次 camd 启动都统一放开。
  chmod 666 "/dev/video$n" 2>/dev/null || true
done

systemctl stop g98-camidle 2>/dev/null || true
systemctl disable g98-camidle 2>/dev/null || true

IFS=","
for node in $NODES; do
  [ -c "$node" ] || { echo "g98-camd-prep: skip $node (not a char device)"; continue; }
  fmt=$(cat /sys/devices/virtual/video4linux/$(basename "$node")/format 2>/dev/null || true)
  case "$fmt" in
    YU12:640x480*) ;;
    *)
      # One short ffmpeg run does VIDIOC_S_FMT with YU12/640x480; keep_format then holds it.
      timeout 3 /root/ffmpeg -hide_banner -loglevel error -re -f lavfi \
        -i testsrc2=size=640x480:rate=10 -vf format=yuv420p -t 0.5 -f v4l2 "$node" >/dev/null 2>&1 || true
      ;;
  esac
  /root/keepf "$node" 1 >/dev/null 2>&1 || true
done
exit 0
