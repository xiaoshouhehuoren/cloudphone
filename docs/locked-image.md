# G98 locked image（0.1）：已完成的部分与 SELinux 边界

> 结论先行：**“无 root、无 adb、user/release-keys”已经在 g98/redroid-panthor-locked:0.1 上验证通过；SELinux 在测试板上已经做到“加载 Android policy + 全局 enforce=1，宿主用 permissive kernel 域存活”；但 redroid 容器里的服务仍然全部落在 kernel 域，只能算策略层验证，不是容器级 enforcing。真正要让容器服务进入各自 domain，必须撤掉 redroid 的 libselinux/init 补丁并重编。** 本文记录已做的改动、验证结果、SELinux 失败原因，以及要继续走的两条路。

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

### 4.4 测试板实验：policy 能加载，enforce 能打开（2026-11-20）

在 G98 测试板上做了受控实验。结论分三层：

1. **Android 13 policy 能被 6.18 内核加载。**
   从 `g98/redroid-panthor-rt-rt1:13` 取出
   `/vendor/etc/selinux/precompiled_sepolicy`（616,315 B），
   静态 loader 写 `/sys/fs/selinux/load`，返回 `loaded 616315 bytes`，RC=0。
   加载前 `/proc/self/attr/current=kernel`，加载后变成 `u:r:kernel:s0`；
   dmesg 出现 `SELinux: ...`、unknown class（mctp_socket、io_uring、
   user_namespace、watch_mountns 等）和 `avc: denied ... permissive=1`。
   说明内核接受这份 policy，只是宿主进程没有 domain transition，全部落在
   `kernel` 域。

2. **用 secilc 可以重编 policy，把 `kernel` 域设为 permissive。**
   容器内 `/system/bin/secilc` 可用（policy version 33），命令：
   `secilc -m -M true -G -c 33 -o policy.bin plat_sepolicy.cil 33.0.cil plat_pub_versioned.cil vendor_sepolicy.cil extra.cil`
   其中 `extra.cil` 只有一行 `(typepermissive kernel)`。
   生成的 policy 为 617,235 B，加载成功；`echo 1 > /sys/fs/selinux/enforce`
   后 `enforce=1`，SSH、m1–m6 都存活，dmesg 中 `permissive=0` 计数为 0。

3. **但这不是容器级 enforcing。**
   容器内 raw `/sys/fs/selinux/enforce=1`，而进程仍然
   `/proc/self/attr/current=u:r:kernel:s0`；`getenforce` 仍是 `Disabled`
   （redroid 的 libselinux `is_selinux_enabled/security_getenforce` hook）。
   所有 Android 服务都在 permissive 的 `kernel` 域，等于“全局开关打开了，
   但没有 domain 可执行”。要变成真 enforcing，仍需撤掉
   `external/selinux` / `system/core` 的 hook 并重编镜像。

实验后已 `echo 0` 并 reboot，宿主恢复 `enforce=0`、`/proc/self/attr/current=kernel`，
m1–m6 由 `unless-stopped` 自动拉起。产物保留在
`/srv/redroid/selinux/`（`policy-kernel-permissive.bin`、`selinux_load`、
`selinux_load.c`、`extra-kernel-permissive.cil`）。这验证了“策略层可行”，
但没有改变“当前 redroid 镜像无法做容器级 enforcing”的结论。

### 4.5 结论

在 **redroid + 共享 Armbian 内核 + 6 个容器 + 当前 rt1/locked 镜像**这个组合下，
真正的“每个容器进入自己的 SELinux domain 并 enforcing”不可达；但“加载 policy、
打开全局 enforce、用 permissive kernel 域保住宿主”已经在测试板上验证可行。能做的：

- 无 root、无 adb、user/release-keys（已完成）；
- 策略层：加载 Android policy、`enforce=1`、`kernel` permissive（测试板已验证）；
- 容器服务级：仍需重编 libselinux/init，当前镜像做不到；
- 不能把 `getenforce` 改成 Enforcing 来假装，容器里它仍会显示 Disabled。

### 4.6 组件级替换实验：用 GSI13 的 stock 二进制换掉 hook（2026-11-20）

路线 B 先试了最省事的一条：不重编，直接把 GSI13 里**未被 patch 的 stock 二进制**
换进 redroid 容器。

