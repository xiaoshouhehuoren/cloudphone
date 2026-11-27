#!/usr/bin/env python3
"""Ensure only the working legacy camera provider runs on each wx instance.

The unused 2.7 external provider aborts while registering external/0 and init
restarts it forever.  Besides wasting CPU/log volume, that used to fill
/data/tombstones.  The fix is both persistent (disabled in its init rc) and
runtime (service stopped); the legacy provider must still expose two cameras.
"""
import re
import subprocess
import sys

DOCKER = "/usr/bin/docker"
EXT_RC = "/vendor/etc/init/android.hardware.camera.provider@2.7-external-service.rc"


def sh(cmd, timeout=60):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)


def main():
    names = [n for n in sh(DOCKER + " ps --format '{{.Names}}'").stdout.split()
             if re.fullmatch(r"wx[0-9]+", n)]
    if not names:
        print("RESULT FAIL: no wx instances")
        return 1
    failures = []
    for name in sorted(names):
        ext = sh("%s exec %s getprop init.svc.vendor.camera-provider-2-7-ext" % (DOCKER, name)).stdout.strip()
        legacy = sh("%s exec %s getprop init.svc.vendor.camera-provider-2-4-legacy" % (DOCKER, name)).stdout.strip()
        rc = sh("%s exec %s cat %s" % (DOCKER, name, EXT_RC)).stdout
        disabled = bool(re.search(r"(?m)^\s+disabled\s*$", rc))
        dump = sh("%s exec %s dumpsys media.camera" % (DOCKER, name), timeout=120).stdout
        m = re.search(r"Number of camera devices:\s*(\d+)", dump)
        devices = int(m.group(1)) if m else -1
        ok = ext in ("", "stopped") and legacy == "running" and disabled and devices == 2
        print("%s ext=%r legacy=%r rc_disabled=%s devices=%d %s"
              % (name, ext, legacy, disabled, devices, "PASS" if ok else "FAIL"))
        if not ok:
            failures.append(name)
    if failures:
        print("RESULT FAIL: " + ", ".join(failures))
        return 1
    print("RESULT PASS (%d/%d)" % (len(names), len(names)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
