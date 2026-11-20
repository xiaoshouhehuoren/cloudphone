# RT 控制微信落地记录（Android 13 / redroid / RK3588）

更新时间：2026-11-20
状态：**已落地并验证**（m6，Android 13 arm64；Camera2 预览/拍照能力保留）

## 0. 结论

RT（Hook Runtime）已经在 redroid 云手机上跑通：

- 镜像：`g98/redroid-panthor-rt-rt1:13`（sha256 `627637390ce3c1cdac06883ae8edf5d1c9d12bd47581fcb50dccdfdadbccb419`，1.81GB）
- m6 容器内微信 `com.tencent.mm` 进程已加载 `/system/lib64/libsalespartner_rt.so`
- Probe `com.salespartner.workphone.wechatprobe` 在微信进程内 `probe_state=ready`
- Probe → Agent Binder 已 `authenticated`，`probe_hello_connected`
- Agent `1.15.1 (2000029)` / Probe `1.11.3 (2000026)`，UI 显示 **在线 / 服务器已连接**
- 原有 Camera2 设备数仍为 2（相机能力未被破坏）

验证截图见 [agent-online.png](../screenshots/rt-agent-online.png)。

## 1. 总览

```text
/system/framework/framework.jar
  └─ classes.dex 已插入 ActivityThread.handleBindApplication()
       └─ makeApplicationInner() -> Application 实例 v7
            └─ invoke-static {v7}, Lcom/salespartner/workphone/runtime/SalesPartnerHookRuntime;->maybeBoot(Landroid/app/Application;)V
  └─ classes5.dex = SalesPartnerHookRuntime + xposed_api（新增，不污染原 4 个 dex）

/system/lib64/libsalespartner_rt.so
  └─ LSPlant + SandHook inline hook + dex_builder，JNI 注册到 de.robv.android.xposed.XposedBridge

/system/product/app/WorkPhoneWechatProbe/WorkPhoneWechatProbe.apk
  └─ assets/xposed_init = com.salespartner.workphone.wechatprobe.WorkPhoneReceiveProbe
       └─ 仅当进程是 com.tencent.mm 且是主进程时加载

/system/product/app/WorkPhoneAgent/WorkPhoneAgent.apk
  └─ Agent 1.15.1，负责 SignedHttps 连 workphone-channel / mock channel

/system/bin/lmkd
  └─ fake lmkd：只 accept/read 丢弃，保证 system_server 的 LmkdConnection 不阻塞
```

## 2. 镜像内文件清单

| 路径 | 大小 | md5 | 说明 |
| --- | ---: | --- | --- |
| `/system/framework/framework.jar` | 14,630,261 | `7d40c1040fd37f3f9d94d997ae2f397c` | classes.dex 已打补丁 + classes5.dex runtime |
| `/system/lib64/libsalespartner_rt.so` | 2,222,024 | `8c01b58fcc35f493a3958ada323b61cb` | LSPlant / JNI hook 库 |
| `/system/product/app/WorkPhoneWechatProbe/WorkPhoneWechatProbe.apk` | 156,187 | 见 apk 签名 | Probe 1.11.3 (2000026) |
| `/system/product/app/WorkPhoneAgent/WorkPhoneAgent.apk` | 225,690 | 见 apk 签名 | Agent 1.15.1 (2000029) |
| `/system/bin/lmkd` | 8,776 | `51fb1235f103fdd01afda15393c8ab91` | fake lmkd |
| `/system/bin/rt-provision.sh` | 1,211 | 见仓库 | Agent enrollment 初始化脚本 |
| `/vendor/lib64/hw/camera.redroid.so` | 675,744 | `22341301c3645b16c02bf1df96ff4dec` | cam15 的 Camera2 HAL（JPEG 版） |

Probe / Agent 使用同一把自签 key（WorkPhone，证书 SHA-256 `07ead97511f90d9424fb0ab2caa96a0edea3dc670a82670513e01e426ef22c91`）。
AgentCallerVerifier 先做 `checkSignatures(AGENT_PACKAGE, PROBE_PACKAGE) == SIGNATURE_MATCH`，同签名即通过；不需要官方 ROM_RELEASE 证书。

## 3. 构建步骤

### 3.1 工具链

