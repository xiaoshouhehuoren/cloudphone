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

### 4.7 实测：这个内核**接受** Android 的 SELinux policy

先说结论：**之前"内核不支持 Android SELinux class"的判断是错的**，
用一次真加载实验推翻了它。加载 Android policy 到 G98 的 Armbian 内核，成功。

**此前的错误推理**：拿 `strings` 数内核镜像里 `property_service`、`service_manager`、
`hwservice_manager`、`keystore_key` 的出现次数，得到 0，就推断内核缺少 Android
的 `classmap`。这个推理从根上不成立 —— **SELinux 的 class 与 permission 定义来自
policy 本身，不是内核二进制里的字符串常量**，所以数不到字符串完全正常。

**正确的验证方式（真加载）**：

    cd /srv/redroid/selinux
    ./selinux_load policy-kernel-permissive.bin      # 617,235 B，含 (typepermissive kernel)
    # loaded 617235 bytes, rc=0

结果：

| 检查项 | 加载前 | 加载后 |
| --- | --- | --- |
| `wc -c /sys/fs/selinux/policy` | 0 | 617235 |
| `ls /sys/fs/selinux/class \| wc -l` | 0 | **103** |
| `/sys/fs/selinux/class/property_service` | 不存在 | 存在，perm = `set` |
| `/sys/fs/selinux/class/service_manager` | 不存在 | 存在，perm = `add/find/list` |
| `/sys/fs/selinux/class/hwservice_manager` | 不存在 | 存在 |
| `/sys/fs/selinux/class/keystore_key` | 不存在 | 存在 |
| `/sys/fs/selinux/class/zygote` | 不存在 | 仍不存在（这份 policy 里没编进去） |

dmesg 只有一条反向提示 `SELinux: Class user_namespace not defined in policy`
（内核有、policy 没有，permissive 下无害），**没有任何 "Unknown class" 报错**。

再做 `echo 1 > /sys/fs/selinux/enforce`（policy 里 `(typepermissive kernel)`）：

- 宿主 `systemctl is-system-running` = degraded（两个既有失败单元：
  `g98-campush.service`、`NetworkManager-wait-online.service`，与 SELinux 无关）；
- m1–m6 全部 Up，m6 `sys.boot_completed`=1、`system_server`/`zygote` 存活、
  `dumpsys window` 正常无 ANR；
- 容器内读 `/sys/fs/selinux/enforce` = **1**；
- 期间约 180 条 avc denied，全部 `permissive=1`，没有实际拦截；
- 之后已把宿主 `enforce` 复位为 0（policy 加载后无法卸载，下次重启自动消失）。

**这意味着什么（以及为什么它还不够）**：

1. 内核不是阻塞点。redroid 镜像里 `libselinux` 把 `selinux_android_load_policy`
   编成 no-op，才是"容器里永远 Disabled"的直接原因；把这个 no-op 换成真实现，
   policy 是能加载进去的 —— 路线 B 在技术上是通的。
2. 但**加载 ≠ 有效强制**。当前是个 `(typepermissive kernel)` 的折中 policy：
   容器里所有进程都在 `u:r:kernel:s0`（permissive），所以 `enforce=1` 只是
   **名义上的 Enforcing**，任何一条 avc 都不会真拦。要变成真强制，就得给
   init/zygote/app 分域，而这又要求 redroid 的 init 走完整的 Android SELinux 流程。
3. 因此 4.6 的结论不变：**必须重编 userspace（libselinux + init + binder）**，
   只是原因从"内核不支持"改成"redroid userspace 主动关掉了 SELinux"。

用一句话区分两条路线的价值：

- 想让 App 里 `getenforce` 显示 `Enforcing`：今天就能做到（加载这份 policy + enforce=1），
  但它是名义强制，属于"看起来像"，本仓库明确不做这种伪装；
- 想真正获得 SELinux 强制：仍然要重编 userspace，且要让 per-domain 策略自洽。

