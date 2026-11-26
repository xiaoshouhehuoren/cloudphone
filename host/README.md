# host/ — 板子宿主机侧组件

这些文件部署到板子（192.168.5.111）的 /root，由 systemd 常驻。

| 文件 | 部署位置 | systemd 单元 | 作用 |
| --- | --- | --- | --- |
| g98-camd.py | /root/g98-camd.py | g98-camd.service | 常驻帧路由：每个实例一路 v4l2loopback 节点，热切换 idle / 图片 / 视频 / RTSP，切源不重启 provider、不重启相机 App |
| g98-camd-prep.sh | /root/g98-camd-prep.sh | （ExecStartPre） | 保证 v4l2loopback 以 devices=12 video_nr=20..31 加载、节点是字符设备且为 YU12:640x480，并 keepf 住格式 |
| g98-camd-adopt.sh | /root/g98-camd-adopt.sh | （ExecStartPost） | camd 拿到节点后，重启各容器里的 provider / cameraserver，让 HAL 重新 open 节点 |
| g98-camd.service | /etc/systemd/system/ | — | 单元定义（Restart=always，Before=g98-dashboard） |
| g98-mediamtx.service | /etc/systemd/system/ | — | mediamtx 常驻（8554/8889），/m 与桌面 WebRTC 依赖它；停 camstream 后必须靠它拉起 |
| deploy_camd_service.sh / deploy_mtx.sh | 本机 | — | 从 Windows 侧 scp + 安装 + 重启的命令封装 |

## 节点拓扑

| 实例 | 后置（camera 0） | 前置（camera 1） |
| --- | --- | --- |
| wx1 | /dev/video20 | /dev/video21 |
| wx2 | /dev/video22 | /dev/video23 |
| wx3 | /dev/video24 | /dev/video25 |
| wx4 | /dev/video26 | /dev/video27 |
| wx5 | /dev/video28 | /dev/video29 |

规则：base = 20 + 2*(N-1)，前置 = base + 1。容器启动脚本（/srv/redroid/wx-start.sh）里的 configure_camera() 会写入
`setprop vendor.camera.redroid.nodes "<back>,<front>"`，HAL 读这个属性决定打开哪两个节点。

## camd 控制接口

```
GET  http://127.0.0.1:8100/state    # 每节点 kind/arg/frames/written/errors
GET  http://127.0.0.1:8100/health
POST /set  {"node":"/dev/video20","kind":"image","path":"/root/camin/x.png"}
POST /set  {"node":"/dev/video20","kind":"video","path":"/root/camin/x.mp4"}
POST /set  {"node":"/dev/video20","kind":"rtsp","url":"rtsp://..."}
POST /set  {"node":"/dev/video20","kind":"idle"}
```

仰望的约束：idle 时按 G98_CAMD_IDLE_FPS（默认 5）出帧，活跃源按 G98_CAMD_FPS（默认 30）；写线程阻塞式 write()
自动被消费端限速，因此不要改异步写。

## 排障要点

- `/dev/videoNN` 必须 `crw-rw-rw-`，变成普通文件时说明有进程用 O_CREAT 打开过，modprobe 前要先 rm。
- 改节点参数必须 `systemctl stop g98-dashboard g98-camd g98-camidle` 再 pkill ffmpeg 再 rmmod，否则 rmmod 报 in use。
- 相机 App 卡在旧帧时，force-stop 相机 + 重启 provider + 重启 cameraserver 才是干净会话。
