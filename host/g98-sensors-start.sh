#!/system/bin/sh
# Runs inside a redroid container: install perms, start g98-sensors, report state.
set -e
chmod 755 /vendor/bin/g98-sensors.sh
chmod 644 /vendor/etc/g98-sensors.dex /vendor/etc/init/g98-sensors.rc 2>/dev/null || true
chmod 644 /vendor/etc/g98_sensors_trace.bin.gz 2>/dev/null || true

/system/bin/setprop ctl.start g98-sensors 2>/dev/null || true
sleep 1
if ! /system/bin/ps -A -o ARGS 2>/dev/null | /system/bin/grep -q '[c]om.g98.sensors.SensorHalJava'; then
  /system/bin/setsid /system/bin/sh /vendor/bin/g98-sensors.sh >/data/local/tmp/g98-sensors.log 2>&1 &
  sleep 2
fi

RUN=$(/system/bin/ps -A -o ARGS 2>/dev/null | /system/bin/grep -c '[c]om.g98.sensors.SensorHalJava' || true)
FAKE=$(/system/bin/dumpsys sensorservice 2>/dev/null | /system/bin/grep -c 'Fake ' || true)
TRACE=$(/system/bin/ls -l /data/local/tmp/g98_sensors_trace.bin 2>/dev/null | /system/bin/awk '{print $5}' || true)
echo "runner=$RUN fake_sensors=$FAKE trace_bytes=${TRACE:-0}"
