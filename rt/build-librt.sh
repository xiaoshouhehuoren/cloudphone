#!/usr/bin/env bash
set -euo pipefail
: "${ROM_DIR:?set ROM_DIR to xiaoshouhehuoren-rom-main path}"
: "${NDK_BIN:?set NDK_BIN to android-ndk-r26d/toolchains/llvm/prebuilt/windows-x86_64/bin}"
CXX="${NDK_BIN}/aarch64-linux-android33-clang++.exe"
cd "${ROM_DIR}/hook_runtime"
"${CXX}" -std=c++20 -fPIC -shared -O2 -static-libstdc++ -fexceptions -frtti \
  -DFMT_HEADER_ONLY=1 -Wno-unused-parameter -Wno-deprecated-declarations \
  -Wno-c++11-narrowing -Wno-psabi -Wno-c++2b-extensions \
  -I native -I third_party/lspd_jni/include -I third_party/lspd_jni/src \
  -I third_party/fmt/include -I third_party \
  -I third_party/lsplant/lsplant/src/main/jni/include \
  -I third_party/lsplant/lsplant/src/main/jni \
  -I third_party/dex_builder/include \
  native/runtime_jni.cpp \
  third_party/lspd_jni/src/elf_util.cpp \
  third_party/lsplant/lsplant/src/main/jni/lsplant.cc \
  third_party/dex_builder/dex_builder.cc third_party/dex_builder/dex_helper.cc \
  third_party/dex_builder/slicer/*.cc third_party/dex_builder/slicer/*.cpp \
  -llog -ldl -lz -o "${OUT:?set OUT to output librt.so path}"
