#!/bin/bash
# Give every wxN container a soft memory ceiling so a runaway page cache /
# WeChat update cannot push the whole board into reclaim stalls.
#
# memory.high is the "throttle and reclaim" line - the container keeps working,
# the kernel just drops its page cache first. memory.max is the hard line that
# would OOM-kill; it is set well above the observed working set on purpose.
#
# Defaults: soft 2048MB, hard 3072MB (observed real usage 1.4-1.8GB per
# instance with WeChat + camera stack). Override with
# G98_MEM_HIGH_MB / G98_MEM_MAX_MB.
set -u
HIGH="${G98_MEM_HIGH_MB:-2048}"
MAX="${G98_MEM_MAX_MB:-3072}"
DOCKER=/usr/bin/docker
[ -x "$DOCKER" ] || DOCKER=docker

cgroup_of() {
    id=$1
    for p in /sys/fs/cgroup/system.slice/docker-$id.scope \
             /sys/fs/cgroup/docker/$id \
             /sys/fs/cgroup/system.slice/docker-$id*.scope; do
        [ -d "$p" ] && { echo "$p"; return 0; }
    done
    return 1
}

applied=0
for c in $("$DOCKER" ps --format '{{.Names}}' 2>/dev/null | grep -E '^wx[0-9]+$'); do
    id=$("$DOCKER" inspect -f '{{.Id}}' "$c" 2>/dev/null) || continue
    p=$(cgroup_of "$id") || { echo "g98-memlimit: no cgroup for $c"; continue; }
    ok=0
    if [ -w "$p/memory.high" ]; then
        echo "${HIGH}M" > "$p/memory.high" 2>/dev/null && ok=1
    fi
    if [ -w "$p/memory.max" ]; then
        echo "${MAX}M" > "$p/memory.max" 2>/dev/null
    fi
    cur=$(cat "$p/memory.current" 2>/dev/null)
    echo "g98-memlimit: $c soft=${HIGH}M hard=${MAX}M now=$((cur/1024/1024))M ok=$ok"
    applied=$((applied+ok))
done
echo "g98-memlimit: applied=$applied"
exit 0
