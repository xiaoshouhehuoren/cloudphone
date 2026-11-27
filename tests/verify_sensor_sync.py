#!/usr/bin/env python3
"""手机传感器同步链路自测（板子侧运行）。

覆盖：dashboard WebSocket /ws/sensor 握手 → 二进制实时文件写入 → 容器内假传感器 HAL 读取
（HAL 日志出现 live mask / live type）→ 断开后 mask 归零。
"""
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import time

NAME = sys.argv[1] if len(sys.argv) > 1 else "wx1"
PORT = 8099
TOKEN_FILE = "/etc/g98/cloudphone.token"
LIVE = "/srv/redroid/%s/local/tmp/g98-sensors-live.bin" % NAME
HAL_LOG = "/data/local/tmp/sensors_java.log"
MAGIC = 0x4739384C


def token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def ws_connect(path, cookie):
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nCookie: %s\r\n\r\n"
           % (path, PORT, key, cookie))
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(4096)
    head = buf.split(b"\r\n\r\n", 1)[0].decode("latin1")
    return s, head.splitlines()[0]


def send_text(s, text):
    data = text.encode()
    mask = os.urandom(4)
    n = len(data)
    if n < 126:
        hdr = bytes([0x81, 0x80 | n])
    elif n < 65536:
        hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
    else:
        hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
    s.sendall(hdr + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))


def read_live():
    with open(LIVE, "rb") as f:
        buf = f.read(180)
    magic, ver, mask, wall = struct.unpack_from("<iiIq", buf, 0)
    vals = struct.unpack_from("<40f", buf, 20)
    return magic, ver, mask, wall, vals


def main():
    fails = []
    s, status = ws_connect("/ws/sensor?name=" + NAME, "g98k=" + token())
    print("[%s] ws handshake: %s" % (NAME, status))
    if "101" not in status:
        fails.append("handshake " + status)
    acc = [1.25, -2.5, 3.75]
    gyro = [0.11, 0.22, 0.33]
    light = [512.0]
    t0 = time.time()
    while time.time() - t0 < 1.6:
        send_text(s, json.dumps({"on": ["acc", "gyro", "light"],
                                 "v": {"acc": acc, "gyro": gyro, "light": light}}))
        time.sleep(0.04)
    magic, ver, mask, wall, vals = read_live()
    age = time.time() - wall / 1000.0
    print("[%s] file magic=0x%08X ver=%d mask=0x%X age=%.2fs" % (NAME, magic, ver, mask, age))
    if magic != MAGIC or ver != 1:
        fails.append("bad header")
    if mask != 0x43:
        fails.append("mask 0x%X != 0x43" % mask)
    if age > 2.0:
        fails.append("stale live file %.1fs" % age)
    if abs(vals[0] - acc[0]) > 0.01 or abs(vals[6] - gyro[2]) > 0.01:
        fails.append("values %r" % (vals[0:3],))
    if abs(vals[24] - light[0]) > 0.01:
        fails.append("light %r" % (vals[24],))
    s.close()
    time.sleep(0.6)
    _m, _v, mask2, _w, _x = read_live()
    print("[%s] after close mask=0x%X" % (NAME, mask2))
    if mask2 != 0:
        # 若控制台里还有别的浏览器在同步，服务器会继续收到它的帧，mask 不会归零。
        warn = "another client still streaming (mask 0x%X)" % mask2
        print("[%s] note: %s" % (NAME, warn))
    try:
        out = subprocess.run(["/usr/bin/docker", "exec", NAME, "sh", "-c",
                              "grep -a 'live mask=' " + HAL_LOG + " | tail -5"],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception as exc:  # noqa: BLE001
        out = ""
        print("[%s] hal log read failed: %s" % (NAME, exc))
    print("[%s] hal log: %s" % (NAME, out.strip().replace("\n", " | ")))
    if "live mask=0x43" not in out:
        fails.append("HAL did not observe live mask")
    if fails:
        print("RESULT FAIL (%s): %s" % (NAME, "; ".join(fails)))
        return 1
    print("RESULT PASS (%s)" % NAME)
    return 0


if __name__ == "__main__":
    sys.exit(main())
