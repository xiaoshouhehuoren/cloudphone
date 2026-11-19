#!/bin/bash
# 微信控制最小脚本：启动、截图、点击
set -u
C=${1:-m6}
case ${2:-start} in
  start)  docker exec $C am start -n com.tencent.mm/.ui.LauncherUI ;;
  shot)   docker exec $C screencap -p /data/local/tmp/wx.png && docker cp $C:/data/local/tmp/wx.png ./wx.png && echo ./wx.png ;;
  focus)  docker exec $C dumpsys window | grep -m1 mCurrentFocus ;;
  tap)    docker exec $C input tap "${3:-360}" "${4:-640}" ;;
  back)   docker exec $C input keyevent 4 ;;
  *) echo "usage: wx.sh <container> start|shot|focus|tap x y|back"; exit 1 ;;
esac
