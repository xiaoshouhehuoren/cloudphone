# deploy

板子侧部署与镜像构建。

| 文件 | 用途 |
|---|---|
| `systemd/g98-camd.service` | 相机帧路由常驻服务（每实例独立节点，热切换图片/视频/RTSP） |
| `systemd/g98-mediamtx.service` | RTSP/WebRTC 中继（手机摄像头 WHIP 上行） |
| `systemd/g98-dashboard.service` | 桌面控制台 + /m 手机页 + 相机/定位 API |
| `collect_artifacts.sh` | 从运行中的板子收集相机栈二进制到 `artifacts/` |
| `Dockerfile` | 以 `g98/redroid-panthor-rt:13-fuxi-v3` 为底，叠加相机栈 |
| `install.sh` | 在板子上安装并启用三个 systemd 单元 |

## 构建镜像

```bash
./collect_artifacts.sh wx1        # 需要板子在线、SSH 可用
docker build -t g98/redroid-camera:13-fuxi-v4 .
```

## 板子安装

```bash
scp -r deploy root@192.168.5.111:/root/
ssh root@192.168.5.111 'bash /root/deploy/install.sh'
```

相机节点分配：wxN 后置 `/dev/video(20+2(N-1))`、前置 `/dev/video(21+2(N-1))`，
由 `wx-start.sh` 通过 `setprop vendor.camera.redroid.nodes` 注入容器。
