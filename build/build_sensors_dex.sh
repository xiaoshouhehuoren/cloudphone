#!/bin/bash
# 构建 Java 传感器 HAL 的 dex（com.g98.sensors.SensorHalJava -> classes.dex）。
# 依赖（不随仓库分发，需自备）：
#   rtwork/android-hidden.jar    隐藏 API 打包（含 aidl/fmq 生成类所需符号）
#   .tmp_sensors_build/genjava/  android.hardware.sensors AIDL 生成的 java 源码
#   rtwork/jdk17/                JDK 17（javac）
#   rtwork/sdk/build-tools/35.0.0/d8.bat
# 产物：.tmp_live/dex/classes.dex → 部署到容器 /vendor/etc/g98-sensors.dex
set -e
cd "$(dirname "$0")/../.."           # 仓库根 = g98 工作目录
SRC="cloudphone/build/sensors/SensorHalJava.java"
rm -rf .tmp_live
mkdir -p .tmp_live/classes .tmp_live/dex
cp "$SRC" .tmp_live/SensorHalJava.java
find .tmp_sensors_build/genjava -name '*.java' > .tmp_live/srcs.txt
echo .tmp_live/SensorHalJava.java >> .tmp_live/srcs.txt
export JAVA_HOME="\$PWD/rtwork/jdk17/jdk-17.0.21+9"
export PATH="\$JAVA_HOME/bin:\$PATH"
javac -encoding UTF-8 -nowarn -source 8 -target 8 -classpath rtwork/android-hidden.jar \
      -d .tmp_live/classes @.tmp_live/srcs.txt
find .tmp_live/classes -name '*.class' > .tmp_live/classes.txt
rtwork/sdk/build-tools/35.0.0/d8.bat --lib rtwork/android-hidden.jar --min-api 33 \
      --output .tmp_live/dex \$(cat .tmp_live/classes.txt)
ls -l .tmp_live/dex/classes.dex
