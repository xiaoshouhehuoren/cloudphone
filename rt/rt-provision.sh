#!/system/bin/sh
# WorkPhone RT provisioning: seed Agent enrollment prefs and start service.
# Usage: rt-provision.sh <credential_id> <api_base>
set -u
PKG=com.salespartner.workphone.agent
if [ $# -lt 2 ]; then
  echo "usage: rt-provision.sh <credential_id> <api_base>"
  exit 1
fi
CRED="$1"
API="$2"
if ! pm path "$PKG" >/dev/null 2>&1; then
  echo "ERROR: $PKG not installed"
  exit 2
fi
# First start creates /data/data/$PKG and workphone_device_identity.xml (device_id).
am force-stop "$PKG"
am start-foreground-service -n "$PKG/.WorkPhoneAgentService" >/dev/null 2>&1 || true
sleep 4
am force-stop "$PKG"
sleep 1
UID=$(stat -c %u "/data/data/$PKG")
DIR="/data/data/$PKG/shared_prefs"
mkdir -p "$DIR"
cat > "$DIR/workphone_native_v4_enrollment.xml" <<EOF
<?xml version='1.0' encoding='utf-8' standalone='yes' ?>
<map>
    <string name="credential_id">$CRED</string>
    <string name="api_base">$API</string>
</map>
EOF
chown "$UID:$UID" "$DIR" "$DIR/workphone_native_v4_enrollment.xml"
chmod 771 "$DIR"
chmod 660 "$DIR/workphone_native_v4_enrollment.xml"
am start-foreground-service -n "$PKG/.WorkPhoneAgentService" >/dev/null 2>&1 || true
sleep 2
echo "provisioned $PKG credential=$CRED api=$API uid=$UID"