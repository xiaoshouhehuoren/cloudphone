#!/bin/bash
set -u
C=m7
RUN=$(docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null || echo false)
if [ "$RUN" != "true" ]; then
  logger -t g98-m7-watch "m7 not running ($RUN), restarting g98-m7.service"
  systemctl restart g98-m7.service
  exit 0
fi
# repair the container proxy route/settings if Android or Docker reset them
ROUTE=$(docker exec "$C" ip route 2>/dev/null | grep -c '^default ' || true)
PROXY=$(docker exec "$C" settings get global http_proxy 2>/dev/null | tr -d '\r' || true)
if [ "$ROUTE" -eq 0 ] || [ "$PROXY" != "172.17.0.1:18081" ]; then
  logger -t g98-m7-watch "m7 proxy state missing (route=$ROUTE proxy=$PROXY), repairing"
  /srv/redroid/container-proxy.sh "$C" 18081 172.17.0.1 >/dev/null 2>&1 || true
fi