**对照证据（为什么值得试）**

| 文件 | redroid rt1 | GSI13 (TQ3A.230805.001.A3) |
| --- | --- | --- |
| `/system/lib64/libselinux.so` | 70K，sha256 `91c4c689…`；`selinux_android_load_policy` @0xea80 只有 8 字节 `mov w0,wzr; ret`，`security_getenforce` @0xc15c 同样 | 103K，sha256 `96bd1b0c…`；同函数分别 0xbc / 0xfc 字节，是真实实现 |
| `/system/lib64/libbinder.so` | 753K，含字符串 `HACKED` | 818K，无 `HACKED` |
| `/system/bin/init` | 2,085,760 B，sha256 `7fe2d024…`，与 `base-init`(`94836b01…`) 不同 | 2,085,472 B，sha256 `f1484207…`，无 `/proc/self/cmdline` 改写逻辑 |

**实验 A：locktest9（stock libselinux + `androidboot.selinux=enforcing`）**

```bash
docker run -d --name locktest9 --privileged --network none --memory=3g \
  -v /srv/redroid/gsi13-libselinux.so:/system/lib64/libselinux.so:ro \
  --entrypoint /init g98/redroid-panthor-locked:0.1 \
  qemu=1 androidboot.hardware=redroid ro.hardware.gralloc=g98 \
  ro.hw_timeout_multiplier=10 androidboot.selinux=enforcing
```

结果：容器 0.2 秒内 `Exited (129)`，日志为空；把同一路径换回 redroid 自己的
libselinux（`/srv/redroid/m6-libselinux.so`）后容器正常 running —— 说明是 stock
libselinux 触发，不是挂载或 entrypoint 问题。宿主 dmesg 记录：

```
init: Service 'apexd-bootstrap' ... exited
selinux: Unknown class property_service
init: Unable to set property 'init.svc.apexd-bootstrap' ... SELinux permission check failed
init: Service with 'reboot_on_failure' option failed, shutting down system.
init: Reboot ending, jumping to kernel
```

**注意**：容器是 `--privileged`，init 持有 CAP_SYS_BOOT，这一下把 G98 宿主直接重启了
（m1–m6 靠 `unless-stopped` 自动拉起）。后续所有这类实验必须加
`--cap-drop SYS_BOOT`。

**实验 B：lt-noreboot（把 apexd 的 reboot_on_failure 去掉）**

```bash
docker cp m6:/system/etc/init/apexd.rc /tmp/apexd.rc
grep -v reboot_on_failure /tmp/apexd.rc > /srv/redroid/apexd-noreboot.rc   # 395 B
```

同样 stock libselinux，但额外把 `apexd.rc` 覆盖掉、cmdline 改
`androidboot.selinux=permissive`。结果：容器不再重启，但**卡在 early stage**——

- `sys.boot_completed` 空、`init.svc.zygote` 空、`getprop` 里连 `ro.boot.*` 都没有；
- `/proc/1/cmdline` 正常带着 `second_stage` 和全部参数，说明参数进来了却没被导入成属性；
- `/proc/1/attr/current` = `kernel`，`/sys/fs/selinux/class` 计数 **0**，即 policy 根本没加载；
- dmesg 反复刷 `ueventd: Cannot get SELinux label on '...': No data available`、
  `selinux: Unknown class property_service`。

**实验 C：整块 stock system 当 `/system` 启动**

把 `/root/gsi13/system.img`（AOSP13 GSI）loop 挂载后直接 bind 到容器 `/system`，
entrypoint 用 stock `init`：

```
init: mount("sysfs", "/sys", "sysfs", 0, NULL) failed Device or resource busy
init: [libfs_mgr]ReadDefaultFstab(): failed to find device default fstab
init: Failed to create FirstStageMount failed to read default fstab for first stage mount
init: Failed to mount required partitions early ...
init: InitFatalReboot: signal 6
```

先用静态 wrapper `/srv/redroid/gsi-init-wrapper` 把 `/dev`、`/proc`、`/sys` detach 掉，
前一层的 `mount sysfs failed` 消失了，但**仍然死在 first-stage mount**：stock init 要求
fstab/super 分区。这正是 redroid `system/core/0001-fix-booting.patch` 改掉的东西
（`FirstStageMain` 改成直接 exec `/system/bin/init second_stage`）。换句话说，
stock 和 redroid 的用户态启动流程从第一条指令起就不兼容。

