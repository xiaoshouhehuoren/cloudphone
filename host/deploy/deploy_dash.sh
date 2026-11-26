#!/bin/bash
set -u
H=192.168.5.111
export SSH_ASKPASS="C:/Users/rosetta/Desktop/g98/_askpass.cmd" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
SSHOPT="-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no"
scp $SSHOPT console3.board.py root@$H:/root/console3.py || exit 9
ssh $SSHOPT root@$H 'bash -s' <<'EOS'
systemctl restart g98-dashboard
sleep 2
systemctl is-active g98-dashboard
/usr/bin/curl -s -o /dev/null -w "m=%{http_code}\n" -H "Cookie: g98k=g98panel" http://127.0.0.1:8099/m
EOS
