#!/usr/bin/env python3
"""Compare the injected source against every stage of the camera chain.

Stages compared:
  1. source file        (ffmpeg banner: WxH, fps, duration)
  2. node format        (/dev/videoNN: WxH)
  3. camd geometry      (content rect inside the node frame, display aspect)
  4. camera app stream  (dumpsys: SurfaceTexture-WxH / ImageReader-WxH)
  5. on-screen picture  (screenshot: non-black rect, mapped back to buffer)

Usage: verify_geometry.py <name> <node> <media> [fit] [ystretch]
"""
import json
import os
import re
import subprocess
import sys

FFMPEG = "/root/ffmpeg"
DOCKER = "/usr/bin/docker"
CAMD = "http://127.0.0.1:8100"


def sh(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, capture_output=True,
                          timeout=timeout).stdout.decode("utf-8", "replace")


def probe(path):
    text = subprocess.run([FFMPEG, "-hide_banner", "-i", path],
                          capture_output=True, timeout=30).stderr.decode("utf-8", "replace")
    info = {}
    m = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", text)
    if m:
        info["duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60
                                 + float(m.group(3)), 2)
    m = re.search(r"Video: [^\n]*?, (\d{2,5})x(\d{2,5})", text)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"([\d.]+) fps", text)
    if m:
        info["fps"] = float(m.group(1))
    return info


