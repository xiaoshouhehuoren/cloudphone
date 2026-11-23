#!/bin/sh
# 常驻“空转”相机源：没有面板注入时给 /dev/video10|11 一个合法流，
# 否则 Camera HAL 报 "Format not set, so stream can't be on"，相机 App 直接 "Can't connect to the camera"。
FF=/root/ffmpeg
PNG=/root/camidle.png
[ -f "$PNG" ] || $FF -hide_banner -loglevel error -f lavfi -i "color=c=0x0b1220:s=640x480" -frames:v 1 -y "$PNG"
ensure() {
  node="$1"
  busy=$(pgrep -af "v4l2 $node" | grep -v camidle.sh | head -1)
  if [ -n "$busy" ]; then
    /root/keepf "$node" 1 >/dev/null 2>&1      # 任何 producer 在写，都保证 keep_format
    return
  fi
  nohup $FF -hide_banner -loglevel error -re -loop 1 -i "$PNG" -pix_fmt yuv420p         -r 10 -s 640x480 -f v4l2 "$node" >>/var/log/camidle.log 2>&1 &
  sleep 0.6
  /root/keepf "$node" 1 >/dev/null 2>&1
}
while true; do
  ensure /dev/video10
  ensure /dev/video11
  sleep 5
done
