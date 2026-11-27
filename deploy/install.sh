#!/bin/bash
# Install host-side units and helpers on the board (run as root on 192.168.5.111).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# --- units -------------------------------------------------------------
for u in g98-hub.service g98-camd.service g98-mediamtx.service \
         g98-netup.service g98-dashboard.service g98-wx@.service \
         g98-wx-touchd@.service g98-memlimit.service g98-memlimit.timer \
         g98-trim.service g98-trim.timer; do
    [ -f "$HERE/systemd/$u" ] && install -m 644 "$HERE/systemd/$u" /etc/systemd/system/
done

# --- host helpers ------------------------------------------------------
install -m 755 "$HERE/../host/g98-camd.py"       /root/g98-camd.py
install -m 755 "$HERE/../host/g98-camd-prep.sh"  /root/g98-camd-prep.sh
install -m 755 "$HERE/../host/g98-camd-adopt.sh" /root/g98-camd-adopt.sh
install -m 755 "$HERE/../host/console3.board.py" /root/console3.py
install -m 755 "$HERE/../host/g98-netup.sh"     /root/g98-netup.sh
install -m 755 "$HERE/../host/g98-netup-all.sh" /root/g98-netup-all.sh
install -m 755 "$HERE/../host/g98-memlimit.sh"  /usr/local/bin/g98-memlimit.sh
install -m 755 "$HERE/../host/g98-trim.sh"      /usr/local/bin/g98-trim.sh
install -m 644 "$HERE/tmpfiles-g98-video.conf" /etc/tmpfiles.d/g98-video.conf
systemd-tmpfiles --create /etc/tmpfiles.d/g98-video.conf 2>/dev/null || true

systemctl daemon-reload

# --- enable ------------------------------------------------------------
# g98-hub groups dashboard + ops api + web console behind one switch.
systemctl enable --now g98-mediamtx.service g98-camd.service g98-hub.service \
                         g98-netup.service g98-memlimit.timer g98-trim.timer
# 内存软上限先落到已经在跑的容器上（timer 每 5 分钟补一次）
/usr/local/bin/g98-memlimit.sh || true
# Per-instance units: enable the ones that exist as containers.
for n in 1 2 3 4 5 6; do
    [ -d "/srv/redroid/wx$n" ] && systemctl enable "g98-wx@$n.service" "g98-wx-touchd@$n.service" || true
done

systemctl --no-pager status g98-camd g98-mediamtx g98-hub g98-netup | head -30
