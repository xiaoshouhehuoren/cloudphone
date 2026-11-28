# G98 cloud phone image: redroid-panthor base + G98 camera stack
#
# Vendor blobs are collected from a working board by ./collect_artifacts.sh
# into ./artifacts/ (git-ignored). NOTE: the redroid base image has no /bin/sh,
# so this Dockerfile uses only COPY and no RUN; file modes come from the (pre-chmod-ed) artifacts.
FROM g98/redroid-panthor-rt:13-fuxi-v3

# --- camera provider (HIDL 2.4 legacy, HandleImporter clone+registerBuffer fix)
COPY artifacts/android.hardware.camera.provider@2.4-service /vendor/bin/hw/android.hardware.camera.provider@2.4-service
# --- camera HAL module (V4L2 wrapper: poll() wait, requeue, handle lifetime)
COPY artifacts/camera.mt6983.so /vendor/lib64/hw/camera.mt6983.so
COPY artifacts/camera.mt6983.so /vendor/lib64/hw/camera.redroid.so
# --- gralloc wrapper (dual ABI g98 device around cros gralloc)
COPY artifacts/gralloc.g98.so /vendor/lib64/hw/gralloc.g98.so
# --- sensors HAL replacement
COPY artifacts/g98-sensors.dex /vendor/etc/g98-sensors.dex
COPY artifacts/g98-sensors.rc /vendor/etc/init/g98-sensors.rc
COPY artifacts/g98-sensors.sh /vendor/bin/g98-sensors.sh
COPY artifacts/g98_sensors_trace.bin.gz /vendor/etc/g98_sensors_trace.bin.gz
# --- VINTF: declare the sensors/gnss AIDL HALs, otherwise SensorService
#     ignores the registered HAL ("No Sensors on the device", devInitCheck -19)
COPY artifacts/vintf-manifest.xml /vendor/etc/vintf/manifest.xml
# --- media_profiles: h264/h263/m4v VideoEncoderCap raised to 1920x1440 so the
#     legacy camera API1 (WeChat face verification) can build its size lists
#     for the 1280x960 v4l2 nodes (stock caps at 1280x720 -> empty list -> -22)
COPY artifacts/media_profiles_V1_0.xml /vendor/etc/media_profiles_V1_0.xml
# --- soft AVC encoder: convert byte-buffer NV12 to I420 (WeChat face video) ---
COPY artifacts/libstagefright_soft_avcenc.so /vendor/lib/libstagefright_soft_avcenc.so
# --- hardware feature exclusions: stop claiming flash/AF/external-cam/network-loc ---
COPY g98-excluded-hardware.xml /system/etc/permissions/g98-excluded-hardware.xml
# --- camera provider init overrides (per-instance nodes injected at boot)
COPY artifacts/android.hardware.camera.provider@2.4-service.rc /vendor/etc/init/android.hardware.camera.provider@2.4-service.rc
COPY artifacts/android.hardware.camera.provider@2.7-external-service.rc /vendor/etc/init/android.hardware.camera.provider@2.7-external-service.rc

ENV ro.hardware.gralloc=g98