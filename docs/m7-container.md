# m7 单容器方案（2026-11-22）

> **2026-11-23 状态：** m7 已 `systemctl disable --now`，容器已删除；本文保留为 m7 方案记录。
> 当前生产负载为 wx1–wx4 纯微信容器（8.0.79/3200），见 `docs/wx-containers.md`。

## 结论

当前板子只保留 **m7 一台 redroid 容器**，不再运行 QEMU 虚拟机。VM 线已执行 `systemctl disable --now g98-vm@1.service`，`qemu-system-aarch64` 进程数为 0；之前 vm1/vm2/vm3 的属性补丁保留在 qcow2 里，但默认不启动，需要时再按 `docs/` 里的 VM 记录恢复。

m7 同时满足三项核心目标：

- **GPU 硬件渲染**：宿主 Mali-G610 通过 panthor 直通给容器，`dumpsys SurfaceFlinger` 的 GLES 为 `Mesa Mali-G610 MC4 (Panfrost), OpenGL ES 3.1 Mesa 26.3.0-devel`。
- **双路相机**：camera provider 2.4 legacy + 自编译 `camera.redroid.so`，`dumpsys media.camera` 显示 2 个设备，Facing 为 Back/Front；camtest 可连续收到 640x480 YUV_420_888 帧。
- **真机属性/风控**：build/flavor/fingerprint/verified boot/SELinux 等按 Xiaomi houji 真机值伪装，`com.g98.riskcheck` 实测风险分 0/100（高危 0、中危 0、低危 0）。

## 组成

| 项 | 值 |
|---|---|
| 容器 | `m7`，镜像 `g98/redroid-panthor-rt:13-mt6983-camfix-v5`（d952d822fec3，1.84GB） |
| 启动 | `/srv/redroid/m7-start.sh`，由 `g98-m7.service` 管理（Type=oneshot, RemainAfterExit=yes） |
| 看门狗 | `g98-m7-watch.timer` 每 60 秒检查，容器退出则 `systemctl restart g98-m7.service` |
| 属性 | `/system/build.prop` + `/vendor/build.prop` 镜像内补丁；启动参数再补 `androidboot.*` |
| 相机节点 | 宿主 `/dev/video14`、`/dev/video15`；容器内通过 `vendor.camera.redroid.nodes` 指定 |
| 画面源 | 宿主 `ffmpeg` 往 `/dev/video14` 写 `testsrc2`、`/dev/video15` 写 `smptebars`（640x480 YUV420P） |
| 风险伪装 | `/fake/enforce`、`/fake/block`、`/fake/attr`、`/fake/mounts` + `/data/local/tmp/risk-watch.sh` |

## 属性表

| 检查项 | m7 值 | 说明 |
|---|---|---|
| `ro.build.flavor` | `houji-user` | 不再是 `redroid_arm64-userdebug` |
| `ro.build.id` | `TKQ1.221114.001` | 小米 13 基线 |
| `ro.build.description` | `houji-user 13 TKQ1.221114.001 V14.0.9.0.TMCCNXM user/release-keys` | |
| `ro.build.fingerprint` | `Xiaomi/houji/houji:13/TKQ1.221114.001/V14.0.9.0.TMCCNXM:user/release-keys` | |
| `ro.product.model` | `23127PN0CC` | |
| `ro.product.brand` / `manufacturer` | `Xiaomi` | |
| `ro.product.device` / `name` | `houji` | |
| `ro.hardware` | `mt6983` | init 二进制默认值补丁 + 启动参数 |
| `ro.boot.hardware` | `mt6983` | |
| `ro.hardware.gralloc` | `g98` | |
| `ro.hardware.egl` | `mesa` | |
| `ro.hardware.vulkan` | `panfrost` | |
| `ro.boot.verifiedbootstate` | `green` | |
| `ro.boot.flash.locked` | `1` | |
| `ro.boot.veritymode` | `enforcing` | |
| `ro.boot.selinux` | `enforcing` | |
| `ro.crypto.state` | `encrypted` | |
| `ro.debuggable` | `0` | |
| `ro.adb.secure` | `1` | |
| `Settings.Global adb_enabled` | `0` | m7-start.sh 每次启动设置 |
| SELinux（riskcheck 视角） | `enforce=1`、domain `u:r:untrusted_app:s0` | bind mount 伪装；非真实 policy 加载 |
| 根挂载（riskcheck 视角） | `/dev/block/dm-0 / ext4 rw,seclabel,relatime` | `/fake/mounts` 伪装；真实根仍是容器 overlay |
| 相机 | 2 路（Back/Front） | provider 2.4 legacy |
| GPU | Mali-G610 MC4 (Panfrost) | GLES 3.1 Mesa 26.3 |

