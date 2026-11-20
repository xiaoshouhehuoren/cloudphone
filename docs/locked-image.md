# G98 locked image（0.1）：已完成的部分与 SELinux 边界

> 结论先行：**“无 root、无 adb、user/release-keys”已经在 g98/redroid-panthor-locked:0.1 上验证通过；但真正的 SELinux enforcing 在 redroid 容器里没有做成，也不是改几个属性/init 文件能解决的。** 本文记录已做的改动、验证结果、SELinux 失败原因，以及要继续走的两条路。

## 1. 目标与基线

目标（按重要性）：

1. RT 进 system：framework.jar、libsalespartner_rt.so、Probe/Agent 预装，启动即生效。
2. 用户侧无 root：没有 su，不能 adb root，ro.debuggable=0。
3. 用户侧无 adb：adbd 不启动，不暴露 5555。
4. ro.build.type=user、ro.build.tags=release-keys。
5. SELinux enforcing。

基线镜像：g98/redroid-panthor-rt-rt1:13
（sha256 627637390ce3c1cdac06883ae8edf5d1c9d12bd47581fcb50dccdfdadbccb419，1.81GB）

产物镜像：g98/redroid-panthor-locked:0.1
（sha256 7e1d56396c9cfad7792304671601d98d4019ee28a17b38771ad7fa3eced2ee63）

## 2. 阶段 1：locked 0.1 已做的改动

### 2.1 关闭 adbd

redroid 的 adbd 不是由 ro.debuggable 单独控制的，镜像里的
/system/etc/init/hw/init.usb.rc 定义了：

~~~
service adbd /system/bin/adbd --root_seclabel=u:r:su:s0
    class core
    socket adbd seqpacket 660 system system
    disabled
    seclabel u:r:adbd:s0
~~~

并且多处 USB 配置会 start adbd。只改 ro.debuggable=0 / ro.adb.secure=1
时，adbd 仍会以 shell 用户启动（已实测）。

locked 0.1 的做法：

- 注释掉 service adbd ... 整段（约 15–20 行）；
- 注释掉 init.usb.rc 里所有 start adbd（约 32、46、65、84、103 行）；
- 保留 USB 其它配置，不影响 MTP/网络。

改动后 init.svc.adbd 为空，ps -A 里没有 adbd。

### 2.2 移除 root

- 删除 /system/xbin/su（原镜像里是 setuid root 的 11192B 文件）。
- 不安装 Magisk/Zygisk；不使用 adb root。

### 2.3 改成 user/release-keys

/system/build.prop 写入/确认：

~~~
ro.build.type=user
ro.build.tags=release-keys
ro.debuggable=0
ro.adb.secure=1
ro.secure=1
~~~

注意：容器启动参数里也可能带 ro.build.type=user 等覆盖；镜像内和 cmdline 要一致，
否则 getprop 的结果会随启动方式变化。

### 2.4 固化

~~~
# 在改好的容器上
docker commit --change 'CMD ["qemu=1","androidboot.hardware=redroid","ro.hardware.gralloc=g98","ro.hw_timeout_multiplier=10"]' <container> g98/redroid-panthor-locked:0.1
~~~

实际 commit 时还保留了产品属性（Xiaomi/houji 指纹、serial 等）。
这些属性属于设备伪装层，不是 locked 本身，本文不展开。

## 3. 验证结果（locktest2）

| 检查项 | 命令 | 结果 |
|---|---|---|
| 构建类型 | getprop ro.build.type | user |
| 签名标记 | getprop ro.build.tags | release-keys |
| 可调试 | getprop ro.debuggable | 0 |
| adb 鉴权 | getprop ro.adb.secure | 1 |
| adbd 服务 | getprop init.svc.adbd | 空 |
| adbd 进程 | ps -A 里查 adbd | 无 |
| su | ls /system/xbin/su | 不存在 |
| 启动完成 | getprop sys.boot_completed | 1 |
| SELinux | getenforce | **Disabled**（未完成） |

测试容器用 --network none --memory=3g、独立 /data，不影响 m1–m6。

最小测试里 2.7 external camera service 会因为 VINTF 缺失崩溃，这是测试容器没带
相机 manifest 导致的，不是 locked 改动的问题；正式容器按运维手册带相机配置即可。

## 4. 阶段 2：SELinux enforcing 为什么没做成

### 4.1 先排除的几条路

1. 只加容器启动参数
   androidboot.selinux=enforcing 能让 getprop ro.boot.selinux 变成 enforcing，
   但 getenforce 仍是 Disabled，/sys/fs/selinux/policy 大小为 0。
2. 普通 Docker bind 覆盖 /proc/cmdline
   Docker 26.1.5 直接拒绝：
   check proc-safety of /proc/cmdline mount ... cannot be mounted because it is inside /proc。
3. 静态 wrapper 先 bind 再 exec /init
   wrapper 能启动，但 init 会重新挂载 /proc，fake cmdline 被覆盖，仍然 permissive。
4. 把 /system/bin/init 里的 /proc/cmdline 改成 /fake_cmdline
   已用 docker cp -L 取出真实 init（2,085,760B，ELF aarch64 PIE，
   interpreter=/system/bin/bootstrap/linker64），替换第一处字符串后 bind 回容器。
   /fake_cmdline 确实包含 androidboot.selinux=enforcing，但
   ro.boot.selinux 为空、getenforce=Disabled、policy 仍为 0。
   说明 cmdline 不是唯一的门；redroid 还在更底层关掉了 SELinux。

