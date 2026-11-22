#!/usr/bin/env python3
"""Keep g98-proxy.py alive without an admin service.

Launched hidden at logon from HKCU Run:
    pythonw.exe g98-proxy-supervisor.py
"""

import msvcrt
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE / "g98-proxy-supervisor.log"
LOCK = HERE / "g98-proxy-supervisor.lock"


def log(message):
    line = "%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), message)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line)


def main():
    lock = LOCK.open("w")
    lock.write("x")
    lock.flush()
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return
    py = Path(sys.executable)
    if py.name.lower() == "python.exe":
        candidate = py.with_name("pythonw.exe")
        if candidate.exists():
            py = candidate
    log("supervisor start pid=%d python=%s" % (os.getpid(), py))
    while True:
        proc = subprocess.Popen(
            [str(py), str(HERE / "g98-proxy.py")],
            cwd=str(HERE),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log("proxy start pid=%d" % proc.pid)
        code = proc.wait()
        log("proxy exit rc=%s" % code)
        time.sleep(5)


if __name__ == "__main__":
    main()
