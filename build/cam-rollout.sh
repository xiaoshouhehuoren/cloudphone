#!/bin/sh
# cam-rollout.sh <container>  -- add the App camera stack (cam15) to a redroid container
set -e
C=$1
IMG=g98/redroid-panthor-cam15:13
[ -z "$C" ] && echo "usage: cam-rollout.sh <container>" && exit 1
mkdir -p /tmp/rtc15
cid=$(docker create $IMG)
docker cp $cid:/vendor/lib64/hw/camera.redroid.so /tmp/rtc15/hw-camera.redroid.so
docker cp $cid:/vendor/lib64/hw/camera.v4l2.so /tmp/rtc15/hw-camera.v4l2.so
docker cp $cid:/vendor/lib64/hw/camera.so /tmp/rtc15/hw-camera.so
docker cp $cid:/vendor/bin/hw/android.hardware.camera.provider@2.4-service /tmp/rtc15/provider24
docker cp $cid:/vendor/etc/init/android.hardware.camera.provider@2.4-service.rc /tmp/rtc15/provider24.rc
docker cp $cid:/vendor/etc/vintf/manifest.xml /tmp/rtc15/manifest.xml
docker rm $cid >/dev/null
# keep a restorable copy of the image's own manifest inside /data (survives restarts)
if [ ! -f /srv/redroid/$C/manifest.xml.orig ] && docker exec $C test -f /vendor/etc/vintf/manifest.xml; then
  docker cp $C:/vendor/etc/vintf/manifest.xml /srv/redroid/$C/manifest.xml.orig
fi
docker cp /tmp/rtc15/hw-camera.redroid.so $C:/vendor/lib64/hw/camera.redroid.so
docker cp /tmp/rtc15/hw-camera.v4l2.so    $C:/vendor/lib64/hw/camera.v4l2.so
docker cp /tmp/rtc15/hw-camera.so         $C:/vendor/lib64/hw/camera.so
docker cp /tmp/rtc15/provider24           $C:/vendor/bin/hw/android.hardware.camera.provider@2.4-service
docker cp /tmp/rtc15/provider24.rc        $C:/vendor/etc/init/android.hardware.camera.provider@2.4-service.rc
docker cp /tmp/rtc15/manifest.xml         $C:/vendor/etc/vintf/manifest.xml
docker exec $C chmod 755 /vendor/lib64/hw/camera.redroid.so /vendor/lib64/hw/camera.v4l2.so /vendor/lib64/hw/camera.so /vendor/bin/hw/android.hardware.camera.provider@2.4-service
docker restart $C
for i in $(seq 1 60); do
  b=$(timeout 15 docker exec $C getprop sys.boot_completed 2>/dev/null | tr -d '\r')
  [ "$b" = "1" ] && echo "BOOT_OK after $((i*3))s" && break
  sleep 3
done
sleep 12
docker exec $C setprop ctl.stop vendor.camera-provider-2-7-ext 2>/dev/null || true
docker exec $C dumpsys media.camera | grep -m1 "Number of camera"
docker exec $C sh -c 'ls -l /vendor/lib64/hw/camera.redroid.so /vendor/bin/hw/android.hardware.camera.provider@2.4-service'
