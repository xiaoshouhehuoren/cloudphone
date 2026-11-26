set -e
cd "$(dirname "$0")"
export TMPDIR="$PWD/tmpb" TMP="$PWD/tmpb" TEMP="$PWD/tmpb"
A=aosp; P=$A/a13_platform_hardware_interfaces/camera/provider/2.4/default; LC=$A/a13_external_libcxx
C=$A/a13_platform_system_core; B=$A/a13_platform_system_libbase; G=$A/a13_platform_system_logging; N=$A/a13_platform_frameworks_native; I=$A/a13_platform_hardware_interfaces; L=$A/libhardware13; V=$A/a13_platform_frameworks_av; HD=$A/a13_platform_system_libhidl; SM=$A/a13_platform_system_media
NDK=android-ndk-r26d/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android33-clang++.exe
DEVALL=""
for w in 1.0 3.2 3.3 3.4 3.5 3.6 3.7; do DEVALL="$DEVALL -I$I/camera/device/$w/default -I$I/camera/device/$w/default/include"; for x in $I/camera/device/$w/default/include/*/; do [ -d "$x" ] && DEVALL="$DEVALL -I$x"; done; done
BASE="$DEVALL -Ihidl-src-out -Ihidl-out -I$A/a13_platform_system_libhwbinder/include -I$A/a13_platform_system_libfmq/include -I$A/a13_platform_system_libfmq/base -I$HD/libhidlmemory/include -I$N/headers/media_plugin -I$N/headers/media_plugin/media/openmax -I$N/libs/gralloc/types/include -I$SM/private/camera/include -I$I/camera/common/1.0/default/include -I$I/camera/common/1.0/default -I$V/camera/include -I$SM/camera/include -I$A/a13_external_tinyxml2 -I$HD/transport/include -I$HD/base/include -I$P -I$I/camera -I$L/include -I$B/include -I$C/libcutils/include -I$C/libsystem/include -I$C/libutils/include -I$G/liblog/include -I$N/libs/binder/include -I$N/libs/utils/include -I$N/libs/nativewindow/include"
FLAGS="-std=c++17 -fPIC -O1 -fno-rtti -fvisibility=hidden -fvisibility-inlines-hidden -nostdinc++ -I$LC/include"
i=0; OBJS=""; F=0
for s in $(find hidl-src-out -name "*.cpp") $A/CameraProvider_legacy_only.cpp $P/LegacyCameraProviderImpl_2_4.cpp $I/camera/common/1.0/default/CameraModule.cpp $I/camera/common/1.0/default/VendorTagDescriptor.cpp $A/HandleImporter_custom.cpp $I/camera/device/1.0/default/CameraDevice.cpp $I/camera/device/3.2/default/CameraDevice.cpp $I/camera/device/3.2/default/CameraDeviceSession.cpp $I/camera/device/3.2/default/Convert.cpp $I/camera/device/3.3/default/CameraDevice.cpp $I/camera/device/3.4/default/CameraDevice.cpp $I/camera/device/3.3/default/CameraDeviceSession.cpp $I/camera/device/3.4/default/CameraDeviceSession.cpp $I/camera/device/3.5/default/CameraDeviceSession.cpp $I/camera/device/3.3/default/Convert.cpp $I/camera/device/3.4/default/Convert.cpp; do
  [ -f "$s" ] || continue; i=$((i+1)); o=tmpb/v_$i.o
  if $NDK $FLAGS -c -o $o $s $BASE 2> tmpb/err_$i.txt; then OBJS="$OBJS $o"; else F=$((F+1)); echo "FAIL $s"; head -20 tmpb/err_$i.txt; fi
done
echo "compiled $(echo $OBJS | wc -w)/$i fails=$F"
$NDK -std=c++17 -fPIC -O1 -fno-rtti -nostdinc++ -I$LC/include -c -o tmpb/vsvc.o $A/service_direct.cpp $BASE || echo VSVC_FAIL
$NDK -o camera.provider-2.4-new $OBJS tmpb/vsvc.o cd35.o meta.o -Llinklibs/ll -lhidlmemory -lhidlbase -lutils -lcutils -llog -lhardware -lcamera_metadata -lbinder -lbase -lfmq -lprocessgroup -lvndksupport -lc++ -ldl 2>&1 | head -20
ls -l camera.provider-2.4-new 2>/dev/null || echo NO_BIN
