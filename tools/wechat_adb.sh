#!/bin/sh
# 云手机微信控制（adb over bridge IP）
# 用法：sh wechat_adb.sh <容器名> <命令> [参数...]
#   ip | start | stop | focus | ui | shot <名字> | tap X Y | tap-text "文字" | text "字符串" | key N | wait N
set -e
C=${1:-m6}; shift || true
IP=$(docker inspect -f '{{.NetworkSettings.IPAddress}}' "$C")
A="adb -s $IP:5560"
T=/data/local/tmp/ui.xml

case "${1:-}" in
  ip)   echo "$IP:5560" ;;
  start) $A shell am start -n com.tencent.mm/.ui.LauncherUI >/dev/null; echo started ;;
  stop)  $A shell am force-stop com.tencent.mm; echo stopped ;;
  focus) $A shell dumpsys window | grep -m2 -E 'mCurrentFocus|mFocusedApp' ;;
  ui)    $A shell uiautomator dump $T >/dev/null 2>&1; $A shell cat $T > /tmp/ui.xml; echo /tmp/ui.xml ;;
  shot)  $A exec-out screencap -p > "/tmp/${2:-shot}.png"; ls -l "/tmp/${2:-shot}.png" ;;
  tap)   $A shell input tap "$2" "$3" ;;
  tap-text)
         $A shell uiautomator dump $T >/dev/null 2>&1
         $A shell cat $T > /tmp/ui.xml
         XY=$(python3 /root/ui_find.py /tmp/ui.xml "$2")
         echo "hit $2 -> $XY"
         $A shell input tap $XY ;;
  text)  $A shell input text "$2" ;;
  key)   $A shell input keyevent "$2" ;;
  wait)  sleep "${2:-3}" ;;
  *) echo "usage: wechat_adb.sh <container> ip|start|stop|focus|ui|shot|tap X Y|tap-text TEXT|text STR|key N|wait N" ;;
esac
