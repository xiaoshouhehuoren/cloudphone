#!/bin/bash
set -u
H=192.168.5.111
export SSH_ASKPASS="C:/Users/rosetta/Desktop/g98/_askpass.cmd" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
SSHOPT="-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no"
scp $SSHOPT camera.provider-2.4-new root@$H:/tmp/provider-new >/dev/null 2>&1 || exit 1
ssh $SSHOPT root@$H 'bash -s' <<'EOS'
for C in wx1 wx2 wx3 wx4 wx5; do
  /usr/bin/docker exec $C am force-stop com.android.camera2 >/dev/null 2>&1
  # Stop the provider first: copying over a running binary fails with ETXTBSY.
  /usr/bin/docker exec $C setprop ctl.stop vendor.camera-provider-2-4-legacy
  sleep 1
  /usr/bin/docker cp /tmp/provider-new $C:/data/local/tmp/provider.new
  /usr/bin/docker exec $C cp /data/local/tmp/provider.new /vendor/bin/hw/android.hardware.camera.provider@2.4-service
  /usr/bin/docker exec $C chmod 755 /vendor/bin/hw/android.hardware.camera.provider@2.4-service
  /usr/bin/docker exec $C setprop ctl.start vendor.camera-provider-2-4-legacy
  /usr/bin/docker exec $C setprop ctl.restart cameraserver
  for i in $(seq 1 20); do n=$(/usr/bin/docker exec $C dumpsys media.camera 2>/dev/null | grep -m1 "Number of camera devices" | tr -dc 0-9); [ "$n" = "2" ] && break; sleep 1; done
  echo "$C md5=$(/usr/bin/docker exec $C md5sum /vendor/bin/hw/android.hardware.camera.provider@2.4-service | cut -c1-12) devices=$n"
done
EOS
