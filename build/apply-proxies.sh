#!/bin/bash
# Re-apply every container -> proxy mapping from /srv/redroid/proxies.conf.
set -u
CONF=/srv/redroid/proxies.conf
while read -r C P _; do
  case "$C" in
    ""|\#*) continue ;;
  esac
  /srv/redroid/container-proxy.sh "$C" "$P" || true
done < "$CONF"