- JDK 17（`rtwork/jdk17/jdk-17.0.21+9`）
- Android SDK：build-tools 35.0.0、platforms/android-35
- Android NDK r26d
- smali / baksmali 3.0.10（2.5.2 在 Android 13 framework dex 上会 ArrayIndexOutOfBounds）

### 3.2 Probe / Agent

源码：`xiaoshouhehuoren-app/android/common/probe` 与 `.../agent`。

redroid 是 Android 13（SDK 33），需要把两个 Manifest 的 minSdk/targetSdk 改为 33，R8 的 `--min-api` 也改 33。Agent 的 `WorkPhonePolicy.java` 里 `Build.VERSION.SDK_INT >= 34` 改为 `>= 33`，否则 `AgentRestartReceiver` 直接 `skipped reason=unsupported_device`。

Windows/MSYS 下构建脚本的适配点：

- `case` 增加 `MINGW*|MSYS*`，校验 `aapt2.exe` / `zipalign.exe`
- 所有 `python3` 改 `python`
- javac 的 `@sources.txt` 参数在 Windows 不展开，改为 `$(cat sources.txt)`
- R8 的几千个 .class 参数会超过 Windows 命令行长度，先 `jar cf classes.jar` 再喂 R8

产物与签名：

```bash
# 构建脚本会输出工作目录到 .tmp/workphone-channel-probe 或 -agent-*
bash build-common-probe.sh
bash build-common-agent.sh
# 使用 apksigner 签同一把 key（脚本 rtwork_sign_apks.py）
java -jar build-tools/lib/apksigner.jar sign --ks workphone.keystore ...
```

### 3.3 libsalespartner_rt.so

```bash
NDK=/path/android-ndk-r26d/toolchains/llvm/prebuilt/windows-x86_64/bin/aarch64-linux-android33-clang++.exe
$NDK -std=c++20 -fPIC -shared -O2 -static-libstdc++ -fexceptions -frtti \
  -DFMT_HEADER_ONLY=1 -Wno-unused-parameter -Wno-deprecated-declarations \
  -Wno-c++11-narrowing -Wno-psabi -Wno-c++2b-extensions \
  -I native -I third_party/lspd_jni/include -I third_party/lspd_jni/src \
  -I third_party/fmt/include -I third_party \
  -I third_party/lsplant/lsplant/src/main/jni/include \
  -I third_party/lsplant/lsplant/src/main/jni \
  -I third_party/dex_builder/include \
  native/runtime_jni.cpp \
  third_party/lspd_jni/src/elf_util.cpp \
  third_party/lsplant/lsplant/src/main/jni/lsplant.cc \
  third_party/dex_builder/dex_builder.cc third_party/dex_builder/dex_helper.cc \
  third_party/dex_builder/slicer/*.cc third_party/dex_builder/slicer/*.cpp \
  -llog -ldl -lz -o librt.so
```

注意：**不要加 `-I third_party/cxxshim`**，它会在 namespace std 里重定义 `same_as`，与 libc++ 的 C++20 concept 冲突。

### 3.4 runtime classes5.dex

```bash
# 7 个 Java 源：hook_runtime/SalesPartnerHookRuntime.java + xposed_api 下 6 个文件
javac -source 8 -target 8 -bootclasspath android.jar -d classes <7 个源>
jar cf runtime_classes.jar -C classes .
java -cp build-tools/lib/d8.jar com.android.tools.r8.D8 --min-api 33 --output runtime_dex runtime_classes.jar
# runtime_dex/classes.dex 约 13KB
```

### 3.5 framework.jar 补丁

1. 从设备拉 `/system/framework/framework.jar`，解出 `classes.dex`..`classes4.dex`。
2. baksmali 3.0.10 反编译 `classes.dex` 到 `bak_classes`。
3. 在 `android/app/ActivityThread.smali` 的 `handleBindApplication` 中找：

```smali
invoke-virtual {v0, v8, v7}, Landroid/app/LoadedApk;->makeApplicationInner(ZLandroid/app/Instrumentation;)Landroid/app/Application;
move-result-object v0
move-object v7, v0
```

在 `move-object v7, v0` 之后插入：

```smali
invoke-static {v7}, Lcom/salespartner/workphone/runtime/SalesPartnerHookRuntime;->maybeBoot(Landroid/app/Application;)V
```

4. 回汇编：

```bash
java -Xmx8g -jar smali310.jar a --api 33 -o classes_patched.dex bak_classes
```

