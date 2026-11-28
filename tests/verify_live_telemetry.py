#!/usr/bin/env python3
"""Verify live RTSP telemetry with an isolated synthetic 30 fps VP8 source.

Runs on the board. Only NODE is changed and is always restored. The publisher
uses a dedicated mediamtx path, so a phone publishing cam-wxN is not disturbed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

CAMD = "http://127.0.0.1:8100"
NODE = os.environ.get("NODE", "/dev/video28")
FFMPEG = os.environ.get("FFMPEG", "/root/ffmpeg")
PATH = "cam-telemetry-test-%d" % os.getpid()
URL = "rtsp://127.0.0.1:8554/" + PATH


def state():
    data = json.loads(urllib.request.urlopen(CAMD + "/state", timeout=10).read())
    return next(n for n in data["nodes"] if n["node"] == NODE)


def set_source(body):
    req = urllib.request.Request(
        CAMD + "/set",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=20).read())


def restore(n):
    kind = n.get("kind") or "idle"
    body = {"node": NODE, "kind": kind}
    geo = n.get("geometry") or {}
    for key in ("fit", "rotate", "mirror", "ystretch"):
        if geo.get(key) is not None:
            body[key] = geo[key]
    arg = n.get("arg")
    if arg and kind in ("rtsp", "image", "video"):
        body["url" if kind == "rtsp" else "path"] = arg
    set_source(body)


def main():
    saved = state()
    pub = None
    fails = []
    samples = []
    try:
        cmd = [
            FFMPEG, "-hide_banner", "-loglevel", "warning", "-nostdin", "-re",
            "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30", "-an",
            "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "8",
            "-b:v", "800k", "-g", "30", "-rtsp_transport", "tcp",
            "-f", "rtsp", URL,
        ]
        pub = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        time.sleep(2)
        if pub.poll() is not None:
            print("[FAIL] publisher exited:", pub.stderr.read().decode("utf-8", "replace")[-800:])
            return 2
        set_source({
            "node": NODE, "kind": "rtsp", "url": URL,
            "fit": "cover", "rotate": 0, "mirror": False, "ystretch": 1.0,
        })
        deadline = time.time() + 20
        while time.time() < deadline and len(samples) < 5:
            time.sleep(2)
            n = state()
            t = n.get("source_telemetry") or {}
            samples.append((n, t))
            print(
                "sample frame=%s pts_us=%s input=%sx%s hold=%s age=%s dup=%s drop=%s" % (
                    t.get("progress_frame"), t.get("progress_out_time_us"),
                    t.get("input_w"), t.get("input_h"), n.get("holding"),
                    t.get("progress_age"), t.get("dup_frames"), t.get("drop_frames"),
                )
            )
        good = [t for _, t in samples if (t.get("progress_frame") or 0) > 0]
        if len(good) < 2:
            fails.append("fewer than two nonempty progress samples")
        else:
            first, last = good[0], good[-1]
            df = last["progress_frame"] - first["progress_frame"]
            dt = last["progress_at"] - first["progress_at"]
            fps = df / dt if dt > 0 else 0.0
            print("decoded progress: %.2f fps (%d frames / %.2fs)" % (fps, df, dt))
            if not 5 <= fps <= 65:
                fails.append("decoded progress rate %.2f outside 5..65 fps" % fps)
            if (last.get("progress_out_time_us") or 0) <= (first.get("progress_out_time_us") or 0):
                fails.append("PTS did not advance")
            if (last.get("input_w"), last.get("input_h")) != (640, 480):
                fails.append("input geometry %sx%s != 640x480" % (last.get("input_w"), last.get("input_h")))
            if last.get("progress_age") is None or last["progress_age"] > 3:
                fails.append("progress telemetry is stale")
            if last.get("dup_frames") or last.get("drop_frames"):
                fails.append("ffmpeg reported duplicated/dropped output frames")
        if samples and samples[-1][0].get("holding"):
            fails.append("live source entered hold during active publishing")
    except Exception as exc:
        fails.append("exception: %s" % exc)
    finally:
        try:
            restore(saved)
        except Exception as exc:
            fails.append("restore failed: %s" % exc)
        if pub is not None:
            pub.terminate()
            try:
                pub.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pub.kill()
                pub.communicate(timeout=5)
    for f in fails:
        print("[FAIL]", f)
    print("RESULT " + ("PASS" if not fails else "FAIL"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
