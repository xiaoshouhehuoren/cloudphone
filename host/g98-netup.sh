#!/bin/bash
# g98-netup.sh <container>  -- redroid 容器走板子真实外网（eth3 -> 192.168.6.1）
# 不再依赖 PC 的 SOCKS5/tproxy；DNS 仍是容器内本地中继（netd 固定 8.8.8.8 -> lo 拦截）
set -u
C=${1:-wx1}
PID=$(docker inspect "$C" --format '{{.State.Pid}}') || exit 1
[ -z "$PID" ] && { echo "no pid for $C"; exit 1; }
NS=$(readlink "/proc/$PID/ns/net")

# 1) 清掉旧的 PC 代理链路（同一 netns 内的 tproxy / netns_dns 进程）
for p in $(pgrep -f 'netns_dns|tproxy.py' || true); do
  if [ "$(readlink "/proc/$p/ns/net" 2>/dev/null)" = "$NS" ]; then
    kill -9 "$p" 2>/dev/null
  fi
done

# 2) 删除 80/443/8080 的 REDIRECT
for port in 80 443 8080; do
  while nsenter -t "$PID" -n iptables -t nat -D OUTPUT -p tcp --dport $port -j REDIRECT --to-ports 13080 2>/dev/null; do :; done
done

# 3) netd 仍把 DNS 指到 8.8.8.8，用 lo 地址拦截后由本地中继转发到公网 DNS
nsenter -t "$PID" -n ip addr add 8.8.8.8/32 dev lo 2>/dev/null || true
if ! nsenter -t "$PID" -n ss -lun 2>/dev/null | grep -q ':53 '; then
  nohup nsenter -t "$PID" -n python3 /root/netns_dns.py 223.5.5.5 53 53 >> "/var/log/dns-$C.log" 2>&1 &
fi
sleep 1
echo "$C direct pid=$PID"
nsenter -t "$PID" -n iptables -t nat -S OUTPUT | grep -c REDIRECT || true
nsenter -t "$PID" -n ss -lun 2>/dev/null | grep ':53 ' || echo "dns relay not listening"