5. 重新打包 framework.jar：用 `classes_patched.dex` 替换原 `classes.dex`，保留原 `classes2.dex`..`classes4.dex`，再放入 `classes5.dex`（runtime）。

### 3.6 fake lmkd

redroid 里 `/system/bin/lmkd` 会被 init 反复拉起又 exit 0；`ProcessList.writeLmkd` → `LmkdConnection.waitForConnection` 在持有 ActivityManagerService 锁时最多等 3 秒，结果所有 App 启动都卡死。fake lmkd 只需做 AF_UNIX SOCK_SEQPACKET 的 accept/read 丢弃，让 `LmkdConnection` 认为已连接即可。

源码：`rt/fake_lmkd.c`；编译：

```bash
aarch64-linux-android33-clang -O2 -fPIE -pie -o lmkd_fake fake_lmkd.c -llog
```

> 这是兼容层，不是内存杀手；不要把 stock lmkd 换回来，否则 App 启动会再次全部卡死。

## 4. 部署到 m6

```bash
# 0) 备份
adb shell 'cp /system/framework/framework.jar /data/local/tmp/framework.jar.bak'
adb shell 'cp /system/framework/boot-framework.vdex /data/local/tmp/boot-framework.vdex.bak || true'

# 1) 推文件（adb push / docker cp 均可）
adb push framework_patched.jar /data/local/tmp/framework.jar
adb push librt.so /data/local/tmp/libsalespartner_rt.so
adb push probe-signed.apk /data/local/tmp/probe-signed.apk
adb push agent-signed.apk /data/local/tmp/agent-signed.apk
adb push lmkd_fake /data/local/tmp/lmkd_fake
adb push rt-provision.sh /data/local/tmp/rt-provision.sh

# 2) 放入系统
adb shell 'cp /data/local/tmp/framework.jar /system/framework/framework.jar'
adb shell 'cp /data/local/tmp/libsalespartner_rt.so /system/lib64/libsalespartner_rt.so'
adb shell 'mkdir -p /system/product/app/WorkPhoneWechatProbe /system/product/app/WorkPhoneAgent'
adb shell 'cp /data/local/tmp/probe-signed.apk /system/product/app/WorkPhoneWechatProbe/WorkPhoneWechatProbe.apk'
adb shell 'cp /data/local/tmp/agent-signed.apk /system/product/app/WorkPhoneAgent/WorkPhoneAgent.apk'
adb shell 'cp /data/local/tmp/lmkd_fake /system/bin/lmkd'
adb shell 'cp /data/local/tmp/rt-provision.sh /system/bin/rt-provision.sh'
adb shell 'chmod 755 /system/bin/lmkd /system/bin/rt-provision.sh'
adb shell 'chmod 644 /system/product/app/WorkPhoneWechatProbe/WorkPhoneWechatProbe.apk /system/product/app/WorkPhoneAgent/WorkPhoneAgent.apk'
adb shell 'rm -f /system/framework/boot-framework.vdex'

# 3) 重启
adb reboot
```

> `/system` 在 redroid 是 overlay 可写；如果目标板子不是，需要先 remount 或直接把上述文件打进镜像。
> 不要在 m1–m5 上直接做，先在 m6 验证；RT 需要与相机 HAL 同镜像时，把 `camera.redroid.so` 一起放进 `/vendor/lib64/hw`。

### 4.1 Agent enrollment

```bash
adb shell 'sh /system/bin/rt-provision.sh cred_local_20261120_0001 http://192.168.99.1:8898'
```

脚本会先启动一次 Agent 生成 `workphone_device_identity.xml`（`device_id=wpdev_...`），然后写 `workphone_native_v4_enrollment.xml`（`credential_id` / `api_base`）并重启 Agent service。真实环境把 `api_base` 换成 Channel 地址。

### 4.2 本地 mock Channel（联调用）

Windows 侧起：
```bash
python rt/win_mock_channel.py 8898
```

m6 内配置：
```bash
adb shell 'settings put global http_proxy 192.168.99.1:8898'
# 或直接让 api_base 指向 192.168.99.1:8898（本次 enrollment 用的是这种方式）
```

mock 会拦截 `/api/channel/app/*`：`wechat-identity/init`、`heartbeat`、`wechat-contacts/digest|sync`、`messages`、`system-control/commands|receipts`，返回 `{code:200,msg:"ok",data:{...}}` envelope。

