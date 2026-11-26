#!/usr/bin/env python3
"""Measure the picture rect inside a phone screenshot (rgb24 via ffmpeg)."""
import subprocess, sys

FFMPEG = "/root/ffmpeg"

def raw(path):
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", path,
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    return subprocess.run(cmd, capture_output=True, check=True).stdout

def bbox(data, w, h, thresh=26, top=140, bottom=300, row_frac=0.5, col_frac=0.5):
    rows = []
    for y in range(top, h - bottom):
        row = data[y * w * 3:(y + 1) * w * 3]
        cnt = sum(1 for x in range(0, w, 4) if max(row[x*3:x*3+3]) > thresh)
        if cnt > (w / 4) * row_frac:
            rows.append(y)
    if not rows:
        return None
    y0, y1 = rows[0], rows[-1]
    cols = []
    for x in range(0, w, 2):
        hit = 0
        for y in range(y0, y1 + 1, 4):
            i = (y * w + x) * 3
            if max(data[i:i+3]) > thresh:
                hit += 1
        if hit > ((y1 - y0) // 4) * col_frac:
            cols.append(x)
    if not cols:
        return None
    return (cols[0], y0, cols[-1] + 2, y1 + 1)

if __name__ == "__main__":
    path = sys.argv[1]
    w = int(sys.argv[2]) if len(sys.argv) > 2 else 1080
    h = int(sys.argv[3]) if len(sys.argv) > 3 else 2400
    data = raw(path)
    box = bbox(data, w, h)
    if not box:
        print("bbox none"); sys.exit(1)
    x0, y0, x1, y1 = box
    cw, ch = x1 - x0, y1 - y0
    print("bbox x=%d y=%d w=%d h=%d dar=%.4f" % (x0, y0, cw, ch, cw / float(ch)))
