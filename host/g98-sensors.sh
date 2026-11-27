#!/system/bin/sh
# G98 sensor HAL (Java): replays the real-device SENC trace through the
# android.hardware.sensors HIDL slot, exposing 10 Fake* sensors.
# Launched by init (g98-sensors.rc) on boot, and by wx-start.sh for live instances.

# Guard: the service may already be running (manual spawn + init at boot).
if /system/bin/ps -A -o ARGS 2>/dev/null | /system/bin/grep -q '[c]om.g98.sensors.SensorHalJava'; then
  echo "g98-sensors: already running"
  exit 0
fi

export ANDROID_ROOT=/system
export ANDROID_DATA=/data
export ANDROID_ART_ROOT=/apex/com.android.art
export ANDROID_I18N_ROOT=/apex/com.android.i18n
export ANDROID_TZDATA_ROOT=/apex/com.android.tzdata
export ANDROID_RUNTIME_ROOT=/apex/com.android.runtime

TRACE=/data/local/tmp/g98_sensors_trace.bin
if [ ! -f $TRACE ]; then
  if [ -f /vendor/etc/g98_sensors_trace.bin.gz ]; then
    echo "g98-sensors: decompressing trace"
    /system/bin/gzip -dc /vendor/etc/g98_sensors_trace.bin.gz > $TRACE
  elif [ -f /vendor/etc/g98_sensors_trace.bin ]; then
    /system/bin/cp /vendor/etc/g98_sensors_trace.bin $TRACE
  fi
fi
if [ ! -f $TRACE ]; then
  echo "g98-sensors: no trace, exposing sensors with zero data"
fi

SP=
i=0
while [ -z "$SP" ] && [ $i -lt 30 ]; do
  SP=$(/system/bin/pidof system_server 2>/dev/null)
  if [ -z "$SP" ]; then
    SP=$(/system/bin/ps -A -o PID,NAME 2>/dev/null | /system/bin/sed -n 's/^ *\([0-9][0-9]*\) *system_server$/\1/p' | /system/bin/sed -n '1p')
  fi
  [ -n "$SP" ] && break
  i=$((i+1))
  sleep 1
done
if [ -z "$SP" ]; then SP=1; fi
BP=$(/system/bin/tr '\0' '\n' < /proc/$SP/environ 2>/dev/null | /system/bin/sed -n 's/^BOOTCLASSPATH=//p')
if [ -z "$BP" ]; then BP=/apex/com.android.art/javalib/core-oj.jar:/apex/com.android.art/javalib/core-libart.jar:/system/framework/framework.jar; fi
export BOOTCLASSPATH="$BP"
exec /system/bin/app_process -Djava.class.path=/vendor/etc/g98-sensors.dex /system/bin com.g98.sensors.SensorHalJava
