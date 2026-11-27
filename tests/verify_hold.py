#!/usr/bin/env python3
"""Verify camd keeps a node fed when the RTSP source is silent ("hold last frame").

Why this matters: the camera HAL must always have a buffer to dequeue.  When the
phone stops publishing, ffmpeg has no frames, and before this change the writer
had nothing to send - the node starved, the HAL returned RESULT errors (3) and
legacy Camera API1 clients (WeChat face verification) killed the session with
"Failed to obtain configurations".

Cases checked on /dev/video22 (wx2):
  A. no publisher at all    -> a black fill frame keeps written growing at FPS,
                               source_frames stays 0, no write errors, and empty
                               connection attempts back off instead of spinning.
  B. publisher then silence  -> last real frame is repeated (holding=true), the
                               node keeps receiving frames, and the reader does
                               NOT reconnect before MAX_SILENCE (30s).
"""
import json, os, subprocess, sys, time, urllib.request

CAMD = "http://127.0.0.1:8100"
NODE = sys.argv[1] if len(sys.argv) > 1 else "/dev/video22"
FFMPEG = "/root/ffmpeg"
URL = "rtsp://127.0.0.1:8554/g98-holdtest"
HOLD_AFTER = 1.0
MAX_SILENCE = 30.0


def post(path, body):
    req = urllib.request.Request(CAMD + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=15).read())


def state(node=NODE):
    data = json.loads(urllib.request.urlopen(CAMD + "/state", timeout=5).read())
    for n in data["nodes"]:
        if n["node"] == node:
            return n
    raise SystemExit("node %s missing from /state" % node)


def sample(seconds):
    a = state()
    time.sleep(seconds)
    b = state()
    return a, b, (b["written"] - a["written"]) / float(seconds)


def fail(msg):
    print("FAIL: " + msg)
    sys.exit(1)


def main():
    failures = []

    # ---- case A: no publisher at all -------------------------------------
    post("/set", {"node": NODE, "kind": "rtsp", "url": URL, "fit": "contain"})
    started = time.time()
    a = state()
    restart_times = []
    last_restarts = a["source_restarts"]
    b = a
    # mediamtx rejects a path with no publisher immediately, so attempts can
    # end before START_TIMEOUT.  Backoff still spaces them about 2s, 4s, 8s,
    # 15s apart; the old fixed 1s retry produced roughly one attempt/second.
    while time.time() - started < 34:
        time.sleep(1)
        b = state()
        if b["source_restarts"] != last_restarts:
            restart_times.append(round(time.time() - started, 1))
            last_restarts = b["source_restarts"]
    elapsed = time.time() - started
    fps = (b["written"] - a["written"]) / elapsed
    print("A no-publisher: written %d -> %d (%.1f fps) source_frames %d -> %d errors %d"
          % (a["written"], b["written"], fps, a["source_frames"], b["source_frames"], b["errors"]))
    print("A backoff: restarts at %s, next delay=%ss" % (restart_times, b.get("retry_delay")))
    if fps < 10:
        failures.append("blank fill frame not written (%.1f fps < 10)" % fps)
    if b["errors"]:
        failures.append("write errors=%d" % b["errors"])
    if "holding" not in b or "last_frame_age" not in b:
        failures.append("/state lacks holding/last_frame_age fields")
    if b.get("retry_delay", 0) < 4:
        failures.append("zero-frame retry backoff did not increase (delay=%s)" % b.get("retry_delay"))
    if len(restart_times) < 4:
        failures.append("expected at least four empty-source retries, got %s" % restart_times)
    else:
        gaps = [round(restart_times[i + 1] - restart_times[i], 1)
                for i in range(len(restart_times) - 1)]
        print("A backoff gaps: %s" % gaps)
        if gaps[-1] < 12:
            failures.append("last retry gap did not reach ~15s: %s" % gaps)
    if b["source_restarts"] > 6:
        failures.append("too many empty-source retries in 34s: %d" % b["source_restarts"])

    # ---- case B: real publisher, then silence ----------------------------
    pub = subprocess.Popen(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", "testsrc2=size=1280x960:rate=30", "-t", "30",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
         "-pix_fmt", "yuv420p", "-f", "rtsp", URL],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    live = None
    for _ in range(20):
        time.sleep(1)
        st = state()
        if st["source_frames"] > 5:
            live = st
            break
    if live is None:
        pub.kill()
        post("/set", {"node": NODE, "kind": "idle"})
        fail("publisher never delivered frames to %s" % NODE)
    print("B live: source_frames=%d holding=%s retry_delay=%s"
          % (live["source_frames"], live["holding"], live.get("retry_delay")))
    if live.get("retry_delay") != 0:
        failures.append("receiving frames did not reset retry backoff")
    pub.kill()
    pub.wait()

    time.sleep(HOLD_AFTER + 1.5)
    a, b, fps = sample(6)
    print("B silent: holding %s -> %s age %s | written %d -> %d (%.1f fps) | source_frames %d -> %d | restarts %d -> %d"
          % (a["holding"], b["holding"], b["last_frame_age"], a["written"], b["written"], fps,
             a["source_frames"], b["source_frames"], a["source_restarts"], b["source_restarts"]))
    if not b["holding"]:
        failures.append("holding flag not set after the source went silent")
    if fps < 10:
        failures.append("node starved after silence (%.1f fps < 10)" % fps)
    if b["source_frames"] != a["source_frames"]:
        failures.append("source frames kept arriving (publisher not really dead?)")
    if b["source_restarts"] != a["source_restarts"]:
        failures.append("reader reconnected inside %.0fs (MAX_SILENCE=%s)"
                        % (6 + HOLD_AFTER, MAX_SILENCE))

    post("/set", {"node": NODE, "kind": "idle"})
    if failures:
        for f in failures:
            print("FAIL: " + f)
        print("RESULT FAIL")
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
