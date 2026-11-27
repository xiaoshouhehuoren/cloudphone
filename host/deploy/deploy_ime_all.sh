#!/bin/bash
# 给运行中的 wx1..wxN 装 ADBKeyboard（控制台打中文用）。
# 用法：bash deploy_ime_all.sh [apk路径]
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APK="${1:-$HERE/../../deploy/apk/ADBKeyboard.apk}"
[ -f "$APK" ] || { echo "apk not found: $APK" >&2; exit 1; }
IME=com.android.adbkeyboard/.AdbIME
ok=0
for c in $(docker ps --format '{{.Names}}' | grep -E '^wx[0-9]+$' | sort); do
  if docker exec "$c" ime list -s 2>/dev/null | grep -q adbkeyboard; then
    docker exec "$c" ime enable "$IME" >/dev/null 2>&1 || true
    echo "$c already installed"
    ok=$((ok+1)); continue
  fi
  docker cp "$APK" "$c:/data/local/tmp/ADBKeyboard.apk" >/dev/null 2>&1
  r=$(docker exec "$c" pm install -r /data/local/tmp/ADBKeyboard.apk 2>&1 | tail -1)
  docker exec "$c" ime enable "$IME" >/dev/null 2>&1 || true
  if docker exec "$c" ime list -s 2>/dev/null | grep -q adbkeyboard; then
    echo "$c install=$r ime=enabled"
    ok=$((ok+1))
  else
    echo "$c install=$r ime=MISSING"
  fi
done
echo "applied=$ok"
