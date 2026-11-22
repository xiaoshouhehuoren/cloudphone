#!/bin/bash
set -e
C=m7
IMG=g98/redroid-panthor-rt:13-mt6983-camfix-v5
docker rm -f $C >/dev/null 2>&1 || true
docker run -d --name $C --privileged --security-opt label=disable --cap-add SYS_NICE --entrypoint /init \
 -v /srv/redroid/camcfg/cam0-external_camera_config.xml:/vendor/etc/external_camera_config.xml:ro \
 -v /srv/redroid/m7:/data \
 --device /dev/dri --device /dev/video14:/dev/video10 --device /dev/video15:/dev/video11 \
 $IMG \
 qemu=1 androidboot.hardware=mt6983 androidboot.verifiedbootstate=green androidboot.flash.locked=1 androidboot.veritymode=enforcing androidboot.selinux=enforcing \
 ro.hardware.gralloc=g98 ro.hw_timeout_multiplier=10 \
 ro.product.model=23127PN0CC ro.product.brand=Xiaomi ro.product.manufacturer=Xiaomi ro.product.device=houji ro.product.name=houji \
 ro.build.type=user ro.build.tags=release-keys \
 ro.build.fingerprint=Xiaomi/houji/houji:13/TKQ1.221114.001/V14.0.9.0.TMCCNXM:user/release-keys >/dev/null
for i in $(seq 1 60); do B=$(docker exec $C getprop sys.boot_completed 2>/dev/null | tr -d '\r'); [ "$B" = "1" ] && break; sleep 3; done
[ "$B" = "1" ] || { echo "boot timeout"; exit 1; }
# per-container proxy: default route + Android global proxy
/srv/redroid/container-proxy.sh "$C" 18081 172.17.0.1 || echo "WARN: proxy setup failed"
# native proxy: transparent TCP/DNS redirect inside the container netns
systemctl restart g98-native-proxy.service || echo "WARN: native proxy restart failed"
sleep 2
# restart the host-side raw YUV producers for camera nodes 14/15
for N in 14 15; do
  P=/srv/redroid/cam${N}.pid
  if [ -f "$P" ]; then kill "$(cat "$P")" 2>/dev/null || true; fi
done
sleep 1
nohup /root/ffmpeg -re -f lavfi -i testsrc2=size=640x480:rate=30 -pix_fmt yuv420p -f v4l2 /dev/video14 >/srv/redroid/cam14.log 2>&1 &
echo $! >/srv/redroid/cam14.pid
nohup /root/ffmpeg -re -f lavfi -i smptebars=size=640x480:rate=30 -pix_fmt yuv420p -f v4l2 /dev/video15 >/srv/redroid/cam15.log 2>&1 &
echo $! >/srv/redroid/cam15.pid
docker exec $C sh -c 'chmod 666 /dev/video10 /dev/video11 /dev/video14 /dev/video15 2>/dev/null; mkdir -p /fake/block /data/local/tmp; echo 1 > /fake/enforce; echo "u:r:untrusted_app:s0" > /fake/attr; { echo "/dev/block/dm-0 / ext4 rw,seclabel,relatime 0 0"; tail -n +2 /proc/mounts; } > /fake/mounts'
docker cp /srv/redroid/risk-watch.sh $C:/data/local/tmp/risk-watch.sh
docker exec $C chmod 755 /data/local/tmp/risk-watch.sh
docker exec $C settings put global adb_enabled 0
# CTID risk warning activity: keep it disabled so the app's risk page cannot block
docker exec $C pm disable cn.cyberIdentity.certification/cn.wh.safety.threat.c 2>/dev/null || true
docker exec $C setprop vendor.camera.redroid.nodes /dev/video14,/dev/video15
docker exec $C setprop ctl.stop vendor.camera-provider-2-7-ext || true
docker exec $C setprop ctl.stop cameraserver || true
docker exec $C setprop ctl.stop vendor.camera-provider-2-4-legacy || true
sleep 2
docker exec $C setprop ctl.start vendor.camera-provider-2-4-legacy
docker exec $C setprop ctl.start cameraserver
sleep 3
docker exec $C dumpsys media.camera 2>/dev/null | grep -q 'Number of camera devices: 2' || echo "WARN: camera devices not 2"
docker exec $C sh -c 'mount --bind /fake/enforce /sys/fs/selinux/enforce; mount --bind /fake/block /sys/block'
docker exec -d $C /system/bin/sh /data/local/tmp/risk-watch.sh
echo "m7 ready"