### 4.8 根因：redroid 在 init 里**直接跳过了 `selinux_setup` 阶段**

4.7 说明了"内核没拦着"，但没回答"为什么 policy 一直没被加载"。查了 redroid 的
源码 fork，答案很干脆：**它把开机流程里专门负责加载 SELinux policy 的那一段删了**。

证据一，`remote-android/platform_system_core` 分支 `redroid-13.0.0_r74`，
提交 `e7385bfc9d`"fix booting"（`init/first_stage_init.cpp`）：

    -    const char* args[] = {path, "selinux_setup", nullptr};
    +    std::vector<const char *> args = {path, "second_stage"};
    +    { // 把 /proc/self/cmdline 里的容器参数原样塞进 argv
    +        ... ReadFileToString("/proc/self/cmdline", &cmdline); ...
    +    }
    -    execv(path, const_cast<char**>(args));
    +    execv(path, const_cast<char**>(args.data()));

AOSP 的第一阶段在 `execv("/system/bin/init", {"selinux_setup"})` 之后会进入
`LoadSelinuxPolicy()` → `set_selinuxmnt("/sys/fs/selinux")` →
`security_load_policy()`，那才是 policy 真正写进内核的地方。redroid 直接把
目标从 `selinux_setup` 改成 `second_stage`，这一整段就再也不会执行 ——
**这就是容器里 `/sys/fs/selinux/policy` 恒为 0、`class` 恒为 0 的原因**，
和内核、和 libselinux 的 no-op 都只是"配套"关系。

证据二，同一个 commit 还在 init 自己的调用点里塞了 `se_hack1`：

    init/property_service.cpp:  CheckMacPerms()            + se_hack1(true);       // HACKED
    init/service.cpp:           ComputeContextFromExecutable() + se_hack1("HACKED");

也就是说，**即使把 selinux_setup 加回来、policy 加载成功，init 也会跳过属性权限检查
和 service 的 SELinux 上下文推导**，照样不是真正的强制。

证据三，`external/selinux` 的 `085e5a0720`"ignore selinux" 一共只改了 14 个函数
（`selinux_android_load_policy`、`security_getenforce`、`selinux_check_access`、
`selinux_android_restorecon_common`、`selinux_android_setcon`、
`selinux_android_setcontext`、`getfilecon`/`lgetfilecon`/`getpeercon`、
`setprocattrcon`/`getprocattrcon`、`selabel_lookup_best_match`、`set_selinuxmnt`、
`selinux_status_open`、`selinux_android_service_open_context_handle`），
做法是加了个宏：

    #define SE_HACK
    #define se_hack()  if (1) return
    #define se_hack1(p) if (1) return p

**这反而是个好消息**：改动小、位置明确，理论上只要重编 `external/selinux`
（一个约 14MB 的仓库）就能拿回"能加载 policy"的 libselinux。但如上，光有库不行，
init 的 `selinux_setup` 和两处 `se_hack1` 也得一起改回来 —— **init 必须重编**。

**架构层面的硬约束（比"编不出来"更致命）**

`/sys/fs/selinux` 在宿主和 m6 里是**同一个 dev/inode**（dev 23 ino 1），
selinuxfs 没有 namespace：**一个内核只有一份 policy**。G98 上 Armbian 宿主 + m1–m6
六台容器共用同一个内核，所以：

- 容器自己 `echo ... > /sys/fs/selinux/load` 直接被拒（实测 rc=1，即使 privileged）；
- 就算在容器里加载成功，那份 policy 会同时作用于**宿主和全部六台容器**；
- 六台实例无法各自持有不同 policy（真机/云手机产品里每台实例是独立内核的 VM）。

结论：**红旭这种容器模型在架构上不可能提供"每实例独立 SELinux 强制"**。
能做的只有"整个板子共享一份 permissive 折中 policy"，那对身份类 App 没有意义。
要让 SELinux 成为真实的安全边界、且每实例独立，只有 VM 一条路。