def node_format(node):
    text = subprocess.run([FFMPEG, "-hide_banner", "-f", "v4l2", "-i", node,
                           "-frames:v", "1", "-f", "null", "-"],
                          capture_output=True, timeout=30).stderr.decode("utf-8", "replace")
    m = re.search(r"yuv420p, (\d+)x(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def camd_geom(node):
    out = sh("curl -s --max-time 4 %s/state" % CAMD)
    for n in json.loads(out)["nodes"]:
        if n["node"] == node:
            return n
    return None


def app_stream(name):
    out = sh('%s exec %s dumpsys media.camera 2>/dev/null' % (DOCKER, name))
    dims = set()
    for m in re.finditer(r"(?:SurfaceTexture|ImageReader)-(\d+)x(\d+)", out):
        dims.add((int(m.group(1)), int(m.group(2))))
    return dims


def screen_bbox(path, w=1080, h=2400, top=560, bottom=420, thresh=30):
    data = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, check=True).stdout
    rows = []
    for y in range(top, h - bottom):
        row = data[y * w * 3:(y + 1) * w * 3]
        cnt = sum(1 for x in range(0, w, 8) if max(row[x*3:x*3+3]) > thresh)
        if cnt > (w / 8) * 0.25:
            rows.append(y)
    if not rows:
        return None
    y0, y1 = rows[0], rows[-1]
    cols = []
    for x in range(0, w, 2):
        hit = 0
        for y in range(y0, y1 + 1, 6):
            i = (y * w + x) * 3
            if max(data[i:i+3]) > thresh:
                hit += 1
        if hit > ((y1 - y0) // 6) * 0.5:
            cols.append(x)
    if not cols:
        return None
    return (cols[0], y0, cols[-1] + 2 - cols[0], y1 + 1 - y0)


def peer_node(node):
    """video20<->video21, video22<->video23, ... (back/front pair order)."""
    try:
        n = int(node.rstrip("/").rsplit("video", 1)[1])
    except (IndexError, ValueError):
        return None
    if n < 20:
        return None
    return "/dev/video%d" % (n + 1 if (n - 20) % 2 == 0 else n - 1)


def main():
    name, node, media = sys.argv[1], sys.argv[2], sys.argv[3]
    fit = sys.argv[4] if len(sys.argv) > 4 else "contain"
    ystretch = float(sys.argv[5]) if len(sys.argv) > 5 else None
    body = {"node": node, "kind": "video" if media.endswith((".mp4", ".webm", ".mkv"))
            else "image", "path": media, "fit": fit}
    if ystretch:
        body["ystretch"] = ystretch
    sh("curl -s -X POST %s/set -H 'Content-Type: application/json' -d '%s'"
       % (CAMD, json.dumps(body)))
    # the camera app remembers the last-used lens: mirror the source onto the
    # peer node (back<->front) so the measured screen is always our test media
    peer = peer_node(node)
    if peer:
        pbody = dict(body)
        pbody["node"] = peer
        sh("curl -s -X POST %s/set -H 'Content-Type: application/json' -d '%s'"
           % (CAMD, json.dumps(pbody)))
    sh("%s exec %s am force-stop com.android.camera2" % (DOCKER, name))
    sh("%s exec %s setprop ctl.restart vendor.camera-provider-2-4-legacy" % (DOCKER, name))
    import time
    time.sleep(3)
    sh("%s exec %s setprop ctl.restart cameraserver" % (DOCKER, name))
    time.sleep(3)
    sh("%s exec %s am start -n com.android.camera2/com.android.camera.CameraLauncher" % (DOCKER, name))
    for _ in range(20):
        time.sleep(1)
        if app_stream(name):
            break
    time.sleep(2)
    sh("%s exec %s screencap -p /data/local/tmp/geo.png" % (DOCKER, name))
    sh("%s cp %s:/data/local/tmp/geo.png /tmp/geo_%s.png" % (DOCKER, name, name))

    src = probe(media)
    nfmt = node_format(node)
    cam = camd_geom(node)
    geo = cam["geometry"]
    streams = app_stream(name)
    box = screen_bbox("/tmp/geo_%s.png" % name)

    ok = True
    def check(label, cond, detail):
        nonlocal ok
        ok = ok and cond
        print("%s %-34s %s" % ("[PASS]" if cond else "[FAIL]", label, detail))

    check("source size", (src.get("width"), src.get("height")) == (geo["src_w"], geo["src_h"]),
          "file=%sx%s camd=%sx%s" % (src.get("width"), src.get("height"), geo["src_w"], geo["src_h"]))
    check("node format", nfmt == (geo["out_w"], geo["out_h"]),
          "node=%s camd=%sx%s" % (nfmt, geo["out_w"], geo["out_h"]))
    check("app stream matches node", (geo["out_w"], geo["out_h"]) in streams,
          "streams=%s" % sorted(streams))
    if geo.get("fit") == "cover":
        check("content covers buffer", geo["content_x"] <= 0 and geo["content_y"] <= 0
              and geo["content_x"] + geo["content_w"] >= geo["out_w"]
              and geo["content_y"] + geo["content_h"] >= geo["out_h"],
              "rect=%dx%d at (%d,%d)" % (geo["content_w"], geo["content_h"],
                                         geo["content_x"], geo["content_y"]))
    else:
        check("content inside buffer", geo["content_x"] >= 0 and geo["content_y"] >= 0
              and geo["content_x"] + geo["content_w"] <= geo["out_w"]
              and geo["content_y"] + geo["content_h"] <= geo["out_h"],
              "rect=%dx%d at (%d,%d)" % (geo["content_w"], geo["content_h"],
                                         geo["content_x"], geo["content_y"]))
    want_dar = round(geo["src_dar"], 3)
    check("display aspect preserved", abs(geo["display_dar"] - want_dar) <= max(0.01, want_dar * 0.02),
          "display=%.3f source=%.3f" % (geo["display_dar"], want_dar))
    if box and geo.get("fit") == "cover":
        # cover fills the frame, so the picture must reach both preview edges.
        full_w, full_h = 1080, 1416
        check("on-screen fills frame", box[2] >= full_w * 0.96 and box[3] >= full_h * 0.94,
              "screen=%s preview=%dx%d" % (box, full_w, full_h))
    elif box:
        # the camera app maps the node buffer onto the preview area with
        # independent x/y factors (it does not preserve the buffer aspect).
        # The user-visible proportion is therefore the plain screen aspect of
        # the picture, which must equal the source aspect.
        obs_dar = box[2] / float(box[3])
        check("on-screen aspect", abs(obs_dar - want_dar) <= max(0.02, want_dar * 0.03),
              "observed=%.3f source=%.3f screen=%s" % (obs_dar, want_dar, box))
    else:
        check("on-screen rect", False, "no picture found in screenshot")
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
