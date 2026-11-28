#!/bin/bash
# watch_camera_client.sh <instance> <seconds> [outdir]
# One-shot forensic capture for any camera client (WeChat face verify, camera
# app, scanner). Captures, for the whole window:
#   - full logcat (all buffers) of the instance          -> logcat.all.txt
#   - one real source frame per second from /dev/videoN  -> frame_NNN.png
#   - one screenshot every 2s                            -> screen_NNN.png
#   - camd state sample every 2s                         -> camd.log
#   - summary.txt: connects, stream config, SDK lines, errors, frame counts
# Usage on the board: bash /root/tests/watch_camera_client.sh wx1 300
set -u
NAME=${1:?usage: watch_camera_client.sh <instance> <seconds> [outdir]}
SECS=${2:-120}
OUT=${3:-/root/watch/$(date +%Y%m%d-%H%M%S)-$NAME}
FRONT_NODE=${FRONT_NODE:-21}
# Sampling intervals are tunable: 1s frame + 2s screenshot captures noticeably
# loaded the board (user felt laggy back/home buttons during a live attempt).
FRAME_INT=${FRAME_INT:-2}
SCREEN_INT=${SCREEN_INT:-4}
mkdir -p "$OUT"
FF=/root/ffmpeg
echo "out=$OUT secs=$SECS"

docker exec "$NAME" logcat -b all -c 2>/dev/null
docker exec "$NAME" logcat -b all -v time > "$OUT/logcat.all.txt" 2>&1 &
P_LOG=$!
(
  for i in $(seq 0 $SECS); do
    date +%H:%M:%S >> "$OUT/frames.txt"
    "$FF" -hide_banner -loglevel error -f v4l2 -i /dev/video$FRONT_NODE \
      -frames:v 1 -y "$OUT/frame_$(printf %03d $i).png" 2>>"$OUT/frames.txt"
    sleep $FRAME_INT
  done
) >/dev/null 2>&1 &
P_FR=$!
(
  N=$((SECS/SCREEN_INT))
  for i in $(seq 0 $N); do
    docker exec "$NAME" screencap -p /data/local/tmp/_watch.png 2>/dev/null
    docker cp "$NAME":/data/local/tmp/_watch.png "$OUT/screen_$(printf %03d $i).png" >/dev/null 2>&1
    sleep $SCREEN_INT
  done
) >/dev/null 2>&1 &
P_SC=$!
(
  N=$((SECS/2))
  for i in $(seq 0 $N); do
    date +%H:%M:%S >> "$OUT/camd.log"
    curl -s --max-time 3 http://127.0.0.1:8100/state | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit()
for n in d['nodes']:
    if n['node']=='/dev/video$FRONT_NODE':
        print(' ', n.get('kind'), 'src', n.get('source_frames'), 'hold', n.get('holding'), 'err', n.get('errors'))
" >> "$OUT/camd.log" 2>&1
    sleep 2
  done
) >/dev/null 2>&1 &
P_CA=$!

echo "$P_LOG $P_FR $P_SC $P_CA" > "$OUT/pids"
echo "started; run: bash /root/tests/watch_camera_client_summary.sh $OUT"
wait $P_FR $P_SC $P_CA 2>/dev/null
kill $P_LOG 2>/dev/null
bash /root/tests/watch_camera_client_summary.sh "$OUT"
