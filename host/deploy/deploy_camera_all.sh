#!/bin/bash
set -u
H=192.168.5.111
export SSH_ASKPASS="C:/Users/rosetta/Desktop/g98/_askpass.cmd" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
SSHOPT="-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no"
LIB=camera_v7.so
scp $SSHOPT $LIB root@$H:/tmp/camera_new.so >/dev/null 2>&1 || { echo scp-fail; exit 1; }
ssh $SSHOPT root@$H 'bash -s' <<'EOS'
for C in wx1 wx2 wx3 wx4 wx5; do
  /usr/bin/docker exec $C am force-stop com.android.camera2 >/dev/null 2>&1
  /usr/bin/docker cp /tmp/camera_new.so $C:/vendor/lib64/hw/camera.mt6983.so
  /usr/bin/docker exec $C chmod 644 /vendor/lib64/hw/camera.mt6983.so
  /usr/bin/docker exec $C setprop ctl.restart vendor.camera-provider-2-4-legacy
  /usr/bin/docker exec $C setprop ctl.restart cameraserver
  for i in $(seq 1 20); do n=$(/usr/bin/docker exec $C dumpsys media.camera 2>/dev/null | grep -m1 "Number of camera devices" | tr -dc 0-9); [ "$n" = "2" ] && break; sleep 1; done
  echo "$C md5=$(/usr/bin/docker exec $C md5sum /vendor/lib64/hw/camera.mt6983.so | cut -c1-8) devices=$n"
done
EOS
