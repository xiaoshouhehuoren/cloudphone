#!/system/bin/sh
# Add the G98 sensor / gnss AIDL HALs to the VINTF manifest.
# Without the manifest entry SensorService refuses the HAL ("No Sensors on the device",
# devInitCheck -19) even though the service is registered with servicemanager.
set -u
M=/vendor/etc/vintf/manifest.xml
[ -f "$M" ] || exit 0
if /system/bin/grep -q 'android.hardware.sensors' "$M"; then
  echo "vintf: sensors already declared"
  exit 0
fi
T=/data/local/tmp/manifest.xml.g98
/system/bin/awk '{
  if ($0 ~ /<\/manifest>/) {
    # 只声明 sensors。声明 gnss 但容器里没有对应 HAL 时 servicemanager 会一直
    # "Waited one second for android.hardware.gnss.IGnss/default"，system_server 卡在
    # 启动阶段（boot_completed 永远为空），PackageManager 解析不出 Activity，
    # 相机 App 就报 "Activity class does not exist"。
    print "  <hal format=\"aidl\"><name>android.hardware.sensors</name><version>1</version><interface><name>ISensors</name><instance>default</instance></interface></hal>"
  }
  print
}' "$M" > "$T" || exit 1
/system/bin/mv "$T" "$M"
/system/bin/chmod 644 "$M"
echo "vintf: patched sensors + gnss"
