#!/usr/bin/env python3
"""Ensure the mobile /cam publisher caps capture and WebRTC sending at 30 fps."""
import pathlib
import sys
import urllib.request


def main():
    token = pathlib.Path('/etc/g98/cloudphone.token').read_text().strip()
    req = urllib.request.Request(
        'http://127.0.0.1:8099/cam?dev=wx1&node=/dev/video21',
        headers={'Cookie': 'g98k=' + token})
    with urllib.request.urlopen(req, timeout=15) as response:
        page = response.read().decode('utf-8')
    checks = {
        'capture fps constant': 'CAPTURE_FPS=30' in page,
        'getUserMedia max 30': 'frameRate:{ideal:CAPTURE_FPS,max:CAPTURE_FPS}' in page,
        'both camera openings constrained': page.count('getUserMedia(cameraConstraints())') == 2,
        'WebRTC sender max 30': 'maxFramerate=CAPTURE_FPS' in page,
        'sender bitrate bounded': 'maxBitrate=2500000' in page,
        'actual sender fps visible': "sendFps.toFixed(1)+' fps" in page,
        'real track settings visible': 't.getSettings?t.getSettings():{}' in page,
        'quality limitation visible': 'qualityLimitationReason' in page,
    }
    failed = False
    for name, ok in checks.items():
        print(('PASS ' if ok else 'FAIL ') + name)
        failed |= not ok
    print('RESULT ' + ('FAIL' if failed else 'PASS'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
