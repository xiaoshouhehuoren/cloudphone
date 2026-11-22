# 待办与阻塞（2026-11-21）

## 已完成
- 6 台云手机（m1–m6）+ 相机 HAL（预览 / 拍照）+ 外部画面注入（静态图已验证）。
- 踩坑记录补到 `G98-踩坑全记录.md` 第九章（Camera2/JPEG/推流/adb/内存）。
- 本地 git 仓库已建：`cloudphone`（2 个 commit，115 个文件，~900KB）。
- 微信控制方案文档 `docs/wechat-control.md` + 脚本 `tools/wx.sh`。

## 已完成 1：网络身份认证 App（2026-11-20）
- 已从应用宝下载官方 APK（`cn.cyberIdentity.certification` 1.2.41，68.7MB，MD5 `463F0578DC475E68F7D0A46568FE75F0`）。
- 已装进 m6：`pm install -r` → `Success`；启动 `cn.wh.project.view.v.SplashActivity` → `MainTabActivity`，首页正常渲染。
- 容器默认无外网（板子无默认路由），用 `tools/win_http_proxy.py` + `settings put global http_proxy 192.168.99.1:8899` 让 App 通过 Windows 侧 CONNECT 代理联网，已过网络检查。
- 仍会弹「设备存在安全风险」：redroid 无 TEE、系统被 root，属这类 App 的完整性检测；装/启/联网都已验证，办证需真机安全环境。
- 细节（下载直链、安装、启动、代理、限制）：`docs/identity-app.md`。

## 已完成 2：推送到 GitHub（2026-11-20）
- 仓库已建并推送：<https://github.com/xiaoshouhehuoren/cloudphone>（master，`dcbf1ea` 起）。
- 通道：从 Windows 凭据管理器取 gh CLI 的 token（`LegacyGeneric:target=gh:github.com:rosebitfa`，对组织是 admin），
  python + dulwich（`vendor/`）直推，绕开 git 的 schannel 出网失败。复现：`tools/push_github.py`（`--verify` 先验证身份）。
- 之后每次 `git commit` 后重跑一次推送脚本即可。

## 已完成 3：微信控制通道（2026-11-20）
- 找到可用通道：**adb 连容器 bridge IP**（`172.17.0.x:5560`），`uiautomator dump` / `screencap` / `input` 全部可用。
- 工具：`tools/wechat_adb.sh`（start/focus/ui/tap/tap-text/text/shot）+ `tools/ui_find.py`（按文字找控件坐标）。
- 实测：启动页 → 点 Log In → 手机号页 → 输入号码成功（截图 `_wx_demo.png`）。
- 剩余：登录需手机号+短信验证码（真人参与），登录后才能写「进聊天发消息」。

## 已完成 4：RT 控制微信能力评估（2026-11-20）
- 对照 `xiaoshouhehuoren-rom` / `-app` / `workphone-channel` 源码，逐项评估 12 项能力：✅ 4 / ⚠️ 4 / ❌ 4。
- 结论：底座（root、系统镜像、Binder、预装 App）覆盖；RT 本体（`libsalespartner_rt.so` + Probe 注入 + framework 补丁）需 Android 13 移植，约 1–2 周。
- 详细矩阵与实施阶段：`docs/rt-wechat-coverage.md`。

## 已完成 5：RT 控制微信落地（2026-11-20）
- m6 上已跑通：framework.jar 的 `ActivityThread.handleBindApplication` 注入 `SalesPartnerHookRuntime.maybeBoot`，微信进程加载 `libsalespartner_rt.so`，Probe `probe_state=ready`。
- Probe→Agent Binder `authenticated`；Agent 1.15.1 / Probe 1.11.3，UI 显示 **在线 / 服务器已连接**；mock Channel 心跳正常。
- 固化镜像：`g98/redroid-panthor-rt-rt1:13`（sha256 `627637390ce3...`，1.81GB）；Camera2 设备数仍为 2。
- 关键兼容层：fake lmkd（否则 redroid 的 system_server 在 `LmkdConnection.waitForConnection` 持锁卡死所有 App 启动）。
- 复现/构建/已知限制：`docs/rt-implementation.md`；验证截图：`screenshots/rt-agent-online.png`。

## 已完成 6：locked image 0.1（2026-11-20）

- 基于 rt1 做成 `g98/redroid-panthor-locked:0.1`（sha256 `7e1d5639...`）：关 adbd、删 su、`ro.build.type=user` / `release-keys` / `ro.debuggable=0`。
- locktest2 验证：`sys.boot_completed=1`、`init.svc.adbd` 为空、无 adbd 进程、`/system/xbin/su` 不存在。
- 仍未完成：容器级 SELinux enforcing（locked 0.1 的 `getenforce=Disabled`）。原因与后续路线见 `docs/locked-image.md`。