### 4.2 根因：redroid 在源码里故意关了 SELinux

redroid 维护者 zhouziyang 在 issue 里明确说过：

- https://github.com/remote-android/redroid-doc/issues/501
  > selinux is disabled in source code (on purpose). Check patches for
  > external/selinux, system/core, frameworks/native and more.
  >
  > ... it's feasible to enable selinux in redroid
  > (should also enable selinux on host OS)

- https://github.com/remote-android/redroid-doc/issues/144
  > redroid currently not work while selinux enabled ...
  >
  > Redroid already disabled selinux (primarily via libselinux hook).
  > Might need undo the hook, and customize the SE rules if want run with selinux enabled

- https://github.com/remote-android/redroid-doc/issues/736
  > No concrete plan yet. But it's feasible to enable selinux support in redroid.

所以，容器内看到的 getenforce=Disabled 不是“少了一个启动参数”，而是
libselinux hook + system/core 补丁把 SELinux 整体短路了。

### 4.3 更根本的限制：SELinux policy 是内核全局的

即使把 redroid 的 hook 全部撤掉，SELinux policy 仍然是每个内核一份，
不是每个容器/namespace 一份。G98 宿主是 Armbian，不是 Android：

- 宿主 /sys/kernel/security/lsm = capability,selinux，SELinux LSM 在；
- 但宿主 /proc/cmdline 没有 selinux= / androidboot.selinux；
- 宿主没有加载 Android policy，dmesg 里只有 init 的
  Permissive SELinux boot, forcing sys.init.perf_lsm_hooks to 1.；
- 容器里的 setenforce 1 返回 SELinux is disabled。

如果在容器里强行加载 Android policy，这份 policy 会变成宿主全局 policy，
m1–m6 以及宿主进程都可能被错误 label/拒绝。这不是“容器级开关”，不能在生产板上试。

### 4.4 结论

在 **redroid + 共享 Armbian 内核 + 6 个容器**这个架构下，
真正的 SELinux enforcing 不可达。能做的只是：

- 无 root、无 adb、user/release-keys（已完成）；
- 不能把 getenforce 变成 Enforcing，也不能假装。

## 5. 这对“对标百度云手机”和身份 App 意味着什么

百度 R3588 云手机是平台侧定制的系统镜像 + 用户侧锁定，不是在公版 Armbian 上跑
redroid 再改几个属性。它的“免 root 支持 RT”说明 RT 可以进 system 镜像，
但不等于它的 SELinux/TEE 状态和 redroid 一样，也不等于身份 App 只检查 root。

对 cn.cyberIdentity.certification 这类 App：

- 无 TEE / 无 Key Attestation 是容器硬伤；
- ro.debuggable=0、无 su、无 adbd 只能消掉一部分“明显 root/调试”信号；
- 如果 App 进一步查 SELinux、内核、容器痕迹、虚拟相机、Play Integrity/Key Attestation，仍会失败；
- 不建议做 getenforce/属性伪装：不是真安全，且很容易被交叉校验。

## 6. 如果一定要继续，两条路

### 路线 A：VM / 独立内核（能拿到真 SELinux，但重）

用 QEMU/Cuttlefish/AVD 跑完整 Android，VM 自带内核和 SELinux policy，
不依赖宿主全局 policy。代价：

- 每个实例内存/CPU 开销远高于容器；
- G98 16GB 跑不了几台；
- TEE 仍是软件模拟，身份 App 未必认。

适合“要验证 SELinux 行为”或“少量高安全实例”，不适合 6 台云手机。

### 路线 B：自己编 Android + 撤 redroid SELinux 补丁（工作量大、风险高）

需要在 Android 13 源码上：

1. 撤掉 external/selinux、system/core、frameworks/native 里的 redroid SELinux hook；
2. 为容器场景定制 sepolicy（否则开机即大量 avc）；
3. 宿主也启用 SELinux 并加载与 Android 兼容的 policy，或改用 Android 宿主；
4. 回归 m1–m6、RT、相机 HAL、gralloc。

这不是 1–2 天的事，且会改变整块板子的安全模型；在没有独立测试板之前不建议动。

## 7. 当前资产与清理

板子上：

- 镜像：g98/redroid-panthor-locked:0.1
- 测试容器：locktest2、locktest3、locktest6、locktest8 等
- 诊断文件：/srv/redroid/init.orig、init.patched1、init.patched2、
  init-wrapper.c、init-wrapper、fake_cmdline

清理测试容器：

~~~
docker rm -f locktest locktest2 locktest3 locktest4 locktest5 locktest5b locktest6 locktest7 locktest8 lockedinspect 2>/dev/null
~~~

保留 locked 0.1 镜像即可；诊断文件可留在 /srv/redroid 供复现。

## 8. 一句话回答“怎么开始”

先做阶段 1：在 rt1 镜像上关 adbd、删 su、改 user/release-keys，commit 成
locked:0.1，用独立容器验证“无 root/无 adb/能开机”。这一步已经完成。
阶段 2 的 SELinux enforcing 不要在这个 redroid 架构上继续投入——它不是配置问题，
是 redroid 的源码设计 + 共享内核 policy 的架构限制；要真 SELinux 就上 VM/独立内核，
或者接受“hardened container”而不是“locked phone”。