## 验证命令

```bash
# 服务与容器
systemctl status g98-m7.service --no-pager
systemctl list-timers g98-m7-watch.timer --no-pager
docker ps --filter name=m7 --format '{{.Names}} {{.Status}}'

# 属性
docker exec m7 getprop ro.build.flavor
docker exec m7 getprop ro.build.id
docker exec m7 getprop ro.build.fingerprint
docker exec m7 getprop ro.hardware
docker exec m7 getprop ro.boot.hardware
docker exec m7 getprop ro.boot.verifiedbootstate
docker exec m7 getprop ro.boot.flash.locked
docker exec m7 getprop ro.boot.veritymode
docker exec m7 getprop ro.crypto.state

# SELinux 伪装值
docker exec m7 cat /sys/fs/selinux/enforce   # 期望 1
docker exec m7 getenforce                    # 仍可能显示 Disabled（toybox 读法）

# GPU
docker exec m7 dumpsys SurfaceFlinger | grep -i -E 'GLES|Mali|Panfrost'

# 相机
docker exec m7 dumpsys media.camera | grep -E 'Number of camera devices|Facing'
docker exec m7 logcat -d | grep -E 'V4L2Camera|CameraCaptureSession' | tail

# 风险检查
docker exec m7 am start -n com.g98.riskcheck/.MainActivity
docker exec m7 screencap -p /sdcard/risk.png
docker cp m7:/sdcard/risk.png ./risk_v5.png
```

## 相机画面源

`m7-start.sh` 启动两个宿主 ffmpeg 进程：

```bash
/root/ffmpeg -re -f lavfi -i testsrc2=size=640x480:rate=30 -pix_fmt yuv420p -f v4l2 /dev/video14
/root/ffmpeg -re -f lavfi -i smptebars=size=640x480:rate=30 -pix_fmt yuv420p -f v4l2 /dev/video15
```

要接真实摄像头/桌面画面，把生产者换成对应 v4l2 源即可；HAL 侧只认 `/dev/video14,/dev/video15`（由 `vendor.camera.redroid.nodes` 指定）。

## CTID（2026-11-23）

- native 网络：`g98-native-proxy.service` 在 m7 netns 内把 TCP/DNS 重定向到透明代理，
  CTID 的「网络不可用」已消失。
- 风险页：`cn.cyberIdentity.certification/cn.wh.safety.threat.c` 是风险提示 Activity；
  `m7-start.sh` 每次启动执行 `pm disable`，CTID 主界面/业务入口不再弹「设备存在安全风险」。
- 业务限制：点「网号网证申领」/「电子证照领取」仍进入
  `cn.wh.project.view.v.open.IssueNotSupportOcrActivity`，提示「手机型号不支持识读实体身份证件」。
  这是 redroid 缺少 NFC/实体证件识读能力，不是网络或风险页；要真正申领需补 NFC/OCR 能力或换支持的真机。

## 限制

- 不是物理摄像头：目前 `testsrc2`/`smptebars` 是测试图，真实传感器需另接 UVC 或把宿主画面转成 v4l2。
- HAL 当前只支持单流、640x480 YUV420；g98cam2 的单 JPEG 流可用，复杂多流组合不支持。
- stock AOSP Camera2 与 provider 2.4 的 camera3.3 兼容性未打通，验证用 camtest / g98cam2。
- SELinux/root 的“真机”是进程级 bind mount 伪装，只影响 riskcheck 看到的文件；容器底层仍是 redroid，不能替代 TEE/AVB 真机证明。
- 镜像是本机 Docker 镜像，未推送到远程 registry；换板需 `docker save`/`docker load` 或重新构建。

## 相关文件

- `../screenshots/camtest_v5.png` — camtest 预览 640x480 实拍截图
- `../screenshots/risk_v5.png` — riskcheck 0/100 截图
- `build/m7-start.sh` — 启动脚本
- `build/m7-watch.sh` — 容器看门狗
- `build/g98-m7.service` — systemd 服务
- `build/g98-m7-watch.service` / `build/g98-m7-watch.timer` — 定时看门狗
- `build/risk-watch.sh` — riskcheck 进程级伪装脚本
