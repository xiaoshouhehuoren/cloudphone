#!/bin/bash
# Install host-side units and helpers on the board (run as root on 192.168.5.111).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

# --- units -------------------------------------------------------------
for u in g98-hub.service g98-camd.service g98-mediamtx.service \
         g98-netup.service g98-dashboard.service g98-wx@.service \
         g98-wx-touchd@.service; do
    [ -f "$HERE/systemd/$u" ] && install -m 644 "$HERE/systemd/$u" /etc/systemd/system/
done

# --- host helpers ------------------------------------------------------
install -m 755 "$HERE/../host/g98-camd.py"       /root/g98-camd.py
install -m 755 "$HERE/../host/g98-camd-prep.sh"  /root/g98-camd-prep.sh
install -m 755 "$HERE/../host/g98-camd-adopt.sh" /root/g98-camd-adopt.sh
install -m 755 "$HERE/../host/console3.board.py" /root/console3.py

systemctl daemon-reload

# --- enable ------------------------------------------------------------
# g98-hub groups dashboard + ops api + web console behind one switch.
systemctl enable --now g98-mediamtx.service g98-camd.service g98-hub.service \
                         g98-netup.service
# Per-instance units: enable the ones that exist as containers.
for n in 1 2 3 4 5 6; do
    [ -d "/srv/redroid/wx$n" ] && systemctl enable "g98-wx@$n.service" "g98-wx-touchd@$n.service" || true
done

systemctl --no-pager status g98-camd g98-mediamtx g98-hub g98-netup | head -30
