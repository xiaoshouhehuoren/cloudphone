#!/system/bin/sh
export ANDROID_ROOT=/system
export ANDROID_DATA=/data
export ANDROID_ART_ROOT=/apex/com.android.art
export ANDROID_I18N_ROOT=/apex/com.android.i18n
export ANDROID_TZDATA_ROOT=/apex/com.android.tzdata
export ANDROID_RUNTIME_ROOT=/apex/com.android.runtime
SP=$(/system/bin/pidof system_server)
export BOOTCLASSPATH="$(/system/bin/tr '\0' '\n' < /proc/$SP/environ | /system/bin/sed -n 's/^BOOTCLASSPATH=//p')"
echo "system_server=$SP bootclasspath_len=${#BOOTCLASSPATH}"
rm -f /data/local/tmp/cam1small.out
/system/bin/app_process -Djava.class.path=/data/local/tmp/cam1small.dex /system/bin com.g98.probe.Cam1Small "$@" >/data/local/tmp/cam1small.out 2>&1 &
pid=$!
sleep 22
kill "$pid" 2>/dev/null
wait "$pid" 2>/dev/null
echo ---OUT
cat /data/local/tmp/cam1small.out 2>&1
echo ---LOG
cat /data/local/tmp/cam1small.log 2>&1