## 已完成 7：SELinux policy 层验证（2026-11-20）

- 测试板上把 Android 13 `precompiled_sepolicy` 真正写入 `/sys/fs/selinux/load`：加载成功，宿主进程 label 从 `kernel` 变 `u:r:kernel:s0`。
- 用容器内 `secilc` 重编 policy，加 `(typepermissive kernel)`，`enforce=1` 后 SSH 与 m1–m6 均存活，`permissive=0` 计数为 0。
- 结论：策略层可行；但容器服务仍全在 `kernel` permissive 域，`getenforce` 仍 Disabled，因为 redroid 的 libselinux/init hook 没撤。产物在 `/srv/redroid/selinux/`，实验后已 reboot 清理。

## 已完成 8：组件级替换被证伪（2026-11-20）

试了“不重编、直接用 AOSP13 GSI 的 stock 二进制换掉 redroid 被 hook 的组件”这条省力路，三种组合全部失败：

| 组合 | 结果 |
| --- | --- |
| 只换 `/system/lib64/libselinux.so`（GSI stock） | 触发 `apexd` `reboot_on_failure`，容器 Exited 129，且因 privileged + CAP_SYS_BOOT **把 G98 宿主重启** |
| 同上 + 覆盖 `apexd.rc` 去掉重启 | 不再重启，但卡在 early stage：`ro.boot.*` 全空、`/sys/fs/selinux/class` 计数 0，policy 未加载 |
| 整块 GSI13 `/system` + stock `init`（先 detach /dev /proc /sys） | `InitFatalReboot: signal 6`：`Failed to mount required partitions early`，stock init 要求 fstab/super |

对照证据：redroid 的 `selinux_android_load_policy` / `security_getenforce` 是 8 字节 `ret` stub（GSI 是 0xbc / 0xfc 字节真实实现）；redroid `libbinder.so` 有 `HACKED` 字符串；redroid `init` 被改成不走 first-stage mount。详见 `docs/locked-image.md` 4.6。

## 阻塞：容器级 SELinux enforcing（2026-11-20）

- redroid 在源码里故意关闭 SELinux（维护者确认：`external/selinux`、`system/core`、`frameworks/native` 的 libselinux hook）；容器参数、bind `/proc/cmdline`、改 init、换 stock 二进制都不能打开。
- 策略层已经验证可加载/可重编，但要让 Android 服务进入各自 domain，必须真重编 libselinux + init + binder。
- **这块 G98 上编不了**：aarch64 宿主，AOSP 官方不支持（`Building Android requires a x86-64 machine`）；`.repo` 只有 6.7M，源码从未 sync；板子无 default route，DNS 全失败；无 clang/ninja。
- 下一步只能是另备 x86_64 Linux 构建机（400G+ 磁盘、AOSP 13 + redroid manifest）重编，或改走 VM/独立内核。
- 宿主 Armbian 与容器共享内核 policy；测试可用 `kernel` permissive 保住宿主，但这不是生产隔离方案。

## 已完成 9：SELinux 关闭的真正位置（源码级，2026-11-20）

- **内核不是阻塞点**：把 617,235 B 的 Android policy 真写进 `/sys/fs/selinux/load` 成功（rc=0），
  `class` 从 0 变 103，`property_service`/`service_manager`/`hwservice_manager`/`keystore_key` 全部出现，
  dmesg 无 "Unknown class"。之前"内核缺 classmap"的判断（数 strings）是错的。
- **真正的原因在 init**：redroid 的 `system/core` 提交 `e7385bfc9d`"fix booting" 把 first stage 的
  `execv("/system/bin/init", {"selinux_setup"})` 改成 `{"second_stage"}` —— 专门加载 policy 的
  `selinux_setup` 阶段被整段跳过，所以 `/sys/fs/selinux/policy` 恒为 0。同一个 commit 还在
  `property_service.cpp` 的 `CheckMacPerms()`、`service.cpp` 的 `ComputeContextFromExecutable()`
  里插了 `se_hack1`。
- **libselinux 的 hook 很小**：`085e5a0720`"ignore selinux" 只改了 14 个函数，靠宏
  `#define se_hack()  if (1) return` / `se_hack1(p) if (1) return p` 实现。
