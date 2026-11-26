#!/bin/bash
# Deploy the console to the board and restart whichever hub unit exists.
set -u
H=${G98_HOST:-192.168.5.111}
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export SSH_ASKPASS="C:/Users/rosetta/Desktop/g98/_askpass.cmd" SSH_ASKPASS_REQUIRE=force DISPLAY=:0
SSHOPT="-o StrictHostKeyChecking=no -o PreferredAuthentications=password -o PubkeyAuthentication=no"
scp $SSHOPT "$ROOT/host/console3.board.py" root@$H:/root/console3.py || exit 9
ssh $SSHOPT root@$H 'bash -s' <<'EOS'
systemctl is-enabled g98-hub.service >/dev/null 2>&1 && U=g98-hub || U=g98-dashboard
systemctl restart "$U"
sleep 2
systemctl is-active "$U"
/usr/bin/curl -s -o /dev/null -w "m=%{http_code}\n" -H "Cookie: g98k=g98panel" http://127.0.0.1:8099/m
EOS
