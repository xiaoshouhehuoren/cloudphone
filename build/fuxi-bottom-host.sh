#!/bin/sh
set -eu
R=${1:?usage: fuxi-bottom-host.sh <root>}
FP='Xiaomi/fuxi/fuxi:13/TKQ1.221114.001/V14.0.9.0.TMCCNXM:user/release-keys'
FILES="$R/system/build.prop $R/vendor/build.prop $R/odm/etc/build.prop $R/product/etc/build.prop $R/system_ext/etc/build.prop $R/vendor_dlkm/etc/build.prop"
for f in $FILES; do
  [ -f "$f" ] || continue
  sed -i -E \
    -e 's/^(ro\.product\.(system|vendor|odm|product|system_ext|vendor_dlkm|odm_dlkm)\.brand=).*/\1Xiaomi/' \
    -e 's/^(ro\.product\.(system|vendor|odm|product|system_ext|vendor_dlkm|odm_dlkm)\.manufacturer=).*/\1Xiaomi/' \
    -e 's/^(ro\.product\.(system|vendor|odm|product|system_ext|vendor_dlkm|odm_dlkm)\.device=).*/\1fuxi/' \
    -e 's/^(ro\.product\.(system|vendor|odm|product|system_ext|vendor_dlkm|odm_dlkm)\.name=).*/\1fuxi/' \
    -e 's/^(ro\.product\.(system|vendor|odm|product|system_ext|vendor_dlkm|odm_dlkm)\.model=).*/\12211133C/' \
    -e 's/^(ro\.product\.(brand|manufacturer)=).*/\1Xiaomi/' \
    -e 's/^(ro\.product\.(device|name)=).*/\1fuxi/' \
    -e 's/^(ro\.product\.model=).*/\12211133C/' \
    -e 's/^(ro\.product\.board=).*/\1fuxi/' \
    -e 's/^(ro\.hardware=).*/\1qcom/' \
    -e 's/^(ro\.boot\.hardware=).*/\1qcom/' \
    "$f"
done
S="$R/system/build.prop"
sed -i -E \
  -e 's#^ro\.build\.description=.*#ro.build.description=fuxi-user 13 TKQ1.221114.001 V14.0.9.0.TMCCNXM release-keys#' \
  -e 's#^ro\.build\.display\.id=.*#ro.build.display.id=fuxi-user 13 TKQ1.221114.001 V14.0.9.0.TMCCNXM release-keys#' \
  -e 's/^ro\.build\.flavor=.*/ro.build.flavor=fuxi-user/' \
  -e 's/^ro\.build\.product=.*/ro.build.product=fuxi/' \
  -e 's/^ro\.build\.type=.*/ro.build.type=user/' \
  -e 's/^ro\.build\.tags=.*/ro.build.tags=release-keys/' \
  -e "s#^ro\.build\.fingerprint=.*#ro.build.fingerprint=$FP#" \
  -e "s#^ro\.system\.build\.fingerprint=.*#ro.system.build.fingerprint=$FP#" \
  "$S"
for f in $FILES; do
  [ -f "$f" ] || continue
  if grep -q '^ro\.board\.platform=' "$f"; then
    sed -i 's/^ro\.board\.platform=.*/ro.board.platform=kalama/' "$f"
  fi
done
grep -q '^ro\.board\.platform=' "$S" || echo 'ro.board.platform=kalama' >> "$S"
sed -i -E "s#^ro\.vendor\.build\.fingerprint=.*#ro.vendor.build.fingerprint=$FP#" "$R/vendor/build.prop"
sed -i -E "s#^ro\.odm\.build\.fingerprint=.*#ro.odm.build.fingerprint=$FP#" "$R/odm/etc/build.prop"
sed -i -E "s#^ro\.product\.build\.fingerprint=.*#ro.product.build.fingerprint=$FP#" "$R/product/etc/build.prop"
sed -i -E "s#^ro\.system_ext\.build\.fingerprint=.*#ro.system_ext.build.fingerprint=$FP#" "$R/system_ext/etc/build.prop"
sed -i -E "s#^ro\.vendor_dlkm\.build\.fingerprint=.*#ro.vendor_dlkm.build.fingerprint=$FP#" "$R/vendor_dlkm/etc/build.prop"
echo "fuxi bottom props patched under $R"