- **架构结论（关键）**：`/sys/fs/selinux` 在宿主和 m6 里是同一个 dev/inode（dev 23 ino 1），
  selinuxfs 无 namespace，**一个内核只有一份 policy**。红旭容器模型下，Armbian 宿主 +
  m1–m6 共享一份 policy，**不可能做到每实例独立 SELinux 强制**。
  => 路线 B（重编 AOSP 撤补丁）即使编成功也拿不到"每实例真强制"，**不再作为可行方案推进**。
- 详见 `docs/locked-image.md` 4.7 / 4.8。

## 已具备：VM 路线的硬件条件（2026-11-20）

- `/dev/kvm` 可用：自写 C 探针实测 `KVM_GET_API_VERSION=12`、`KVM_CREATE_VM`、`KVM_CREATE_VCPU` 全成功；GICv3 就绪。
- 板子经 Windows 反向代理（`tmp_g98_proxy_tunnel.py`，本机 8899 → 板子 127.0.0.1:8899）恢复公网，
  `apt-get install qemu-system-arm` 成功，`/usr/bin/qemu-system-aarch64` 就位（1:10.0.13+ds-0+deb13u1，RC=0）。
- 资源：8 核 / 15.9GB（可用约 9.9GB）/ 剩 402G；guest 系统可用 `/root/gsi13/system.img` + `vbmeta.img`。
- **缺口已补上**：见下面「已完成 10」——板载内核能在 QEMU `virt` 上跑，而且带 binder。

## WSL 构建机：已可用（2026-11-20 晚）

- 用户手动装好 WSL（MSIX 包必须管理员权限，脚本提权三次被 UAC 取消，最后是用户点的）。
  实测 `wsl --version`：WSL 6.18.58.1-1、WSLg 1.0.79、MSRDC 1.2.7391、Direct3D 1.611.1。
- Ubuntu 用 import 方式落地（不碰商店、不下载大包）：
  `wsl --import Ubuntu C:/Users/rosetta/Desktop/g98/.wsl/Ubuntu <rootfs.tar.gz> --version 2`。
  **坑**：PowerShell 单引号字符串里写反斜杠会变成双反斜杠，导致 `Wsl/ERROR_PATH_NOT_FOUND`，用正斜杠。
- rootfs 没有 ca-certificates，https 源握手失败；源改成 http://mirrors.tuna.tsinghua.edu.cn 后
  `apt` 跑到 **5.4MB/s**（Windows 宿主出口只有 0.13–0.5MB/s，WSL 走的是另一条路径）。
- `repo init -u https://mirrors.tuna.tsinghua.edu.cn/git/AOSP/platform/manifest.git -b android-13.0.0_r74 --depth=1`
  成功；已 sync 到 `system/core`、`external/selinux`、`external/libdrm`、`external/minigbm`、`external/drm_hwcomposer`，
  全量 sync 在后台跑（`/opt/aosp`）。
- **`platform/*` 前缀只是 repo 里的路径**：实际 checkout 是 `/opt/aosp/system/core`、`/opt/aosp/external/selinux`。
- ~~关键待办：编干净的 `init`（撤掉 `selinux_setup`→`second_stage` 与 `se_hack`）~~
  —— **已解决**，见下面「已完成 12」。


## 已完成 10：板子上 VM 路线打通（KVM + 板载内核 + binder，2026-11-20）

- **qemu 全套就位**：`qemu-system-arm`（1:10.0.13+ds-0+deb13u1）、`qemu-efi-aarch64`（AAVMF）、
  `ipxe-qemu`（提供 `efi-virtio.rom`）。`apt-get download linux-image-arm64` 只拿到 1444 B 元包，
  真包名是 `linux-image-6.12.117+deb13-cloud-arm64-unsigned`（28,394,444 B，7MB/s）。
- **Debian 12 arm64 guest 起得来**：`debian-12-genericcloud-arm64.qcow2`（345,047,040 B）+ AAVMF UEFI，
  0.68s `Freeing unused kernel memory`，1.42s `systemd[1]`。
- **决定性结果：板载内核当 guest 内核可用，而且 binder 能挂**。
  `-kernel /boot/vmlinuz-6.18.48-ophub -initrd /boot/initrd.img-6.18.48-ophub`，
  guest 里 `uname -r` = 6.18.48-ophub，`mount -t binder none /dev/binderfs` 成功，
  出现 `binder / binder-control / features / hwbinder / vndbinder`。
- **两个坑**：`-drive if=virtio` 需要 `efi-virtio.rom`（装 ipxe-qemu）；
  别用 `-kernel` 直接启 Debian cloud 内核（virtio-blk 是模块，必 `VFS: Unable to mount root fs`）。
  binderfs 的 fs 类型名是 `binder` 不是 `binderfs`。
