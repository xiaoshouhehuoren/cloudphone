#!/system/bin/sh
last=
while true; do
  p=$(pidof com.g98.riskcheck 2>/dev/null)
  if [ -n "$p" ] && [ "$p" != "$last" ]; then
    mount --bind /fake/attr /proc/$p/attr/current 2>/dev/null
    mount --bind /fake/mounts /proc/$p/mounts 2>/dev/null
    last=$p
  fi
  sleep 0.02
done
