#!/bin/bash
# Install host-side units and helpers on the board (run as root).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 644 "$HERE/systemd/g98-camd.service"     /etc/systemd/system/
install -m 644 "$HERE/systemd/g98-mediamtx.service" /etc/systemd/system/
install -m 644 "$HERE/systemd/g98-dashboard.service" /etc/systemd/system/
install -m 755 "$HERE/../host/g98-camd.py"          /root/g98-camd.py
install -m 755 "$HERE/../host/g98-camd-prep.sh"     /root/g98-camd-prep.sh
install -m 755 "$HERE/../host/g98-camd-adopt.sh"    /root/g98-camd-adopt.sh
systemctl daemon-reload
systemctl enable --now g98-mediamtx.service g98-camd.service g98-dashboard.service
systemctl --no-pager status g98-camd g98-mediamtx g98-dashboard | head -20
