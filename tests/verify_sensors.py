#!/usr/bin/env python3
"""Verify the G98 sensor stack inside every running redroid instance.

Checks, per instance:
  1. the Java HAL process (com.g98.sensors.SensorHalJava) is alive
  2. the AIDL service android.hardware.sensors.ISensors/default is registered
  3. SensorService reports "Total 10 h/w sensors" (the real-device SENC trace)
  4. the ten expected sensor names are present
  5. devInitCheck is not -19 (ENODEV)
Exit code 0 only when every instance passes.
"""
import re
import subprocess
import sys

EXPECTED = [
    "Fake Accelerometer", "Fake Gyroscope", "Fake Magnetometer",
    "Fake Rotation Vector", "Fake Gravity", "Fake Linear Acceleration",
    "Fake Light", "Fake Proximity", "Fake Pressure", "Fake Step Counter",
]


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def instances():
    out = sh("/usr/bin/docker ps --format '{{.Names}}'")
    return [n for n in out.split() if re.fullmatch(r"wx[0-9]+", n)]


def check(name):
    fails = []
    proc = sh(f"/usr/bin/docker exec {name} sh -c \"ps -A -o ARGS | grep -c '[c]om.g98.sensors'\"").strip()
    if proc not in ("1", "2"):
        fails.append(f"HAL process count={proc!r}")
    svc = sh(f"/usr/bin/docker exec {name} sh -c \"service list | grep -c 'android.hardware.sensors.ISensors/default'\"").strip()
    if svc != "1":
        fails.append("ISensors service not registered")
    dump = sh(f"/usr/bin/docker exec {name} dumpsys sensorservice 2>/dev/null")
    m = re.search(r"Total (\d+) h/w sensors", dump)
    total = int(m.group(1)) if m else 0
    if total != len(EXPECTED):
        fails.append(f"h/w sensors={total} (want {len(EXPECTED)})")
    missing = [s for s in EXPECTED if s not in dump]
    if missing:
        fails.append("missing: " + ", ".join(missing[:3]))
    if re.search(r"devInitCheck\s*:\s*-19", dump):
        fails.append("devInitCheck=-19")
    return fails, total


def main():
    targets = sys.argv[1:] or instances()
    bad = 0
    for name in targets:
        fails, total = check(name)
        if fails:
            bad += 1
            print(f"[FAIL] {name} sensors={total}: " + "; ".join(fails))
        else:
            print(f"[PASS] {name} sensors={total} (HAL up, AIDL registered)")
    print(f"RESULT {'PASS' if bad == 0 else 'FAIL'} ({len(targets) - bad}/{len(targets)})")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
