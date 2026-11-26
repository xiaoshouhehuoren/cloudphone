#!/bin/bash
# Compare injected video geometry against node format and on-screen picture rect.
set -u
NODE=${1:-/dev/video20}
SRC=${2:-/root/camin/1795581891716.mp4}
NAME=${3:-wx1}
WATCH=${4:-everything}

reset_camera() {
  /usr/bin/docker exec "$NAME" am force-stop com.android.camera2 >/dev/null 2>&1
  /usr/bin/docker exec "$NAME" setprop ctl.restart vendor.camera-provider-2-4-legacy
  sleep 3
  /usr/bin/docker exec "$NAME" setprop ctl.restart cameraserver
  sleep 3
}

shot() {
  /usr/bin/docker exec "$NAME" sh -c 'screencap -p /data/local/tmp/g.png'
  /usr/bin/docker cp "$NAME:/data/local/tmp/g.png" "$1" >/dev/null
}

set_src() {
  /usr/bin/curl -s --max-time 5 -X POST http://127.0.0.1:8100/set \
    -H 'Content-Type: application/json' \
    -d "{\"node\":\"$NODE\",\"kind\":\"video\",\"path\":\"$SRC\"$1}" | head -c 400
  echo
}

echo "== source probe =="
/root/ffmpeg -hide_banner -i "$SRC" 2>&1 | grep -E 'Duration|Video:' | sed 's/^ *//'

echo "== node format =="
/root/ffmpeg -hide_banner -f v4l2 -i "$NODE" -frames:v 1 -f null - 2>&1 | grep -E 'Video:' | head -2

for MODE in contain cover; do
  echo "== mode $MODE =="
  if [ "$MODE" = cover ]; then
    set_src ',"fit":"cover"'
  else
    set_src ''
  fi
  reset_camera
  /usr/bin/docker exec "$NAME" am start -n com.android.camera2/com.android.camera.CameraLauncher >/dev/null 2>&1
  sleep 4
  shot /tmp/shot_$MODE.png
  /usr/bin/curl -s --max-time 3 http://127.0.0.1:8100/state | python3 -c "
import json,sys
d=json.load(sys.stdin)
for n in d['nodes']:
    if n['node']=='$NODE':
        g=n['geometry']; s=n['src']
        print('camd src=%sx%s fps=%s dur=%s' % (s.get('width'),s.get('height'),s.get('fps'),s.get('duration')))
        print('camd fit=%s rotate=%s content=%dx%d at (%d,%d) content_dar=%.4f' % (g['fit'],g['rotate'],g['content_w'],g['content_h'],g['content_x'],g['content_y'],g['content_dar']))
"
  echo -n "screen "
  python3 /root/tests/shot_bbox.py /tmp/shot_$MODE.png 1080 2400 420
  /usr/bin/docker exec "$NAME" dumpsys media.camera 2>/dev/null | grep -o 'ImageReader-[0-9]*x[0-9]*' | head -2
done
