#!/bin/bash
# 守护所有 wx* 容器的出网配置（容器重启/PID 变化后自动重做）
LOG=/var/log/g98-netup.log
while true; do
  for c in $(docker ps --format '{{.Names}}' 2>/dev/null | grep -E '^wx[0-9]+$'); do
    PID=$(docker inspect "$c" --format '{{.State.Pid}}' 2>/dev/null)
    [ -z "$PID" ] && continue
    PF=/run/g98-netup-$c.pid
    if [ -f "$PF" ] && [ "$(cat "$PF" 2>/dev/null)" = "$PID" ]; then continue; fi
    echo "$(date -Is) reconfig $c pid=$PID" >> $LOG
    /root/g98-netup.sh "$c" >> $LOG 2>&1
    echo "$PID" > "$PF"
  done
  sleep 20
done
