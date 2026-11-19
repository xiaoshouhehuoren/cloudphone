#!/bin/sh
# 云手机微信控制小工具（在板子上跑：sh /root/wx.sh <cmd>）
# 依赖 docker exec；redroid 里 monkey 已损坏，用 am start。
set -e
C=${C:-m6}
PKG=com.tencent.mm
case "$1" in
  start) docker exec $C am start -n $PKG/.ui.LauncherUI ;;
  stop)  docker exec $C am force-stop $PKG ;;
  shot)  docker exec $C screencap -p /data/local/tmp/wx.png; docker cp $C:/data/local/tmp/wx.png /tmp/wx.png; echo /tmp/wx.png ;;
  tap)   docker exec $C input tap "$2" "$3" ;;
  swipe) docker exec $C input swipe "$2" "$3" "$4" "$5" ${6:-300} ;;
  text)  docker exec $C input text "$2" ;;
  key)   docker exec $C input keyevent "$2" ;;
  top)   docker exec $C dumpsys activity activities | grep -m1 topResumedActivity ;;
  *) echo 'usage: wx.sh start|stop|shot|tap x y|swipe x1 y1 x2 y2 [ms]|text <str>|key <code>|top' ;;
esac