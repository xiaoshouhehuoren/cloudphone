# G98 cloud phone image: redroid-panthor base + our camera stack
#
# The heavy vendor blobs (camera provider, camera HAL, gralloc wrapper, sensors
# dex) are collected from a working board by ./collect_artifacts.sh into
# ./artifacts/ before building; they are git-ignored on purpose.
FROM g98/redroid-panthor-rt:13-fuxi-v3

# --- camera provider (HIDL 2.4 legacy, built from hal/ with HandleImporter fix)
COPY artifacts/camera.provider-2.4-service /vendor/bin/hw/android.hardware.camera.provider@2.4-service
# --- camera HAL module (V4L2 wrapper with poll() wait + handle lifetime fixes)
COPY artifacts/camera.mt6983.so /vendor/lib64/hw/camera.mt6983.so
COPY artifacts/camera.mt6983.so /vendor/lib64/hw/camera.redroid.so
# --- gralloc wrapper (dual ABI g98 device around cros gralloc)
COPY artifacts/gralloc.g98.so /vendor/lib64/hw/gralloc.g98.so
# --- sensors HAL replacement
COPY artifacts/g98-sensors.dex /vendor/etc/g98-sensors.dex
COPY artifacts/g98-sensors.rc /vendor/etc/init/g98-sensors.rc
# --- camera provider init overrides (per-instance nodes injected at boot)
COPY artifacts/android.hardware.camera.provider@2.4-legacy-service.rc /vendor/etc/init/android.hardware.camera.provider@2.4-legacy-service.rc
COPY artifacts/android.hardware.camera.provider@2.7-external-service.rc /vendor/etc/init/android.hardware.camera.provider@2.7-external-service.rc

ENV ro.hardware.gralloc=g98

RUN chmod 644 /vendor/lib64/hw/camera.mt6983.so /vendor/lib64/hw/camera.redroid.so \
              /vendor/lib64/hw/gralloc.g98.so /vendor/bin/hw/android.hardware.camera.provider@2.4-service