## 5. 验证结果（m6 实测）

```bash
# 1) lmkd / 设备数
adb shell getprop init.svc.lmkd        # running
adb shell dumpsys media.camera | grep 'Number of camera devices'   # 2

# 2) Probe 在微信进程
adb shell logcat -d -v brief | grep 'Probe booted for com.tencent.mm'
# I/SalesPartner-HookRuntime(...): [WorkPhoneReceiveProbe] Probe booted for com.tencent.mm

# 3) Binder / 身份 / 心跳
adb shell logcat -d -v brief | grep -E 'probe_hello_connected|heartbeat.*ack|wechat_account_state'
# probe_hello_connected probe_version=1.11.3
# wechat_account_state account_init=true account_id_sha256_12=fa318c4fe26c ...
# heartbeat ack commands_enabled=false recovered_failures=0 inbound_pending=0
```

Agent UI（截图 `screenshots/rt-agent-online.png`）：WorkPhone 标题右侧 **在线**，ROM `V14.0.9.0.TMCCNXM`，Agent `1.15.1 (2000029)`，Probe `1.11.3 (2000026)`，服务器 **已连接**，网络 **在线**。

## 6. 镜像与回滚

- 固化镜像：`docker commit m6 g98/redroid-panthor-rt-rt1:13`
- 原始镜像仍保留：`g98/redroid-panthor-rt:13`
- 替换回原版：
  ```bash
  adb shell 'cp /data/local/tmp/framework.jar.bak /system/framework/framework.jar'
  adb shell 'cp /data/local/tmp/boot-framework.vdex.bak /system/framework/boot-framework.vdex'
  adb shell 'rm -f /system/lib64/libsalespartner_rt.so /system/product/app/WorkPhoneWechatProbe/WorkPhoneWechatProbe.apk /system/product/app/WorkPhoneAgent/WorkPhoneAgent.apk'
  adb shell 'cp /system/bin/lmkd.real /system/bin/lmkd'   # 如果保留了备份
  adb reboot
  ```

## 7. 已知限制

1. **测试签名**：Probe/Agent 用 WorkPhone 自签 key，不是官方 ROM_RELEASE 证书；生产需按官方 `sign-rom-probe.sh` / `sign-rom-agent.sh` 重签，并同步更新 Agent 的签名白名单。
2. **微信账号是合成的**：本次为验证 Binder/Channel，在 `com.tencent.mm_preferences.xml` 写入了 `login_weixin_username=wp_probe_test` 等字段；真实收发消息必须用真实微信登录。原文件备份在 m6 `/data/local/tmp/wx_prefs.bak`。
3. **语音/文件能力**：Probe 对 8.0.80/3220 报 `probe_state=degraded_transient reason=wechat_8066_voice_path_boundary_unavailable`，文本/联系人链路可用；语音转写、文件传输尚未验证。
4. **Channel 命令**：mock 的 heartbeat 返回 `commands_enabled=false`，只验证了身份、心跳和在线；真实服务端命令（发消息、加好友、通讯录同步）需要接真实 workphone-channel 租户和设备凭证。
5. **lmkd**：镜像里的 fake lmkd 是让 ActivityManager 不阻塞的兼容层；如果替换成 stock lmkd，App 启动会全部卡住（这是 redroid 环境问题，不是 RT 补丁）。
6. **/data 不在镜像里**：docker commit 只固化 `/system`、`/vendor`；Agent 的 APK 已在 `/system/product/app` 预装，但 enrollment prefs/device_id 需要用 `rt-provision.sh` 在每台容器首次启动后生成。
7. **内存**：6 台容器已经用掉约 14GB/15GB；RT 增加一个微信进程和 Agent，建议一台一台灰度，不要同时全开。
8. **Camera 与 RT 同镜像**：本镜像基于 m6 的 cam15（Camera2 预览+拍照），相机设备数 2；如果只想要 RT，不需要改相机 HAL。

## 8. 关键文件（仓库）

- `rt/fake_lmkd.c`：fake lmkd 源码
- `rt/rt-provision.sh`：Agent enrollment 初始化脚本
- `rt/win_mock_channel.py`：本地 mock Channel（含 gzip/HTTP CONNECT）
- `rt/build-librt.sh`：librt 交叉编译示例
- `rt/build-runtime.sh`：runtime classes5.dex 构建示例

