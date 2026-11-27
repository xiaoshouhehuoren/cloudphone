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
    print "  <hal format=\"aidl\"><name>android.hardware.sensors</name><version>1</version><interface><name>ISensors</name><instance>default</instance></interface></hal>"
    print "  <hal format=\"aidl\"><name>android.hardware.gnss</name><version>2</version><interface><name>IGnss</name><instance>default</instance></interface></hal>"
  }
  print
}' "$M" > "$T" || exit 1
/system/bin/mv "$T" "$M"
/system/bin/chmod 644 "$M"
echo "vintf: patched sensors + gnss"
