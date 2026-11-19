cd /c/Users/rosetta/Desktop/g98
export TMPDIR="C:/Users/rosetta/Desktop/g98/tmpb" TMP="C:/Users/rosetta/Desktop/g98/tmpb" TEMP="C:/Users/rosetta/Desktop/g98/tmpb"
A=aosp; L=$A/libhardware13; LC=$A/a13_external_libcxx
C=$A/a13_platform_system_core; B=$A/a13_platform_system_libbase; G=$A/a13_platform_system_logging; M=$A/a13_platform_system_media; N=$A/a13_platform_frameworks_native; I=$A/a13_platform_hardware_interfaces; V=$A/a13_platform_frameworks_av
NDK=android-ndk-r26d/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android33-clang++.exe
INC="-nostdinc++ -Ia13arc -I$M/private/camera/include -I$LC/include -include $B/include/android-base/macros.h -I$A/compat -I$V/camera/include -I$N/libs/binder/include -I$L/include -I$B/include -I$C/libcutils/include -I$C/libsync/include -I$C/libsystem/include -I$C/libutils/include -I$M/camera/include -I$G/liblog/include -I$I/camera -I$L/modules/camera/3_4 -I$N/libs/nativewindow/include -I$N/libs/utils/include"
SRC=$(find $L/modules/camera/3_4 -name "*.cpp" | grep -v test | grep -v "arc/exif_utils.cpp" | grep -v "arc/jpeg_compressor.cpp")
echo "SRC_COUNT=$(echo $SRC | wc -w)  ARC_COUNT=$(echo $SRC | tr ' ' '\n' | grep -c '/arc/')"
rm -f camera_v7.so
$NDK -std=c++17 -fPIC -O1 -fno-rtti -shared -o camera_v7.so $SRC a13arc/libyuv_shim.cpp a13arc/jpeg_encoder.cpp a13arc/jo_jpeg.cpp $V/camera/CameraMetadata.cpp $V/camera/VendorTagDescriptor.cpp sync_stub.cpp $INC -Ldl_libs -lhardware -Llinklibs/ll -llog -ldl -lutils -lcutils -lc++ -lbase -lcamera_metadata 2>&1 | grep -E "error:|undefined reference|warning: undefined" | head -10
ls -l camera_v7.so 2>/dev/null || echo NO_SO
echo "--- NEEDED:"
android-ndk-r26d/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-readelf.exe -d camera_v7.so 2>/dev/null | grep NEEDED
echo "--- arc symbols:"
android-ndk-r26d/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-nm.exe -D camera_v7.so 2>/dev/null | grep "arc" | head -20
echo "--- undefined count:"
android-ndk-r26d/toolchains/llvm/prebuilt/windows-x86_64/bin/llvm-nm.exe -D --undefined-only camera_v7.so 2>/dev/null | grep -c " U "
