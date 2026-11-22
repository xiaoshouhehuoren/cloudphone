#!/bin/bash
# Keep a native proxy attached to the current netns of a container.
set -u
C="$1"
if [ -z "$C" ]; then
  echo "usage: $0 <container> [tcp-port] [dns-port] [upstream-port]" >&2
  exit 2
fi
TCP="$2"
if [ -z "$TCP" ]; then TCP=19000; fi
DNS="$3"
if [ -z "$DNS" ]; then DNS=19053; fi
UP="$4"
if [ -z "$UP" ]; then UP=18081; fi
while true; do
  PID=$(docker inspect -f '{{.State.Pid}}' "$C" 2>/dev/null || true)
  if [ -z "$PID" ] || [ "$PID" = "0" ]; then
    sleep 2
    continue
  fi
  if ! /srv/redroid/native-proxy-iptables.sh "$C" "$TCP" "$DNS"; then
    sleep 2
    continue
  fi
  /usr/bin/nsenter -t "$PID" -n /usr/bin/python3 /srv/redroid/g98-native-proxy.py \
    --listen "127.0.0.1:$TCP" \
    --dns-listen "127.0.0.1:$DNS" \
    --upstream "172.17.0.1:$UP" \
    --dns-upstream 8.8.8.8:53
  sleep 2
done