- **内核差异（关键）**：板载 6.18.48-ophub 有 `CONFIG_ANDROID_BINDER_IPC=y` / `BINDERFS=y` /
  `BINDER_DEVICES="binder,hwbinder,vndbinder"`；Debian 官方 6.12 cloud 内核是
  `# CONFIG_ANDROID_BINDER_IPC is not set` —— **Debian 内核跑不了 Android，板载内核可以**。
- **推论**：每实例独立内核 → 独立 selinuxfs → 独立 policy，4.8 的架构约束被绕开。VM 路线成立。
- **下一步硬活**：把 `g98/redroid-panthor-rt-rt1:13` 的 rootfs 导出成 ext4 磁盘，在 VM 里
  exec `/system/bin/init`，让 Android 以真 SELinux 起来。这是 VM 路线能不能落地的分水岭。
- 详见 `docs/locked-image.md` 4.9。

## 已完成 11：locked 0.2 / 0.3 交付（2026-11-20）

- **0.2**：以运行中的 m6 为基线 `docker commit`（0.2-base = b887d2a8bcc2），再删 su、
  注释 `service adbd` 与所有 `start adbd`、build.prop 改 user/release-keys/debuggable=0，
  commit 成 `g98/redroid-panthor-locked:0.2` = e9006c8b0f03（1.81GB）。
  验证：55s `boot_completed=1`、无 adbd、无 su、RT agent 起来；相机 0 个设备。
- **0.3**：修掉两个静默打掉相机的坑 —— ① 内核升级后 `/lib/modules/6.18.48-ophub` 缺
  `v4l2loopback.ko`（从 `/root/v4l2loopback-main/` 拷进 `extra/` + `depmod -a`，
  `vermagic: 6.18.48-ophub` 正好匹配）；② 容器自己的 /dev tmpfs 里 ueventd 把
  `/dev/video10/11` 建成 `0600 root:root`，camera provider 以 cameraserver 跑 →
  `deviceAdded open v4l2 device /dev/video10 failed:Permission denied`。在
  `/system/etc/ueventd.rc` 固化 0666 规则后 commit 成 `g98/redroid-panthor-locked:0.3` = 4e12f9046ee5。
  验证：55s 开机、无 root/adb、`/dev/video10/11` = 666、**camera devices = 2**、RT agent 运行。
- **口径**：这是 hardened container，不是 locked phone；SELinux 仍 Disabled，无 TEE。
- 详见 `docs/locked-image.md` 第 9 节。
- **注**：0.3 的“SELinux 仍 Disabled”已被下面「已完成 12」改写 —— 容器里已有真实 SELinux domain，
  但强制力仍是 permissive。

## 已完成 12：统一 SELinux 打通（容器真域分离）+ locked 0.9（2026-11-21）

- **根因是两处源码**：`external/selinux` 提交 `085e5a0720` 用 `se_hack` 宏把 14 个函数变成空操作；
  `system/core` 提交 `e7385bfc9d` 把 `first_stage_init.cpp` 里的 `{path,"selinux_setup"}` 换成
  `{path,"second_stage"}`，**AOSP 的整套 policy 加载被整段跳过**；另外 `property_service.cpp` /
  `service.cpp` 各插了一处 `se_hack1`，这是“进程进不了自己 domain”的直接原因。
- **init 四轮才打对**（WSL `m -j16 init` 增量 16 秒）：init → init2（已加载则跳过）→
  **init3（无条件 `set_selinuxmnt("/sys/fs/selinux")` 再递归 restorecon）**。
  关键坑：`is_selinux_enabled()` 的 ANDROID 分支只看 `selinux_mnt` 是否非空，而
  `selinux_android_restorecon_common()` 首句是 `if (is_selinux_enabled() <= 0) return 0;` ——
  宿主已预加载 policy 时走 else 分支、不调 `set_selinuxmnt`，**restorecon 一个标签都没改却返回 0**。
- **policy 要补 HAL transition**：先编出的 policy 与镜像自带 `precompiled_sepolicy`
  字节一致（646,345 B / md5 `f09343b5…`），但跑起来 79 条 “no domain transition”——
  redroid 从没用过 SELinux，vendor HAL 的 `(typetransition init <hal>_exec process <hal>)`
  规则根本不存在。补两轮后 `policy-full3.bin` = **646,633 B / md5
  `81f9da4aee2374226b962581de743300`**（11 个 HAL + `vendor_file → vendor_init`）。
  `secilc` 只在镜像里有（`/system/bin/secilc`），**必须 docker cp 进容器再编**。