### 4.9 实测：板子上 VM 路线打通（KVM + 板载内核 + binder）

2026-11-20 在 G98 上把“每实例独立内核”这条路走通了。

**一、KVM 是真的能用**
自写 C 探针（纯 ioctl）跑出 `KVM_GET_API_VERSION=12`、`KVM_GET_VCPU_MMAP_SIZE=8192`、
`KVM_CREATE_VM=4`、`KVM_CREATE_VCPU=5` 全部成功；`/dev/kvm`（10,232）、`/dev/vhost-net` 存在，
`/proc/interrupts` 有 vgic / kvm guest ptimer / kvm guest vtimer。

**二、qemu 与固件已就位**
- `apt-get install qemu-system-arm qemu-utils` → RC=0，`qemu-system-aarch64 1:10.0.13+ds-0+deb13u1`
- `qemu-efi-aarch64`：AAVMF 固件 `/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd` + `AAVMF_VARS.fd`
- `ipxe-qemu`：提供 `efi-virtio.rom`，缺了会报 `failed to find romfile "efi-virtio.rom"`

**三、两个必须知道的 QEMU 坑**
1. `-drive if=virtio` 会去找 virtio 的 option ROM，Debian 上默认不带；
   装 `ipxe-qemu` 解决，或用 `-device virtio-blk-pci,drive=hd0,romfile=` 显式关掉。
2. **不要用 `-kernel` 直接塞 Debian cloud 内核**：它的 virtio-blk 是模块
   （`CONFIG_VIRTIO_BLK=m`），没有 initrd 就
   `Kernel panic - not syncing: VFS: Unable to mount root fs on unknown-block(0,0)`。
   要么走 UEFI（两个 pflash）让镜像自己的 grub/initrd 干活，要么用板载内核 + 板载 initrd。

**四、Debian 12 arm64 guest 起得来**
`debian-12-genericcloud-arm64.qcow2`（345,047,040 B）+ AAVMF → UEFI → kernel → systemd：
客机日志里 `Freeing unused kernel memory` 在 0.68s，`systemd[1]` 在 1.42s 起来。

**五、决定性的一步：板载内核在 guest 里跑，而且 binder 可用**

~~~
qemu-system-aarch64 -machine virt,gic-version=3 -accel kvm -cpu host \
  -smp 4 -m 3072 \
  -kernel /boot/vmlinuz-6.18.48-ophub -initrd /boot/initrd.img-6.18.48-ophub \
  -append "console=ttyAMA0 root=/dev/vda1 rw cloud-init=disabled" \
  -drive file=deb12.qcow2,if=none,id=hd0,format=qcow2 -device virtio-blk-pci,drive=hd0 \
  -device virtio-net-pci,netdev=n0 -netdev user,id=n0,hostfwd=tcp::2222-:22 \
  -nographic
~~~

guest 里实测：

~~~
# uname -r
6.18.48-ophub
# grep binder /proc/filesystems
nodev   binder
# mkdir -p /dev/binderfs && mount -t binder none /dev/binderfs && echo OK
OK
# ls /dev/binderfs
binder binder-control features hwbinder vndbinder
~~~

注意 binderfs 的文件系统类型名是 `binder` 而不是 `binderfs`：
`mount -t binderfs ...` 会报 `unknown filesystem type 'binderfs'`，那是名字写错了，不是没编。

配置佐证：`/boot/config-6.18.48-ophub` 有 `CONFIG_ANDROID_BINDER_IPC=y`、
`CONFIG_ANDROID_BINDERFS=y`、`CONFIG_ANDROID_BINDER_DEVICES="binder,hwbinder,vndbinder"`；
而 Debian 官方 `linux-image-6.12.117+deb13-cloud-arm64` 是
`# CONFIG_ANDROID_BINDER_IPC is not set` —— **Debian 内核跑不了 Android，板载内核可以**。

