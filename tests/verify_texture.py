#!/usr/bin/env python3
"""Verify camd gives injected video the statistics of a real sensor feed.

Why this matters: face liveness / anti-replay checks look for temporal sensor
traits. A source that is decoded once and repeated byte-for-byte is a "sticker
stream": every frame pixel-identical, the frame interval a perfect clock, and
brightness never drifts, while a real camera has shot noise, AE breathing and
transport jitter. camd now adds all three (G98_CAMD_GRAIN,
G98_CAMD_AE_BREATH, G98_CAMD_FRAME_JITTER) and pre-renders a noisy frame set
for still images instead of repeating one decoded picture.

Checks on NODE (default /dev/video22):
  A. image source -> consecutive frames differ (grain alive) and mean luma
                     drifts over time (AE breathing).
  B. idle source  -> frames keep flowing after switching back to idle.
"""
import json, subprocess, sys, time, urllib.request

CAMD = "http://127.0.0.1:8100"
NODE = sys.argv[1] if len(sys.argv) > 1 else "/dev/video22"
FFMPEG = "/root/ffmpeg"
W, H = 1280, 960
FRAME = W * H * 3 // 2
IMAGE = "/root/camin/astronaut.png"


def post(path, body):
    req = urllib.request.Request(CAMD + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def state():
    data = json.loads(urllib.request.urlopen(CAMD + "/state", timeout=5).read())
    for n in data["nodes"]:
        if n["node"] == NODE:
            return n
    raise SystemExit("node %s missing from /state" % NODE)


def grab(path, frames):
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "v4l2",
                    "-i", NODE, "-frames:v", str(frames), "-f", "rawvideo",
                    "-pix_fmt", "yuv420p", path, "-y"], check=True, timeout=60)
    with open(path, "rb") as fh:
        data = fh.read()
    return [data[i:i + FRAME] for i in range(0, len(data) - FRAME + 1, FRAME)]


def mean_y(frame):
    y = frame[:60000]
    return sum(y) / float(len(y))


def main():
    failures = []
    post("/set", {"node": NODE, "kind": "image", "path": IMAGE,
                  "fit": "contain", "ystretch": 1.0})
    time.sleep(2.5)
    a = grab("/tmp/tex_a.raw", 12)
    time.sleep(6)
    b = grab("/tmp/tex_b.raw", 6)
    if len(a) < 6 or len(b) < 3:
        failures.append("not enough frames captured %d/%d" % (len(a), len(b)))
    else:
        distinct = len(set(a))
        print("A. frames=%d distinct=%d" % (len(a), distinct))
        if distinct < 2:
            failures.append("frames byte-identical: still a sticker stream")
        changed = sum(1 for x, y in zip(a[0][:60000], a[-1][:60000]) if x != y)
        print("A. changed Y between first/last frame = %d/60000" % changed)
        if changed < 500:
            failures.append("almost no temporal grain (%d/60000)" % changed)
        m0 = mean_y(a[0])
        m1 = mean_y(b[-1])
        print("A. meanY first=%.2f later=%.2f drift=%.2f" % (m0, m1, m1 - m0))
        if abs(m1 - m0) < 0.5:
            failures.append("no AE breathing (luma drift %.2f)" % (m1 - m0))
    # latency of the writer must stay sane: 12 fps static + jitter
    s0 = state()
    time.sleep(4)
    s1 = state()
    fps = (s1["written"] - s0["written"]) / 4.0
    print("A. written fps = %.1f errors=%d" % (fps, s1["errors"]))
    if fps < 6:
        failures.append("writer too slow (%.1f fps)" % fps)
    if s1["errors"]:
        failures.append("write errors=%d" % s1["errors"])
    post("/set", {"node": NODE, "kind": "idle"})
    time.sleep(2)
    s2 = state()
    if s2["written"] <= s1["written"]:
        failures.append("idle node not fed after switch")
    print("B. idle written=%d kind=%s" % (s2["written"], s2["kind"]))
    if failures:
        for f in failures:
            print("FAIL: " + f)
        print("RESULT FAIL (%d)" % len(failures))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())