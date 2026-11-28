#!/usr/bin/env python3
"""Verify device-profile guardrails, excluded-hardware XML, and regional settings.

Usage:
  python3 verify_device_coherence.py [--check-pm] [wx5 ...]
Without container args, checks XML and wx-start.sh profile guardrails only.
"""
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

EXCLUDED = {
    "android.hardware.camera.flash",
    "android.hardware.camera.external",
    "android.hardware.camera.autofocus",
    "android.hardware.location.network",
}
MUST_KEEP = {
    "android.hardware.camera.any",
    "android.hardware.camera.front",
}


def find_file(candidates):
    for p in candidates:
        if Path(p).is_file():
            return Path(p)
    return None


def sh(cmd, env=None, timeout=60):
    return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)


def check_xml(fails):
    here = Path(__file__).resolve().parent
    xml_path = find_file([
        "/root/artifacts/g98-excluded-hardware.xml",
        str(here.parent / "deploy" / "g98-excluded-hardware.xml"),
    ])
    if not xml_path:
        fails.append("g98-excluded-hardware.xml not found")
        return
    root = ET.parse(str(xml_path)).getroot()
    names = {el.get("name") for el in root.findall("unavailable-feature")}
    print("XML:", xml_path, "excluded=", sorted(names))
    if names != EXCLUDED:
        fails.append("XML excluded set %s != expected %s" % (sorted(names), sorted(EXCLUDED)))
    if names & MUST_KEEP:
        fails.append("XML must not exclude required camera features")


def check_wx_start(fails):
    here = Path(__file__).resolve().parent
    script = find_file([
        "/srv/redroid/wx-start.sh",
        str(here.parent / "build" / "wx-start.sh"),
    ])
    if not script:
        fails.append("wx-start.sh not found")
        return
    text = script.read_text(encoding="utf-8")
    if "persist.sys.time_zone" in text:
        fails.append("wx-start.sh still uses typo persist.sys.time_zone instead of persist.sys.timezone")
    if "persist.sys.timezone" not in text:
        fails.append("wx-start.sh missing persist.sys.timezone")
    if "bash" and os.name != "nt":
        r = sh(["bash", "-n", str(script)])
        if r.returncode != 0:
            fails.append("bash -n wx-start.sh failed: " + r.stderr.strip())
        # k50 without fingerprint must fail closed before touching docker
        env = os.environ.copy()
        env["G98_DEVICE_PROFILE"] = "k50"
        env.pop("G98_K50_FINGERPRINT", None)
        r2 = sh(["bash", str(script), "9"], env=env)
        if r2.returncode == 0 or "G98_K50_FINGERPRINT" not in r2.stderr:
            fails.append("k50 without G98_K50_FINGERPRINT did not fail closed: rc=%s err=%s" % (r2.returncode, r2.stderr.strip()))
        env["G98_K50_FINGERPRINT"] = "bad/fingerprint"
        r3 = sh(["bash", str(script), "9"], env=env)
        if r3.returncode == 0 or "invalid G98_K50_FINGERPRINT" not in r3.stderr:
            fails.append("k50 with malformed fingerprint did not fail closed")
        env["G98_DEVICE_PROFILE"] = "unknown"
        r4 = sh(["bash", str(script), "9"], env=env)
        if r4.returncode == 0:
            fails.append("unsupported profile did not fail closed")
        print("wx-start.sh guardrails: syntax + k50 fail-closed checks OK")


def check_container(name, check_pm, fails):
    tz = sh(["/usr/bin/docker", "exec", name, "getprop", "persist.sys.timezone"]).stdout.strip()
    loc = sh(["/usr/bin/docker", "exec", name, "getprop", "persist.sys.locale"]).stdout.strip()
    sloc = sh(["/usr/bin/docker", "exec", name, "settings", "get", "global", "system_locales"]).stdout.strip()
    has_xml = sh(["/usr/bin/docker", "exec", name, "test", "-f", "/system/etc/permissions/g98-excluded-hardware.xml"]).returncode == 0
    print("== %s == tz=%s locale=%s system_locales=%s excluded_xml=%s" % (name, tz, loc, sloc, has_xml))
    if tz != "Asia/Shanghai":
        fails.append("%s persist.sys.timezone=%r != Asia/Shanghai" % (name, tz))
    if loc != "zh-CN":
        fails.append("%s persist.sys.locale=%r != zh-CN" % (name, loc))
    if sloc != "zh-CN":
        fails.append("%s system_locales=%r != zh-CN" % (name, sloc))
    if not has_xml:
        fails.append("%s missing /system/etc/permissions/g98-excluded-hardware.xml" % name)
    if check_pm:
        feats = set()
        for line in sh(["/usr/bin/docker", "exec", name, "pm", "list", "features"]).stdout.splitlines():
            if line.startswith("feature:"):
                feats.add(line.split(":", 1)[1].split("=", 1)[0].strip())
        leaked = sorted(EXCLUDED & feats)
        missing = sorted(MUST_KEEP - feats)
        print("  pm features: leaked_excluded=%s missing_required=%s" % (leaked, missing))
        if leaked:
            fails.append("%s pm still advertises excluded features: %s" % (name, leaked))
        if missing:
            fails.append("%s pm missing required features: %s" % (name, missing))


def main():
    args = sys.argv[1:]
    check_pm = "--check-pm" in args
    containers = [a for a in args if a != "--check-pm"]
    fails = []
    check_xml(fails)
    check_wx_start(fails)
    for c in containers:
        check_container(c, check_pm, fails)
    for f in fails:
        print("[FAIL]", f)
    print("RESULT " + ("PASS" if not fails else "FAIL"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