（顺带确认：GSI 的 `sh`/`toybox` 不能直接当 `--entrypoint`，它们的解释器
`/system/bin/linker64 → /apex/com.android.runtime/...` 在 init 挂载 APEX 之前不存在。）

**4.6 结论**：单独换 libselinux / 换 init / 换整块 system，三种组合都跑不通。
redroid 的 SELinux 关闭是**横跨 libselinux + init + binder 的一整套**，且 init 的
first-stage 流程本身被改过；要撤掉它，必须重编这三个模块，即回到完整的 AOSP 构建。

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

其中第 2 步的 policy 层已经在测试板上验证：`secilc` 可重编，`(typepermissive kernel)`
可让 Armbian 宿主在 `enforce=1` 下存活；第 1 步已经用 GSI13 的 stock 二进制做过
组件级替换（见 4.6），三种组合均失败，确认必须真重编。

**重编需要什么（G98 这块板子做不到）**

| 需要 | 现状 |
| --- | --- |
| x86_64 Linux 构建机 | G98 是 aarch64。AOSP 官方明确不支持 aarch64 宿主：`Building Android requires a x86-64 machine`，prebuilt 工具链全是 x86-64 |
| AOSP 13 源码 + redroid manifest | `/root/redroid-build/work/.repo` 只有 6.7M 元数据，`repo sync` 从未成功（sync.log 是 BrokenPipeError）；manifest 指向 `android.googlesource.com`（`android-13.0.0_r74`）+ redroid 自建 remote |
| 网络 | 板子无 default route（只有 192.168.99.0/24 与 docker0），`192.168.99.1`/`.254` 都 ping 不通，DNS 解析全失败 |
| clang/ninja | 板上只有 gcc 14.2.0；无 clang、无 ninja |
| 磁盘 | 板上剩 402G，AOSP 13 全量 + out 约需 400G+，勉强 |

结论：路线 B 的“真重编”只能在**另备一台 x86_64 Linux 机器**上做，
产出 `external/selinux`、`system/core`、`frameworks/native` 三个模块的
未打补丁版本（或整体 `camera.v4l2` + RT 的完整镜像），再回灌到 G98。

**更省力的替代**：只重编这三个模块反而不划算——redroid 的 init 与 stock 的差异是
结构性的（first-stage mount、fstab、apex），单换模块大概率仍然起不来；实际工作量
接近整包编译。

没有独立测试板之前，不建议把这套东西放到生产容器上。

## 7. 当前资产与清理

板子上：

- 镜像：g98/redroid-panthor-locked:0.1
- 测试容器：locktest2、locktest3、locktest6、locktest8 等
- 诊断文件：/srv/redroid/init.orig、init.patched1、init.patched2、
  init-wrapper.c、init-wrapper、fake_cmdline
- SELinux 策略实验：/srv/redroid/selinux/policy-kernel-permissive.bin、
  selinux_load、selinux_load.c、extra-kernel-permissive.cil
- 组件级替换实验：/srv/redroid/gsi13-libselinux.so、m6-libselinux.so、
  apexd-noreboot.rc、gsi-init-wrapper（静态，detach /dev /proc /sys 后 exec stock init）
- 只读挂载的 GSI13：`mount -o loop,ro /root/gsi13/system.img /mnt/gsi13`（实验后已 umount）

清理测试容器：

~~~
docker rm -f locktest locktest2 locktest3 locktest4 locktest5 locktest5b locktest6 locktest7 locktest8 lockedinspect 2>/dev/null
~~~

保留 locked 0.1 镜像即可；诊断文件可留在 /srv/redroid 供复现。

## 8. 一句话回答“怎么开始”

先做阶段 1：在 rt1 镜像上关 adbd、删 su、改 user/release-keys，commit 成
locked:0.1，用独立容器验证“无 root/无 adb/能开机”。这一步已经完成。
阶段 2 在测试板上可以继续做“策略层”：加载 Android policy、`enforce=1`、
`kernel` permissive 已经验证；但要让容器服务真正进入自己的 domain，必须重编
redroid 的 libselinux/init，或改走 VM/独立内核。当前 locked 0.1 仍应称为
“hardened container”，不是“locked phone”。
