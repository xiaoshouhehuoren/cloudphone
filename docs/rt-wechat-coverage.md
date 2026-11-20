# 走 RT 控制微信：需要的能力 vs 我们现在的方案

> 结论先说：**底座（设备、系统镜像、root、Binder、预装 App）我们能覆盖；RT 本体（libsalespartner_rt.so + Probe 注入）目前不覆盖，需要一次 Android 13 移植。**
> 技术上可行（源码、root、可改镜像都有），但有三个硬点：ART/LSPlant 版本适配、framework（ActivityThread）注入点、system_server 的 system_broker。
> 如果只是把微信跑起来、用 UI 自动化做加好友/发消息，现有方案已经能用；要拿到通讯录/历史消息/未读/媒体哈希这类进程内数据，必须走 RT。

本文对照 `xiaoshouhehuoren-rom`、`xiaoshouhehuoren-app`、`workphone-channel` 三个仓库的源码，逐项判断我们（RK3588 + redroid Android 13 云手机）的覆盖情况。

## 一、RT 路线到底是什么

RT = ROM 里的 **Hook Runtime**（`xiaoshouhehuoren-rom/hook_runtime/`）：

```
Zygote / app_process
  └─ 加载 libsalespartner_rt.so（LSPlant ART hook + ARM64 inline trampoline + dobby/dex_builder）
       └─ ActivityThread 在 Application.attach() 之后调用 SalesPartnerHookRuntime.maybeBoot(app)
            └─ 校验 /product/app/WorkPhoneWechatProbe/WorkPhoneWechatProbe.apk 的 assets/xposed_init
                 └─ 仅当进程是 com.tencent.mm 时，用 InMemoryDexClassLoader 加载 Probe
                      └─ Probe 在微信进程内读 DB/prefs/media，通过 Binder BINDER_V3 / PROBE_CALLBACK_V3 与 Agent 通信
                           └─ Agent 用 SignedHttps 连 workphone-channel
```

关键源码位置：

| 组件 | 位置 |
| --- | --- |
| Java loader | `xiaoshouhehuoren-rom/hook_runtime/SalesPartnerHookRuntime.java` |
| native JNI | `xiaoshouhehuoren-rom/hook_runtime/native/runtime_jni.cpp`（`#include <lsplant.hpp>`、`elf_util.h`） |
| Xposed API shim | `hook_runtime/xposed_api/de/robv/android/xposed/*` |
| Probe 源码 | `xiaoshouhehuoren-app/android/common/probe/`（build-common-probe.sh / sign-rom-probe.sh） |
| Agent 源码 | `xiaoshouhehuoren-app/android/common/agent/`（build-common-agent.sh） |
| system_broker | `xiaoshouhehuoren-app/android/oneplus9/system_broker/`（AIDL：ISalesPartnerControl/AppState/ProbeStatus/ControlResult） |

ROM README 写明：当前目标是 OnePlus 9 Android 14 **user** 构建、SELinux enforcing、AVB 签名，且**仍需真机验收**（ART 符号兼容性和完整 hook 集只有真机能证明）。

## 二、RT 路线需要的能力清单

| # | 能力 | 具体要求 | 我们现状 | 判定 |
| --- | --- | --- | --- | --- |
| 1 | 设备与 root | 可改系统分区、可调试、无 SELinux 阻碍 | redroid Android 13：`ro.debuggable=1`、`ro.secure=1`、**SELinux Disabled**、`/system/xbin/su`，Docker 容器 root | ✅ 覆盖 |
| 2 | 系统镜像可控 | 预装系统 APK / native 库 / 改 framework | 已有 docker commit 流水线（cam9→cam15），/vendor 已验证可改，/system 同理可写 | ✅ 覆盖 |
| 3 | 预装 Probe / Agent | /product/app 或 /system/priv-app，平台签名 | 源码齐全，可编译；签名需换成我们自己的 key（见 #8） | ✅ 覆盖 |
| 4 | 精确微信版本 | Probe 钉死：8.0.66/2980、8.0.79/3200、8.0.80/3200 | m6 现装 **8.0.80 / versionCode 3220**，差 20，不在支持列表 | ⚠️ 需换 APK |
| 5 | RT native 库 | libsalespartner_rt.so + LSPlant + ARM64 trampoline | 仓库只有源码，没有针对 Android 13/redroid 的产物；需自己编 | ❌ 缺 |
| 6 | Zygote/app_process 注入 | 在 app_process 启动路径 dlopen RT 库 | redroid 预构建镜像，无源码构建链；需 patch app_process/libandroid_runtime 或等价方案 | ❌ 缺 |
| 7 | framework 注入点 | ActivityThread 在 Application.attach() 后调 SalesPartnerHookRuntime | redroid framework.jar 为预构建；需 smali 补丁或重建 framework | ❌ 缺 |
| 8 | 签名/证书白名单 | Agent 校验 Probe 证书 SHA-256（OnePlus testkey）；Probe 校验 Agent 签名 | 源码可改常量、可重签；无 OnePlus testkey | ⚠️ 需改源码重签 |
| 9 | Binder V3 | BINDER_V3 / PROBE_CALLBACK_V3 | Android Binder 原生支持，代码里有 | ✅ 覆盖 |
| 10 | system_broker | system_server 服务（应用控制、包白名单、签名校验） | 只有 Android 14 的 SystemServer 集成脚本；redroid 13 无对应源码/服务 | ❌ 缺（可先不做，不影响 Probe 主链路） |
| 11 | Channel 联网 | Agent → workphone-channel（SignedHttps、设备凭证、claim） | 容器默认无外网；已有 win_http_proxy + `settings put global http_proxy` 可通；Channel 地址/设备注册 flow 未接 | ⚠️ 需联调 |
| 12 | 风控/账号安全 | 微信官方签名、无 root/调试检测、hook 隐蔽性 | redroid 无 TEE、被 root、可调试，微信风控可能识别；RT 本身也需隐蔽 | ⚠️ 高风险 |

