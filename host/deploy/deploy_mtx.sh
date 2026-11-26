#!/bin/bash
set -u
H=192.168.5.111
export SSH_ASKPASS="C:/Users/rosetta/Desktop/g98/_askpass.cmd" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
SSHOPT="-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no"
scp $SSHOPT cloudphone/host/g98-mediamtx.service root@$H:/etc/systemd/system/g98-mediamtx.service || exit 9
ssh $SSHOPT root@$H 'bash -s' <<'EOS'
systemctl daemon-reload
systemctl enable --now g98-mediamtx
sleep 2
systemctl is-active g98-mediamtx
ss -ltnp | grep -E "8554|8889" | head -3
/usr/bin/curl -s -o /dev/null -w "whep=%{http_code}\n" -X POST -H "Content-Type: application/sdp" --data-binary "v=0" http://127.0.0.1:8889/whep/wx1
EOS
