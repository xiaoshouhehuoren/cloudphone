#!/usr/bin/env python3
"""Wait until a camd node really receives new source frames again.

camd holds the last frame when an RTSP publisher goes away, so written keeps
growing even though the picture is frozen. Poll source_frames + holding instead
and tell the operator when it is safe to start a face-verification attempt.
"""
import json, sys, time, urllib.request

CAMD = "http://127.0.0.1:8100"
NODE = sys.argv[1] if len(sys.argv) > 1 else "/dev/video20"
TIMEOUT = int(sys.argv[2]) if len(sys.argv) > 2 else 600


def sample():
    data = json.load(urllib.request.urlopen(CAMD + "/state", timeout=5))
    for n in data["nodes"]:
        if n["node"] == NODE:
            return n.get("source_frames") or 0, bool(n.get("holding")), n.get("last_frame_age")
    raise SystemExit("node not found: " + NODE)


def main():
    last, _hold, _age = sample()
    deadline = time.time() + TIMEOUT
    i = 0
    while time.time() < deadline:
        src, hold, age = sample()
        if src > last and not hold:
            print("STREAM_LIVE node=%s src=%d age=%s" % (NODE, src, age), flush=True)
            return 0
        if i % 15 == 0:
            print("waiting node=%s src=%d hold=%s age=%s" % (NODE, src, hold, age), flush=True)
        last = max(last, src)
        i += 1
        time.sleep(2)
    print("STILL_NO_STREAM node=%s after %ds" % (NODE, TIMEOUT), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