**六、这意味着什么**
binder 是 Android userspace 的地基。板载内核在 guest 里能挂 binderfs，说明
“把 redroid 的 Android rootfs 搬进这台 VM、由它自己的内核承载”在技术上可行。
真做成之后，每个实例有自己的内核 → 有自己的 selinuxfs → 有自己的 policy，
4.8 的“一内核一份 policy”约束就被绕开了。

代价前面已算过：一台 3GB / 4vCPU，6 台放不进 16GB。

**七、还没做的**
把 `g98/redroid-panthor-rt-rt1:13` 的 rootfs 导出成 ext4 磁盘，在 VM 里
chroot / exec `/system/bin/init`，让它以真 SELinux 起来 —— 这是下一步的硬活，
也是整个 VM 路线能不能落地的分水岭。

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

### 路线 A：VM / 独立内核（唯一能拿到真 SELinux 的路，已具备硬件条件）

VM 有自己的内核，selinuxfs 和 policy 都是实例私有的，不碰宿主那份 —— 这是 4.8 里
"一内核一份 policy"约束的唯一出口。**G98 这块板子本身就有条件跑**：

- `/dev/kvm` 存在（10,232），自写 C 探针实测 `KVM_GET_API_VERSION=12`、
  `KVM_CREATE_VM`、`KVM_CREATE_VCPU` 全部成功，GICv3 就绪
  （`/proc/interrupts` 有 vgic / kvm guest ptimer / kvm guest vtimer）；
- 板子通过 Windows 反向代理拿到了公网，`apt-get install qemu-system-arm` 已装成功
  （`qemu-system-arm 1:10.0.13+ds-0+deb13u1`，`/usr/bin/qemu-system-aarch64` 就位，RC=0）；
- 资源：8 核、15.9GB 内存（可用约 9.9GB）、`/` 剩 402G；
- 已有 AOSP13 GSI 镜像可当 guest 系统：`/root/gsi13/system.img`（1.9GB）+ `vbmeta.img`。

**guest 内核问题已经解决（2026-11-20 实测，见 4.9）**：板子自己的
`/boot/vmlinuz-6.18.48-ophub` + `/boot/initrd.img-6.18.48-ophub` 可以直接在
QEMU `virt` 上启动，而且它**自带 binder / binderfs**（红旭容器需要，而 Debian
官方内核反而不带）。这意味着“Android rootfs 跑在独立内核的 VM 里”在板子上是可行的。

代价不变：每实例内存/CPU 开销远高于容器；实测一台 3GB / 4vCPU 的 guest，
宿主 `Mem: 15957 总 / 9800 可用`，6 台 × 3GB 超出 16GB，实际只能 4 台 × 3GB 或 6 台 × 2GB 配 swap。

适合"要验证 SELinux 行为"或"少量高安全实例"，不适合 6 台云手机。

### 路线 B：自己编 Android + 撤 redroid SELinux 补丁（工作量大、风险高）

需要在 Android 13 源码上：

1. 撤掉 external/selinux、system/core、frameworks/native 里的 redroid SELinux hook；
2. 为容器场景定制 sepolicy（否则开机即大量 avc）；
3. 宿主也启用 SELinux 并加载与 Android 兼容的 policy，或改用 Android 宿主；
4. 回归 m1–m6、RT、相机 HAL、gralloc。

其中第 2 步的 policy 层已经在测试板上验证：`secilc` 可重编，`(typepermissive kernel)`
可让 Armbian 宿主在 `enforce=1` 下存活；第 1 步已经用 GSI13 的 stock 二进制做过
组件级替换（见 4.6），三种组合均失败，确认必须真重编。

**但即使全部编好，这条路在架构上仍然不成立**（见 4.8）：容器共享一个内核，
一份 policy 要同时罩住 Armbian 宿主和 6 台 Android 容器，没法给每台实例独立策略。
所以重编能换来的只是"整个板子处于名义 enforcing"，拿不到"每实例真强制"。
**结论：路线 B 不再作为可行方案推进。**

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
