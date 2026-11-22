# cloudphone — G98 (RK3588) 云手机平台

把一块 BDY G98 开发板（RK3588 / 15GB / Armbian 6.18 / Docker）做成 **m7 + 4 台 Android 13 云手机**，
并给它接上**可用的虚拟摄像头**（Camera2 预览 + 拍照）、微信、以及把宿主机任意画面灌进相机的通路。

> **2026-11-23 更新：** m7 已停用（容器删除、服务 disable），当前生产负载是 **wx1–wx4 四台纯微信容器**，微信已切到 **8.0.79 / versionCode 3200**（RT Probe 支持版本）。m7 方案保留为记录，可随时重新 enable。
>
> **2026-11-22 交付快照：** **m7 单容器**（GPU 硬渲、双路相机、真机属性、riskcheck 0/100）+ **wx1–wx4 四台纯微信容器**（各自独立 `/data`、serial/MAC/Android ID、代理端口）。VM 线已停用。详见 [`docs/m7-container.md`](docs/m7-container.md)、[`docs/wx-containers.md`](docs/wx-containers.md)、[`docs/container-proxy.md`](docs/container-proxy.md)。

## 内容

| 目录 | 说明 |
|---|---|
| `docs/HANDOFF.md` | 交接文档：硬件、容器、相机 HAL 的全部状态与复现步骤 |
| `docs/G98-知识总汇.md` | **知识总汇**：硬件、架构演进（6 容器→VM→m7）、相机/GPU/riskcheck、VM 复盘、全部踩坑索引 |
| `docs/G98-踩坑全记录.md` | 100+ 轮踩坑记录（含本轮相机/推流新增章节） |
| `docs/G98-运维手册.md` | 日常运维：容器、ADB、串流、面板 |
| `docs/rt-wechat-coverage.md` | RT（Hook Runtime）控制微信的能力对照与移植评估 |
| `docs/rt-implementation.md` | RT 在 Android 13 云手机的落地记录：镜像、构建、部署、验证、限制 |
| `rt/` | fake lmkd、Agent enrollment、mock Channel、librt/runtime 构建脚本 |
| `hal/arc/` | 自写 libyuv shim、JPEG 编码器（jo_jpeg）、arc/ 源 |
| `hal/camera3_4/` | 改过的 AOSP camera HAL 3.4 源码（V4L2 wrapper、元数据、多流） |
| `docs/m7-container.md` | **当前方案**：m7 单容器（属性/相机/GPU/riskcheck）说明与验证命令 |
| `docs/container-proxy.md` | **每容器独立代理**：反向 SSH + socat + 每容器端口 |
| `docs/wx-containers.md` | **四台纯微信容器**：wx1–wx4 的身份、代理、服务与验证 |
| `build/wx-start.sh` | 启动单台微信容器、等待 boot、装微信、配代理 |
| `build/g98-wx@.service` | systemd 模板：`g98-wx@1..4` |
| `build/wx-identities.conf` | wx1–wx4 的 serial / MAC / Android ID / 代理端口表 |
| `build/g98-proxy-relay@.service` | 按端口模板把 docker0 暴露到板子 127.0.0.1 |
| `build/container-proxy.sh` | 给容器设默认路由 + Android 全局代理 |
| `build/g98-proxy-relay.service` | 板子 docker0:18081 → 127.0.0.1:18081 relay |
| `tools/g98-proxy.py` | Windows 侧每容器 HTTP/HTTPS 代理 |
| `build/_rehal7.sh` | 用 NDK 交叉编译 `camera.redroid.so`（Linux/Windows MSYS 均可） |
| `build/m7-start.sh` | 启动 m7 容器、相机源、属性与风控伪装 |
| `build/m7-watch.sh` | m7 容器看门狗脚本 |
| `build/cam-rollout.sh` | 把相机栈热插拔到任意容器（m1–m6） |
| `tools/raw2v4l2.py` | 宿主机侧：TCP 原始帧 → `/dev/video10` |
| `tools/_stream.ps1` | Windows 侧：GDI 实时抓屏 → TCP（15 fps） |
| `tools/_cap.ps1` | Windows 侧：截一张桌面图 |

## 快速回顾

- 当前 **wx1–wx4** 四台纯微信容器（8.0.79/3200）；m7 已于 2026-11-23 停用，`m1`–`m6` 和 VM 线均已停用。
- 每容器代理：wx1–wx4 18082–18085；m7 的 18081 配置保留但当前不启动；容器 → 板子 relay → Windows 代理；详见 `docs/container-proxy.md`。
- 相机：m7 双路 Back/Front，宿主 ffmpeg → `/dev/video14/15`。
- 外部画面：宿主写 `/dev/video14`（容器 `/dev/video10`）即可。
- RT：历史 m6 验证记录保留在 `docs/rt-implementation.md`。
- 已知限制：单容器独占 v4l2 节点、代理依赖 Windows 在线、native 直连可能绕过全局代理、4 台微信仍共用同一条公网出口；微信登录需人工。

## 许可

内部工程记录，按现状提供。