- **开机自动加载**：`/etc/g98/policy-full3.bin` + `/usr/local/sbin/g98-load-selinux` +
  `/etc/systemd/system/g98-selinux-policy.service`（`Before=docker.service`，已 enable）。
- **locked 0.9** = `70c3a0d71ab7`（3.54GB）= 0.8 + 补回 `boot-framework.vdex`(514,612 B) +
  build.prop 写 `ro.hardware.egl=mesa` + `/system/etc/init/zz-g98-nodes.rc`
  （`/dev/video10–15`、`/dev/dri/card0`、`/dev/dri/renderD128` 各 `chmod 0666`）。
- **l11 验证**：`boot_completed=1`（约 4 分钟，其中 59s 是 relabel）、user/release-keys、
  `ro.debuggable=0`、无 adbd、无 su、104 个包、**相机 2 路**、RT agent 在线（u0_a58）；
  **init / system_server / surfaceflinger / zygote64 / netd / vold / cameraserver 全部真实 domain**；
  avc denied 里 `permissive=0` 的条数为 0。
- **三个坑**：`/dev/dri/*` 660 → SurfaceFlinger `no suitable EGLConfig found` abort；
  `ro.hardware.egl` 为空 → libEGL 选 ANGLE 然后失败；m6 的 `docker diff` 删过
  `/system/framework/boot-framework.vdex`。ueventd 规则对继承来的 /dev 节点无效，必须 init rc chmod。
- **口径**：**有 SELinux 域、没有 SELinux 强制**。内核侧是 `(typepermissive kernel)`，
  `getenforce` 仍是 Permissive，所有 avc 只记不拦；不能对外说“enforcing”。
  真 enforcing 需要给 Armbian 宿主每个进程也定义 domain，是独立的一大块工作。
- 详见 `docs/locked-image.md` 第 10 节。

## 已完成：m7 单容器方案（2026-11-22）

- VM 线全线停用：`systemctl disable --now g98-vm@1.service`，`qemu-system-aarch64` 进程 0；后续默认只走容器。
- m7 镜像 `g98/redroid-panthor-rt:13-mt6983-camfix-v5`（d952d822fec3，1.84GB）：`ro.hardware=mt6983`、`houji-user` 属性、GPU Panfrost、provider 2.4 + `camera.redroid.so` 双路 Back/Front、riskcheck 0/100。
- 启动：`/srv/redroid/m7-start.sh` + `g98-m7.service`（oneshot），看门狗 `g98-m7-watch.timer`（60s）；相机源为宿主 ffmpeg 写 `/dev/video14/15`（640x480 YUV420P）。
- 局限：SELinux/root 为进程级 bind mount 伪装，不等于真机 TEE/AVB；stock AOSP Camera2 未打通；HAL 仅单流 640x480。
- 详见 `docs/m7-container.md`。

## 已完成：知识总汇（2026-11-22）

- 新增 `docs/G98-知识总汇.md`：按硬件基线、运行形态演进、m7 启动/镜像、GPU、相机 HAL、riskcheck、VM 复盘、运维命令等 13 节汇总全部项目知识。
- `docs/G98-踩坑全记录.md` 增加第十章「2026-11-21/22：VM 路线与 m7 单容器」；`docs/G98-运维手册.md` 与 `docs/HANDOFF.md` 已加当前状态提示。

## 已完成：每容器独立代理（2026-11-22）

- m7 出口链路：App → `172.17.0.1:18081` → 板子 `g98-proxy-relay.service`（socat）→ 板子 `127.0.0.1:18081` → 反向 SSH → Windows `192.168.99.1:18081` → 外网。
- Windows 侧 `tools/g98-proxy.py` 每容器一个端口（m7=18081）；控制 API `127.0.0.1:18080`；日志 `tools/g98-proxy.log`。Docker NAT 会 SNAT 成板子地址，**容器身份必须用端口区分**。
- 板子侧 `/srv/redroid/container-proxy.sh` 自动设置默认路由和 Android 全局代理；`m7-start.sh` 调用它，`m7-watch.sh` 每 60 秒自愈。
- 实测：m7 重启后自动恢复；浦发 8.2.7 进入 `MainActivity` 并加载首页，登录页可到（截图 `screenshots/spdb-relay.png`）。
- 限制：Windows 防火墙挡板子入站，必须靠反向 SSH；当前机器非管理员，Startup 注册需手工运行 `tools/g98-proxy-register.ps1`，否则 Windows 重启/会话结束后代理和隧道会失效。
- 详见 `docs/container-proxy.md`。
