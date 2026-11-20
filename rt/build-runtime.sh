#!/usr/bin/env bash
set -euo pipefail
: "${ROM_DIR:?set ROM_DIR to xiaoshouhehuoren-rom-main path}"
: "${ANDROID_JAR:?set ANDROID_JAR to platforms/android-35/android.jar}"
: "${D8_JAR:?set D8_JAR to build-tools/35.0.0/lib/d8.jar}"
WORK="${OUT_DIR:?set OUT_DIR}"
rm -rf "${WORK}" && mkdir -p "${WORK}/classes" "${WORK}/dex"
find "${ROM_DIR}/hook_runtime" -name '*.java' | sort > "${WORK}/sources.txt"
javac -source 8 -target 8 -bootclasspath "${ANDROID_JAR}" -d "${WORK}/classes" @"${WORK}/sources.txt"
jar cf "${WORK}/runtime_classes.jar" -C "${WORK}/classes" .
java -cp "${D8_JAR}" com.android.tools.r8.D8 --min-api 33 --output "${WORK}/dex" "${WORK}/runtime_classes.jar"
cp "${WORK}/dex/classes.dex" "${WORK}/classes5.dex"
echo "built ${WORK}/classes5.dex"
