#!/bin/bash
# Collect the running board's camera stack into ./artifacts for the Docker build.
# Usage: ./collect_artifacts.sh [container]   (default wx1)
set -euo pipefail
export MSYS2_ARG_CONV_EXCL="*" MSYS_NO_PATHCONV=1
export G98_HOST="${G98_HOST:-192.168.5.111}"
H="${G98_HOST:-192.168.5.111}"
C="${1:-wx1}"
DIR="$(cd "$(dirname "$0")" && pwd)"
D="$DIR/artifacts"
mkdir -p "$D"

if [ -z "${SSH_ASKPASS:-}" ]; then
    for cand in "$DIR/../../_askpass.cmd" "$DIR/../_askpass.cmd"; do
        if [ -f "$cand" ]; then
            export SSH_ASKPASS="$(cygpath -w "$cand" 2>/dev/null || echo "$cand")" SSH_ASKPASS_REQUIRE=force
            export DISPLAY="${DISPLAY:-:0}"
            break
        fi
    done
fi
SSHOPT="-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no"

sshx() { ssh $SSHOPT "root@$H" "$@"; }

pull() {   # $1 = path inside the container
    local path="$1" name tmp
    name="$(basename "$path")"
    tmp="$(mktemp)"
    # base64 over ssh avoids scp path translation and works for any file size.
    sshx "/usr/bin/docker exec $C base64 -w0 '$path'" > "$tmp"
    base64 -d "$tmp" > "$D/$name"
    rm -f "$tmp"
    if [ ! -s "$D/$name" ]; then
        echo "FAILED to pull $path" >&2
        return 1
    fi
    printf '%s  %-58s %8d B\n' "$(sha256sum "$D/$name" | cut -d' ' -f1)" "$name" "$(stat -c%s "$D/$name")"
}

echo "collecting from $C @ $H -> $D"
pull /vendor/bin/hw/android.hardware.camera.provider@2.4-service
pull /vendor/lib64/hw/camera.mt6983.so
pull /vendor/lib64/hw/gralloc.g98.so
pull /vendor/etc/g98-sensors.dex
pull /vendor/etc/init/g98-sensors.rc
pull /vendor/etc/init/android.hardware.camera.provider@2.4-service.rc
pull /vendor/etc/init/android.hardware.camera.provider@2.7-external-service.rc
pull /vendor/bin/g98-sensors.sh
# The VINTF manifest must declare the sensors/gnss AIDL HALs, otherwise
# SensorService ignores the registered HAL (devInitCheck -19). Keep the
# artifact name (vintf-manifest.xml) in sync with the Dockerfile COPY.
pull /vendor/etc/vintf/manifest.xml
mv -f "$D/manifest.xml" "$D/vintf-manifest.xml"
pull /vendor/etc/g98_sensors_trace.bin.gz
# The legacy camera API (API1: WeChat face verification, QR scanners) derives its
# video-size upper bound from <VideoEncoderCap maxFrameWidth/maxFrameHeight>; the
# stock file caps at 1280x720, so a 1280x960 node yields an empty size list and
# cameraserver fails with "generated preview size list is empty!!" (-22 / -19).
pull /vendor/etc/media_profiles_V1_0.xml
echo "done: $(ls -1 "$D" | wc -l) artifacts"