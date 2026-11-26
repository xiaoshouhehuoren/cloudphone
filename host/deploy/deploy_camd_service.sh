#!/bin/bash
set -u
H=192.168.5.111
export SSH_ASKPASS="C:/Users/rosetta/Desktop/g98/_askpass.cmd" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
SSHOPT="-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no"
for f in g98-camd.py g98-camd-prep.sh g98-camd-adopt.sh g98-camd.service; do
  scp $SSHOPT cloudphone/host/$f root@$H:/root/$f || exit 9
done
scp $SSHOPT console3.board.py root@$H:/root/console3.py || exit 9
ssh $SSHOPT root@$H 'bash -s' <<'EOS'
set -u
chmod +x /root/g98-camd-prep.sh /root/g98-camd-adopt.sh
pkill -f g98-camd.py 2>/dev/null
sleep 0.5
cp /root/g98-camd.service /etc/systemd/system/g98-camd.service
systemctl daemon-reload
systemctl enable g98-camd >/dev/null 2>&1
systemctl restart g98-camd
sleep 8
echo "--- camd unit"; systemctl is-active g98-camd
echo "--- camd state"; /usr/bin/curl -s http://127.0.0.1:8100/state
echo ""
echo "--- camidle"; systemctl is-active g98-camidle || true
systemctl restart g98-dashboard
sleep 3
echo "--- dashboard"; systemctl is-active g98-dashboard
/usr/bin/docker exec wx1 dumpsys media.camera 2>/dev/null | grep -E "Number of camera devices" | head -1
/usr/bin/docker exec wx1 pidof android.hardware.camera.provider@2.4-service
EOS
