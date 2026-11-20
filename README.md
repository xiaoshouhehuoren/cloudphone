# cloudphone — G98 (RK3588) 云手机平台

把一块 BDY G98 开发板（RK3588 / 15GB / Armbian 6.18 / Docker）做成 **6 台 Android 13 云手机**，
并给它接上**可用的虚拟摄像头**（Camera2 预览 + 拍照）、微信、以及把宿主机任意画面灌进相机的通路。

## 内容

| 目录 | 说明 |
|---|---|
| `docs/HANDOFF.md` | 交接文档：硬件、容器、相机 HAL 的全部状态与复现步骤 |
| `docs/G98-踩坑全记录.md` | 100+ 轮踩坑记录（含本轮相机/推流新增章节） |
| `docs/G98-运维手册.md` | 日常运维：容器、ADB、串流、面板 |
| `docs/rt-wechat-coverage.md` | RT（Hook Runtime）控制微信的能力对照与移植评估 |
| `hal/arc/` | 自写 libyuv shim、JPEG 编码器（jo_jpeg）、arc/ 源 |
| `hal/camera3_4/` | 改过的 AOSP camera HAL 3.4 源码（V4L2 wrapper、元数据、多流） |
| `build/_rehal7.sh` | 用 NDK 交叉编译 `camera.redroid.so`（Linux/Windows MSYS 均可） |
| `build/cam-rollout.sh` | 把相机栈热插拔到任意容器（m1–m6） |
| `tools/raw2v4l2.py` | 宿主机侧：TCP 原始帧 → `/dev/video10` |
| `tools/_stream.ps1` | Windows 侧：GDI 实时抓屏 → TCP（15 fps） |
| `tools/_cap.ps1` | Windows 侧：截一张桌面图 |

## 快速回顾

- 6 台容器 `m1`–`m6`，镜像 `g98/redroid-panthor-rt:13`，ADB 端口 5555–5560（`/root/adb-ports.sh` 恢复）。
- 相机：`g98/redroid-panthor-cam15:13`（含 JPEG 拍照），当前只在 **m6** 启用。
- 外部画面：宿主机往 `/dev/video10` 写 `yuv420p 640x480` 即可，详见 HANDOFF 第二节。
- 已知限制：无 EXIF、录像未验证、单容器独占 `/dev/video10`、实时推流时客户端 buffer 只读导致黑屏（待修 gralloc registerBuffer）。

## 许可

内部工程记录，按现状提供。
