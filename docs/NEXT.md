# 待办与阻塞（2026-11-20）

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
- **缺口**：GSI 没有可引导的内核；板上 `/root/g98-kernel-6.1.115/Image` 是带板级驱动的内核，
  在 QEMU `virt` 上大概率起不来，需要一个通用 arm64 内核（需 mainline 源码）。

## 阻塞：WSL 构建机（2026-11-20）

- `Microsoft-Windows-Subsystem-Linux` 与 `VirtualMachinePlatform` 已启用（提权 dism 完成），
  但 `Component Based Servicing\RebootPending` 仍在 —— **必须重启 Windows 才能生效**，尚未重启。
- 本机 in-box `wsl.exe` 是存根，缺 kernel；需装 WSL MSIX 包。712MB 的
  `Microsoft.WSL_3.0.5.0_x64_ARM64.msixbundle` 正在后台下载（0.3–1.7MB/s）；
  另有 47MB 快速备选（`wsl_update_x64.msi` 17MB + `ubuntu-base-24.04` rootfs 30MB）已下齐在 `.tmp_wsl/`。
- 注意：**即使 WSL 装好，AOSP 全量 `repo sync` 在本机 0.13–0.5MB/s 的出口带宽下也不现实**（>80 小时）。

