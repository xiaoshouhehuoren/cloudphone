#!/bin/bash
# Apply the per-container HTTP proxy and default route.
# Usage: container-proxy.sh <container> [port] [proxy-host]
# If port is omitted, read it from /srv/redroid/proxies.conf.
set -uo pipefail
C=$1
if [ -z "$C" ]; then
  echo "usage: container-proxy.sh <container> [port] [proxy-host]" >&2
  exit 1
fi
CONF=/srv/redroid/proxies.conf
if [ "$#" -ge 2 ]; then
  PORT=$2
else
  PORT=$(awk -v c="$C" '$1==c {print $2; exit}' "$CONF" 2>/dev/null)
fi
if [ "$#" -ge 3 ]; then
  HOST=$3
else
  HOST=172.17.0.1
fi
if [ -z "$PORT" ]; then
  echo "no proxy port configured for $C" >&2
  exit 1
fi
# Docker bridge gateway; the container needs this before it can reach the LAN proxy.
docker exec "$C" ip route replace default via 172.17.0.1 dev eth0
docker exec "$C" settings put global http_proxy "$HOST:$PORT"
docker exec "$C" settings put global global_http_proxy_host "$HOST" 2>/dev/null || true
docker exec "$C" settings put global global_http_proxy_port "$PORT" 2>/dev/null || true
# Verify the proxy TCP endpoint from inside the container.
if docker exec "$C" /system/bin/toybox nc -w 3 "$HOST" "$PORT" </dev/null >/dev/null 2>&1; then
  echo "proxy $HOST:$PORT ready for $C"
else
  echo "WARN: proxy $HOST:$PORT not reachable from $C" >&2
  exit 2
fi
