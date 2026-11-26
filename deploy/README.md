# deploy — 板子部署与镜像构建

## 目录

| 文件 | 用途 |
|---|---|
| `collect_artifacts.sh` | 从运行中的板子（默认 wx1）收集相机栈二进制到 `artifacts/` |
| `Dockerfile` | 以 `g98/redroid-panthor-rt:13-fuxi-v3` 为底，叠加相机栈二进制与 rc |
| `install.sh` | 在板子上安装 host 脚本与 systemd 单元并启用 |
| `systemd/*.service` | 六个板子单元（见下表） |
| `artifacts/` | 构建输入，git-ignored，由 `collect_artifacts.sh` 生成 |

## systemd 单元

| 单元 | 职责 |
|---|---|
| `g98-mediamtx.service` | RTSP/WebRTC 中继（手机 WHIP 上行、camd 可拉 RTSP） |
| `g98-camd.service` | 相机帧路由常驻进程（每实例节点热切换 idle/image/video/rtsp/jpeg） |
| `g98-hub.service` | 聚合：dashboard(:8099/:8443) + ops API(:8098) + webconsole(:8080) |
| `g98-netup.service` | 容器出网配置守护（`g98-netup-all.sh` 循环 + `g98-netup.sh` 单容器） |
| `g98-wx@N.service` | 单实例启动（复用 `/srv/redroid/wxN`，注入 `vendor.camera.redroid.nodes`） |
| `g98-wx-touchd@N.service` | 单实例触摸注入守护 |

## 构建镜像

```bash
./collect_artifacts.sh wx1        # 板子在线、SSH 可用；输出 sha256 与字节数
docker build -t g98/redroid-camera:13-fuxi-v4 .
```

收集内容（7 个文件）：provider 2.4 legacy 可执行文件、`camera.mt6983.so`、`gralloc.g98.so`、
`g98-sensors.dex`、三个 init rc（`g98-sensors.rc`、provider 2.4 / 2.7 rc）。

## 板子安装

```bash
tar czf g98deploy.tgz deploy && scp g98deploy.tgz root@192.168.5.111:/root/
ssh root@192.168.5.111 'cd /root && rm -rf deploy && tar xzf g98deploy.tgz && bash deploy/install.sh'
```

安装后执行回归：

```bash
ssh root@192.168.5.111 'bash /root/cloudphone-tests/run_regression.sh'   # 或仓库 tests/ 同步后的路径
```

## 节点分配

wxN 后置 `/dev/video(20+2(N-1))`、前置 `/dev/video(21+2(N-1))`；由 `wx-start.sh` 在 provider 启动前
`setprop vendor.camera.redroid.nodes "<back>,<front>"`，HAL 构造时读取该属性。
v4l2loopback 需 `MAX_DEVICES>=16` 重编模块，`devices=12 video_nr=20..31 exclusive_caps=1 max_buffers=8`。
