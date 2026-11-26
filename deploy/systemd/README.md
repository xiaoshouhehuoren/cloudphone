# G98 systemd units

Six logical units cover the whole host stack:

| unit | role | notes |
| --- | --- | --- |
| `g98-mediamtx.service` | RTSP/WebRTC media server (:8554, :8889, :8888) | Restart=always |
| `g98-camd.service` | camera frame router (:8100), owns `/dev/video20..31` | `G98_CAMD_FPS=30`, `G98_CAMD_IDLE_FPS=30` (lower rates starve HAL → CameraServiceWatchdog) |
| `g98-hub.service` | one switch for dashboard (:8099/:8443), ops API (:8098), web console (:8080) | oneshot aggregate; `Requires=` the three real services |
| `g98-wx@N.service` | starts/stops container `wxN` via `/srv/redroid/wx-start.sh` | oneshot, `RemainAfterExit=yes` |
| `g98-wx-touchd@N.service` | touch input bridge for `wxN` | |
| `g98-netup.service` | re-applies redroid networking after docker events | |

## Usage

```bash
systemctl start g98-hub            # dashboard + api + console
systemctl stop  g98-hub            # stops all three
systemctl restart g98-camd         # restart camera router only
systemctl status 'g98-wx@*'
```

`g98-dashboard.service`, `g98-api.service`, `g98-webconsole.service` stay
installed as the real implementations; `g98-hub` is the single control point.

## Migration note

Older deployments only had `g98-dashboard.service`. `install.sh` installs
`g98-hub` alongside it, so nothing breaks: `g98-hub` `Requires=` the old
unit and starting the hub starts (or reuses) it.
