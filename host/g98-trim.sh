#!/bin/bash
# Hourly housekeeping: give the page cache back to the kernel and trim ART
# caches inside the containers. Cheap (a few seconds), keeps ~2GB available.
set -u
DOCKER=/usr/bin/docker
[ -x "$DOCKER" ] || DOCKER=docker

for c in $("$DOCKER" ps --format '{{.Names}}' 2>/dev/null | grep -E '^wx[0-9]+$'); do
    "$DOCKER" exec "$c" pm trim-caches 256M >/dev/null 2>&1 && echo "trim $c ok"
done
sync
echo 1 > /proc/sys/vm/drop_caches 2>/dev/null || true
free -m | sed -n 2p
