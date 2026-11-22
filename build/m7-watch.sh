#!/bin/bash
set -e
C=m7
RUN=$(docker inspect -f '{{.State.Running}}' "$C" 2>/dev/null || echo false)
if [ "$RUN" != "true" ]; then
  logger -t g98-m7-watch "m7 not running ($RUN), restarting g98-m7.service"
  systemctl restart g98-m7.service
fi