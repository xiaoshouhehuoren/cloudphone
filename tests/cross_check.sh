#!/bin/bash
set -u
NAME=wx1; NODE=/dev/video20; SRC=/root/camin/cross.png
set_src() {
  /usr/bin/curl -s -X POST http://127.0.0.1:8100/set -H 'Content-Type: application/json' \
    -d "{\"node\":\"$NODE\",\"kind\":\"image\",\"path\":\"$SRC\"$1}" >/dev/null
}
shot() {
  /usr/bin/docker exec $NAME sh -c 'screencap -p /data/local/tmp/x.png'
  /usr/bin/docker cp $NAME:/data/local/tmp/x.png "$1" >/dev/null
}
reset_camera() {
  /usr/bin/docker exec $NAME am force-stop com.android.camera2 >/dev/null 2>&1
  /usr/bin/docker exec $NAME setprop ctl.restart vendor.camera-provider-2-4-legacy
  sleep 3
  /usr/bin/docker exec $NAME setprop ctl.restart cameraserver
  sleep 3
  /usr/bin/docker exec $NAME am start -n com.android.camera2/com.android.camera.CameraLauncher >/dev/null 2>&1
  sleep 4
}
run_case() {
  label=$1; extra=$2
  set_src "$extra"
  reset_camera
  shot /tmp/cross_$label.png
  echo "== $label =="
  /usr/bin/curl -s --max-time 3 http://127.0.0.1:8100/state | python3 -c "
import json,sys
for n in json.load(sys.stdin)['nodes']:
    if n['node']=='$NODE':
        g=n['geometry']
        print('  content=%dx%d at (%d,%d) ystretch=%s content_dar=%.4f display_dar=%.4f' % (g['content_w'],g['content_h'],g['content_x'],g['content_y'],g['ystretch'],g['content_dar'],g['display_dar']))
"
  python3 /root/tests/white_bbox.py /tmp/cross_$label.png
}
run_case k1701 ',"ystretch":1.701'
run_case k100 ''
run_case cover ',"fit":"cover","ystretch":1.701'
