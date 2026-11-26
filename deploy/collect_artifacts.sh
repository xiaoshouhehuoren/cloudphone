#!/bin/bash
# Collect the running board's camera stack into ./artifacts for the Docker build.
set -euo pipefail
H="${G98_HOST:-192.168.5.111}"
C="${1:-wx1}"
D="$(cd "$(dirname "$0")" && pwd)/artifacts"
mkdir -p "$D"
export SSH_ASKPASS="${SSH_ASKPASS:-$(dirname "$0")/../_askpass.cmd}" SSH_ASKPASS_REQUIRE=force DISPLAY=${DISPLAY:-:0}
SSHOPT="-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no"

pull() { # remote-in-container path -> local name
  ssh $SSHOPT root@$H "/usr/bin/docker cp $C:$1 /tmp/_art && /usr/bin/docker cp $C:$1 \$D/$(basename $1)" 2>/dev/null || \
  ssh $SSHOPT root@$H "cat $1" > "$D/$(basename $1)"
}

ssh $SSHOPT root@$H "cat /root/console3.py" >/dev/null # connectivity probe

for p in /vendor/bin/hw/android.hardware.camera.provider@2.4-service \
         /vendor/lib64/hw/camera.mt6983.so \
         /vendor/lib64/hw/gralloc.g98.so \
         /vendor/etc/g98-sensors.dex \
         /vendor/etc/init/g98-sensors.rc \
         /vendor/etc/init/android.hardware.camera.provider@2.4-legacy-service.rc \
         /vendor/etc/init/android.hardware.camera.provider@2.7-external-service.rc; do
  n="$(basename $p)"
  ssh $SSHOPT root@$H "/usr/bin/docker cp $C:$p /tmp/_art_art" >/dev/null 2>&1
  ssh $SSHOPT root@$H "/usr/bin/docker exec $C sh -c 'base64 -w0 $p'" > "$D/$n.b64"
  base64 -d "$D/$n.b64" > "$D/$n" && rm -f "$D/$n.b64"
  echo "collected $n"
done