**汇总：✅ 4 项、⚠️ 4 项、❌ 4 项。**

## 三、我们现在的方案是什么、覆盖到哪

当前 `cloudphone` 仓库的交付：

- 6 台 redroid Android 13 容器（m1–m6），Docker + systemd，ADB 端口 5555–5560；
- 自研相机 HAL（Camera2 预览 + JPEG 拍照）、外部画面注入；
- 微信 8.0.80 已装、可启动；
- **adb over bridge IP** 控制通道：`uiautomator dump` / `screencap` / `input tap/text` 全部可用；
- 代理工具（win_http_proxy）让容器访问外网。

用这条 UI 自动化路线，今天能覆盖的业务能力：

| 业务能力 | UI 自动化（现状） | RT + Probe（目标） |
| --- | --- | --- |
| 手机号加好友 / 通过 / 备注 | ✅ 能做（搜索、点击、输入；备注需回读） | ✅ |
| 文本消息发送 | ✅ 能做（进聊天、输入、发送） | ✅（还可校验 msgId/status） |
| 图片/文件发送 | ✅ 部分（push 文件 + 选择器点击） | ✅（媒体哈希、FD 传输） |
| 通讯录 / 好友列表 | ⚠️ 只能逐屏读 UI | ✅ 直接读 raw |
| 历史消息 / 未读统计 | ❌ 读不到 DB | ✅ 直接投影 |
| 消息状态精确确认 | ❌ 只能看界面 | ✅ status=2 / msgId 校验 |
| 多账号 / 后台常驻 | ❌ 无 | ✅ |
| 会话变化实时回调 | ❌ 无 | ✅ |

结论：**UI 自动化能覆盖加好友 + 发消息 + 发文件这类前台流程，覆盖不了数据同步和状态类能力。**

## 四、要把 RT 落到 redroid，需要做哪些事

### 阶段 0：可行性验证（1–2 天）
1. 找到 Probe 支持的微信 **8.0.80 / versionCode 3200**（或 8.0.79 / 3200）安装包；
2. 用 app 仓库脚本编译 Probe / Agent（需要 Android SDK，仓库自带 build 脚本）；
3. 先验证 LSPlant 能否 hook redroid 的 ART 13（写一个最小 demo，向 `com.tencent.mm` 注入一个空 hook，看 `Probe booted` 日志）。

### 阶段 1：系统镜像改造（3–5 天）
1. 把 Probe APK 预装到 `/product/app/WorkPhoneWechatProbe/`（或 /system/priv-app），签名换成我们自己的 key；
2. 把 `libsalespartner_rt.so` 放进 `/system/lib64/`；
3. 在 `app_process` 启动路径加载 RT 库（最简：改 `libandroid_runtime.so` 的 `AndroidRuntime::start` 里 dlopen；或给 app_process 加 wrapper）；
4. 验证 `logcat | grep SalesPartner-HookRuntime` 出现 `Probe booted for com.tencent.mm`。

### 阶段 2：framework 注入（3–5 天）
1. 反编译/重建 redroid 的 `framework.jar`，在 `ActivityThread.handleBindApplication` 的 `Application.attach()` 后插入 `SalesPartnerHookRuntime.maybeBoot(app)`；
2. 把 `xposed_api` 的 6 个类打进 framework（或随 RT 库加载）；
3. 重启验证微信进程能加载 Probe、Agent 侧 `probe_loaded=true`。

### 阶段 3：业务联调（3–5 天）
1. 安装 Agent，配置 Channel 地址（`WORKPHONE_CHANNEL_PUBLIC_BASE_URL`）、设备凭证、扫码绑定；
2. 验证 Binder V3 的 12 个事务（HELLO → 消息 → 通讯录 → 媒体 → 文件 → 备注…）；
3. 按 workphone-channel 的 6 个 contract 做验收：device-heartbeat（probe_loaded/binder_ready）、chat-account-unread、wechat-contacts-sync、friend-add-active-customers、inbound-text-ignore、manage-tenant-devices。

总计约 **1–2 周（1 人）**，最大不确定性是 #5/#6：LSPlant 与 redroid ART 13 的兼容性、app_process 注入的稳定性。

## 五、建议

1. **短期先用 UI 自动化**：加好友、发消息、发文件这些前台流程现在就可用，先把业务跑起来；
2. **并行做阶段 0**：确认微信 3200 APK 和 LSPlant demo —— 这两件事任意一件不成立，RT 路线就要重新评估；
3. **不要先做 system_broker**：它只影响应用控制/保活，不影响 Probe 主链路，放到最后；
4. **风控预案**：redroid 的 root/调试特征明显，正式跑号前先用小号验证，避免主号被封。

---

*本文基于 2026-11-20 的仓库代码（xiaoshouhehuoren-rom / -app / workphone-channel）与 m6 实机状态整理。*