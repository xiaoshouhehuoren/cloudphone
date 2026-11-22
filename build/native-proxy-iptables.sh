#!/bin/bash
# Install per-container transparent redirect rules in the container netns.
set -u
C="$1"
if [ -z "$C" ]; then
  echo "usage: $0 <container> [tcp-port] [dns-port]" >&2
  exit 2
fi
TCP="$2"
if [ -z "$TCP" ]; then TCP=19000; fi
DNS="$3"
if [ -z "$DNS" ]; then DNS=19053; fi
PID=$(docker inspect -f '{{.State.Pid}}' "$C" 2>/dev/null || true)
if [ -z "$PID" ] || [ "$PID" = "0" ]; then
  echo "container $C is not running" >&2
  exit 1
fi
ipt() {
  nsenter -t "$PID" -n iptables -t nat "$@"
}
ipt -N G98_TPROXY 2>/dev/null || true
ipt -F G98_TPROXY
ipt -A G98_TPROXY -d 127.0.0.0/8 -j RETURN
ipt -A G98_TPROXY -d 172.17.0.0/16 -j RETURN
ipt -A G98_TPROXY -d 169.254.0.0/16 -j RETURN
ipt -A G98_TPROXY -p udp --dport 53 -j REDIRECT --to-ports "$DNS"
ipt -A G98_TPROXY -p tcp -j REDIRECT --to-ports "$TCP"
while ipt -D OUTPUT -j G98_TPROXY 2>/dev/null; do :; done
ipt -I OUTPUT 1 -j G98_TPROXY
echo "native redirect installed for $C tcp=$TCP dns=$DNS"
