#!/bin/bash
set -u
NAME=wx1
NODE=/dev/video20
/root/ffmpeg -hide_banner -loglevel error -y -f lavfi -i "color=c=black:s=640x480:d=1" -vf "drawbox=x=0:y=0:w=640:h=480:color=black@1:t=fill,drawbox=x=20:y=20:w=600:h=440:color=white@1:t=2,drawbox=x=320:y=240:w=2:h=2:color=red@1:t=fill,drawgrid=w=64:h=48:t=1:c=gray@0.6" -frames:v 1 /root/camin/calib.png
/usr/bin/curl -s -X POST http://127.0.0.1:8100/set -H 'Content-Type: application/json' -d "{\"node\":\"$NODE\",\"kind\":\"image\",\"path\":\"/root/camin/calib.png\"}" >/dev/null
/usr/bin/docker exec $NAME am force-stop com.android.camera2 >/dev/null 2>&1
/usr/bin/docker exec $NAME setprop ctl.restart vendor.camera-provider-2-4-legacy
sleep 3
/usr/bin/docker exec $NAME setprop ctl.restart cameraserver
sleep 3
/usr/bin/docker exec $NAME am start -n com.android.camera2/com.android.camera.CameraLauncher >/dev/null 2>&1
sleep 4
/usr/bin/docker exec $NAME sh -c 'screencap -p /data/local/tmp/calib.png'
/usr/bin/docker cp $NAME:/data/local/tmp/calib.png /tmp/calib_shot.png >/dev/null
echo done
