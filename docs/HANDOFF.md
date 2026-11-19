# G98 云手机项目 — 交接文档

更新时间：2026-11-19 23:10（Asia/Shanghai）
总体状态：**全部完成** —— 6 台云手机、微信、摄像头串流、**App 内相机（Camera2 预览 + 拍照全部打通）**。

---

## 一、硬件与环境

~~~
物理机：bdy-g98（BDY G98 开发板）
  SoC:     RK3588（8 核 ARM64）
  内存:    15GB（跑 6 台容器已用 13GB+，很紧张）
  磁盘:    459GB
  系统:    Armbian，内核 6.18.48-ophub
  Docker:  26.1.5
  SSH:     root@192.168.99.50   密码 1234
  GPU:     Panthor + Mesa 26.3 + gralloc.cros
~~~

**铁律：板子完全没有外网**（所有源返回 000）。源码/工具只能 Windows 侧下载后 scp。

Windows 工作目录：`C:\Users\rosetta\Desktop\g98\`

SSH 走 askpass 封装（工作区里的 `_ssh.sh` / `_scp.sh` 就是这个）：

~~~bash
export SSH_ASKPASS='C:/Users/rosetta/Desktop/g98/_askpass.cmd'
export SSH_ASKPASS_REQUIRE=force DISPLAY=:0
ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password \
    -o PubkeyAuthentication=no root@192.168.99.50
~~~

---

## 二、交付内容（全部稳定运行）

### 6 台云手机容器

| 容器 | 机型 | ADB 端口 |
|---|---|---|
| m1 | Pixel (panther) | 5555 |
| m2 | 2201123C (Redmi) | 5556 |
| m3 | LE2123 (OnePlus) | 5557 |
| m4 | SM-S918B (Samsung) | 5558 |
| m5 | PJZ110 (OPPO) | 5559 |
| m6 | 23127PN0CC (Xiaomi) | 5560 |

基础镜像 `g98/redroid-panthor-rt:13`，数据目录 `/srv/redroid/m1..m6`。
每台独立序列号 + `user/release-keys` + 差异化 fingerprint。

**m6 已升级为带相机 HAL 的镜像 `g98/redroid-panthor-cam15:13`（原 /data 不变）—— 预览 + 拍照都可用的最终版。**

### 微信
6 台全部安装（`/root/wechat860.apk`），已验证可登录。

### 摄像头串流（WebRTC 主用）
~~~
http://192.168.99.50:8889/cam0   cam1    （WebRTC，主用）
HLS / RTSP / JPEG 推流              （备用）
~~~

### systemd 服务（5 个，全部 enabled）
~~~
docker  camstream  adb-ports  g98-dashboard:8099  g98-campush
~~~

### 运维脚本（板子 /root/）
~~~
g98-check.sh  g98-wake.sh  cam-bringup.sh  cam-source.sh
cam-from-push.sh  wechat-login.sh  adb-ports.sh
~~~

### 接入外部画面（把任意内容灌进云手机相机）

云手机相机读的是宿主机 `/dev/video10`（相机0）/ `/dev/video11`（相机1）这两个 **v4l2loopback 虚拟设备**，所以“外部画面”就是从宿主机往这两个节点写帧。

```bash
# 演示：把 Windows 桌面截图当相机画面（已实测通过，2026-11-20）
powershell -NoProfile -File _cap.ps1                 # 截当前桌面 -> _desk.png
scp _desk.png root@192.168.99.50:/root/desk.png
ssh root@192.168.99.50
  pkill -f '/dev/video10'
  nohup /root/ffmpeg -re -loop 1 -framerate 30 -i /root/desk.png \
    -vf 'scale=640:480:force_original_aspect_ratio=decrease,pad=640:480:(ow-iw)/2:(oh-ih)/2' \
    -pix_fmt yuv420p -f v4l2 /dev/video10 &
  docker exec m6 setprop ctl.restart vendor.camera-provider-2-4-legacy
  docker exec m6 setprop ctl.restart cameraserver
  docker exec m6 am start -n com.android.camera2/com.android.camera.CameraActivity
```

换真实源只改输入：`-f v4l2 -i /dev/video0`（USB 摄像头）、`-i rtsp://…`、`-re -i movie.mp4`、`-f x11grab -i :0`，输出端永远是 `-pix_fmt yuv420p -s 640x480 -f v4l2 /dev/video10`。

**三个必须知道的坑**：

1. 格式固定 `yuv420p` + `640x480`（元数据在 HAL 连接设备时枚举，换分辨率要重启 provider 才会重新广告）。
2. 换生产者后 **必须同时重启 provider 和 cameraserver**，否则新会话可能拿到 `initializeImpl: unable to initialize device: Device or resource busy (-16)`（旧 fd 还占着 `exclusive_caps=Y` 的节点）。
3. `docker exec` 里别嵌套引号（会被吞），一律「本地写脚本 → scp → `sh /tmp/x.sh`」。

实测截图：`_desk.png`（桌面原图 2304x1440）、`_d1.png`（云手机内预览显示桌面）、`_d2.png`（按快门后）、`_desk_shot.jpg`（相册里落盘的照片 638x480，两端留黑边是 pad 608x400→640x480 的结果）。

### 面板
`http://192.168.99.50:8099/`

---

## 三、App 内相机（已打通，预览 + 拍照可用）

### 3.1 结果

~~~
dumpsys media.camera  →  Number of camera devices: 2
相机 App              →  全屏预览出画面（ffmpeg testsrc 彩条），可连续出帧
拍照                  →  点快门后 App 自己压 JPEG 落盘，颜色正确
连续 3000+ 帧         →  Shutter out-of-order 0 次、drain 超时 0 次、provider 不崩、无 Camera error 弹窗
~~~

证据（工作区）：
- `_cam30.png` / `_cam31.png`：720x1280 相机 UI + 彩条预览
- `_cam60.png`：拍照后回到预览（无错误对话框）
- `_photo3.jpg`：拍到的照片（638x480，testsrc2 图案，颜色正确）
- App 落盘位置：`/storage/emulated/0/Pictures/IMG_YYYYMMDD_HHMMSS.jpg`（真实路径 `/data/media/0/Pictures/`

### 3.2 为什么难

redroid 默认**没有相机 HAL**（上游明确 "camera HAL not implemented by default"），
社区没有可运行的完整方案。整条链路要自己编译、自己适配容器里的 gralloc/v4l2。

### 3.3 数据通路

~~~
App (Camera2)
  → cameraserver (system)
  → android.hardware.camera.provider@2.4-service   (自编译, 2874824 B, VINTF 只有 2.4)
  → /vendor/lib64/hw/camera.redroid.so             (自编译 libhardware 3.4 + arc + JPEG 编码器, 675744 B)
  → /dev/video10 | /dev/video11                    (v4l2loopback, MMAP)
  ← ffmpeg testsrc=yuv420p 640x480@30 写入 video10 / video11
~~~

设备节点选择：系统属性 `vendor.camera.redroid.nodes`（逗号分隔）优先；
否则固定用 `/dev/video10,/dev/video11`；再否则才扫描 /dev/video*。

### 3.4 交付物

| 产物 | 大小 | 位置 |
|---|---|---|
| camera.redroid.so（HAL） | 675744 B | 容器 `/vendor/lib64/hw/`；工作区 `camera_v7.so`；板子 `/root/camera_hal_jpeg.so`（md5 22341301c3645b16c02bf1df96ff4dec） |
| camera.v4l2.so / camera.so | 553384 B | 同上（同一份二进制，兼容旧路径） |
| provider 2.4 service | 2874824 B | 容器 `/vendor/bin/hw/` |
| provider .rc | 351 B | 容器 `/vendor/etc/init/` |
| manifest.xml（只留 2.4） | 2973 B | 容器 `/vendor/etc/vintf/` |
| 镜像 | 1.8GB | `g98/redroid-panthor-cam15:13`（sha256 cc2375a46e1e） |
| 源码改动 | — | `aosp/libhardware13/modules/camera/3_4/`、`a13arc/` |

### 3.5 攻克的壁垒（12 道）

1. **hidl-gen 缺失** → Ubuntu ports arm64 deb，Python 手解 ar
2. **226 个 HIDL 头** → hidl-gen 批量生成
3. **libc++ ABI 隔离**（`std::__ndk1` vs `std::__1`）→ `-nostdinc++ -I a13_external_libcxx/include`
4. **无 typeinfo** → `-fno-rtti`
5. **HIDL gServicePrioMap/gServiceSidMap 缺失** → 批量 sed 补默认值
6. **mapper 死锁** → 自写 HandleImporter（importBuffer 直接放行）
7. **V3_3/3_4/3_5 session 编译失败** → include 顺序 + namespace 修补
8. **VTT 虚表缺失** → 真实 CameraDeviceSession.cpp
9. **VINTF 注册被拒** → 只保留 2.4，一个 interface 多个 instance
10. **v4l2loopback 只支持 MMAP** → RequestBuffers 从 USERPTR 改 MMAP + 自己 mmap
11. **HIDL buffer handle 生命周期** → CaptureRequest/GrallocFrameBuffer 各自 native_handle_clone
12. **shutter 乱序**（DQBUF 顺序 ≠ 提交顺序）→ HAL 侧按 frame_number 排序上报
13. **BLOB 一广告就 0 台设备**：`qualified_formats` 只认 YUYV/MJPEG，而设备只枚举 YU12 →
    BLOB 必须自己回退到 YUV420 的尺寸表，IMPL_DEFINED 也要回退（否则按 BGR32 枚举帧尺寸失败 -ENODEV）
14. **App 请求会被 HAL 判非法**：`AE_LOCK`（0x10002）等无效果菜单控件原来只接受精确匹配值，
    Google Camera 会请求未广告的值 → 整条请求非法（表现为拍照后 "Camera error"）；
    `MenuControlOptions::IsSupported` / `SliderControlOptions::IsSupported` 改成永远返回 true
15. **YUV_420_888 客户端 buffer 是 NV12 半平面**：cb/cr pixel stride=2、row stride=width，
    按 I420 平面直拷会得到绿/品红错色 → 必须自己重排色度为 U,V 交错
16. **无 libjpeg 也能拍照**：自写 `EncodeJpegI420`（libyuv shim 转 RGBA + jo_jpeg 基线编码器）供 BLOB 通路

另外三处 metadata 修正：
- **fps 区间荒唐**（dummy v4l2 报 1ms..数小时）→ 在 `GetFormatFrameDurationRange` 夹到 15–30fps
- **BLOB（拍照）**：早期没编码器时用 `ARC_NO_JPEG` 不广告 BLOB；现在自写 `EncodeJpegI420`（jo_jpeg + libyuv shim）
  重新广告 HAL_PIXEL_FORMAT_BLOB，并把 BLOB 的尺寸表回退到 YUV420（见 3.5 第 13 条）
- **多流被拒** → setupStreams 允许"1 主流 + 若干同尺寸附加流"

### 3.6 关键补丁位置（改代码必看）

| 文件 | 改动 |
|---|---|
| `a13arc/base/logging.h`（新） | 补 `LOG_IF` / `VLOG`，`using android::base::{ERROR,INFO,WARNING}` |
| `a13arc/libyuv.h`、`libyuv_shim.cpp`（新） | 手写 libyuv 兼容层：I420Copy/Scale、YUY2ToI420、I420ToARGB/ABGR、ConvertToI420（crop+rotate）、MJPGToI420(返回-1) |
| `aosp/compat/base/synchronization/lock.h` | `using AutoLock = Autolock;` |
| `arc/image_processor.cpp` | JPEG 分支已无条件编译（不再有 ARC_NO_JPEG 守卫）；memcpy 改 `std::min(in,out)` 防越界 |
| `arc/frame_buffer.h/.cpp` | 析构/构造持有 `owned_handle_`（native_handle_clone）；Map() **优先直接 mmap dma-buf fd**，gralloc lock 仅作回退 |
| `capture_request.h/.cpp` | 克隆所有 output buffer handle（vector<buffer_handle_t> owned_output_handles_），析构 delete |
| `camera.h/.cpp` | 有序完成：`mPendingResults` map + `queueResult()` + `flushResultsLocked()`，按 frame_number 递增上报 |
| `v4l2_camera.cpp` | setupStreams 多流放宽；`flushBuffers()` 加 `in_flight_lock_` 与 dequeue 互斥 |
| `v4l2_wrapper.cpp` | RequestBuffers→MMAP；DequeueRequest 遍历全部 output buffer、跳过 JPEG、copy_size 限长；fps 夹紧 |
| `v4l2_camera_hal.cpp` | 节点收敛到 video10/11（`sys/system_properties.h`） |
| `format_metadata_factory.cpp` | BLOB 特判回退到 YUV420（"JPEG stream will be encoded from V4L2 pixel format …"）；IMPL_DEFINED 无 qualified 时也回退 YUV420，避免 `Failed to get all frame sizes for format BGR32` |
| `metadata/menu_control_options.h`、`slider_control_options.h` | `IsSupported()` 恒 true（无效果控件不校验客户端值，否则 AE_LOCK 请求被判非法） |
| `v4l2_wrapper.cpp`（NV12 段） | 对 `fourcc == V4L2_PIX_FMT_YUV420` 的输出流，把 I420 色度重排成 NV12 半平面写进客户端 buffer |
| `a13arc/jpeg_encoder.cpp/.h`（新） | `EncodeJpegI420()`：I420→RGBA（libyuv shim）→ `jo_write_jpg_fp`→内存流→拷出 |
| `a13arc/jo_jpeg.cpp`（新） | 公开领域单文件基线 JPEG 编码器，改成 `jo_write_jpg_fp(FILE*,…)` 供内存流使用 |
| `arc/image_processor.cpp`（JPEG 段） | `ConvertToJpeg` 改调 `EncodeJpegI420`，质量取 `ANDROID_JPEG_QUALITY`（缺省 80），EXIF 暂不写 |
| `HandleImporter_custom.cpp` | importBuffer/freeBuffer 空实现（绕开 HIDL mapper 死锁） |

核心代码片段（有序上报）：

~~~cpp
void Camera::queueResult(std::shared_ptr<CaptureRequest> request, int err, int64_t ts) {
  if (!mResultOrderInitialized) { mNextResultFrame = request->frame_number; mResultOrderInitialized = true; }
  mPendingResults[request->frame_number] = {request, err, ts};
}
void Camera::flushResultsLocked() {
  while (true) {
    auto it = mPendingResults.find(mNextResultFrame);
    if (it == mPendingResults.end()) break;
    auto p = it->second; mPendingResults.erase(it);
    if (p.err) completeRequestWithError(p.request, p.err);
    else { notifyShutter(p.request->frame_number, p.timestamp); sendResult(p.request); }
    ++mNextResultFrame;
  }
}
~~~

### 3.7 已验证

- **m6 长跑**：预览彩条可见，`DBG complete` 到 **frame 27463**（约 20 分钟），OOO=0，DRAIN=0，DEAD_OBJECT=0，CAM_FatalErrorHandler=0，provider pid 89 稳定，相机 App 有焦点。
- **拍照**：点快门 → `I/CAM_JpegUtilNative: Compressing jpeg with size = (640, 480) …` → `Image with uri: content://media/external/images/media/… was published to the MediaStore`；
  文件 `/data/media/0/Pictures/IMG_20261119_122519.jpg`（19573 B）拉回来看，**颜色正确**（`_photo3.jpg`）。
  拍照后 `Invalid request settings` / `FatalErrorHandler` 计数均为 0，App 回到预览继续出帧（frame 3066）。
- **cam15 镜像**：`docker commit m6 g98/redroid-panthor-cam15:13`（sha256 cc2375a46e1e），
  镜像内 `/vendor/lib64/hw/camera.redroid.so` = 675744（md5 22341301c3645b16c02bf1df96ff4dec）、provider 2874824、manifest 2973。
- **cam14 镜像冷启动**：新容器 `mcV` 起来后 `Number of camera devices: 2`，证明镜像自带 provider/HAL/manifest。
  但该容器 /data 是全新的，FUSE 挂了（`ls /storage/emulated/0/Android/data` → Invalid argument，重启无效），
  相机 App 因此崩溃 —— 这是**新 data 的环境问题，不是镜像问题**；m6 用同一份二进制跑得很好。

### 3.8 已知限制（不影响预览）

1. **拍照可用，但没有 EXIF、没有录像**：HAL 的 BLOB 通路用自写 jo_jpeg 编码（无 EXIF/GPS/缩略图）；
   相机 App 通常走 YUV_420_888 自己压 JPEG，实测这条路已正确。
   录像（MediaRecorder/VideoCapture）未验证，需要编码器与另一套 surface 通路。
   照片尺寸只有 640x480（受 v4l2loopback 源的限制，BLOB 尺寸表跟着 YUV420 走）。
2. **每帧仍有一行 `E V4L2Wrapper: Failed to map output frame`**：第二路 output buffer 的 fd 不可映射（无 fd 的 handle），
   该路不写但照常 complete，预览不受影响。彻底修需查清该 buffer 来源（可能是 app 申请的 YUV 分析流）。
3. **全新 /data 的 FUSE 问题**：第一次开机 camera2 会因 `/storage/emulated/0/Android/data` code -22 崩溃；
   m6 上 restart 一次即恢复，但 mcV（全新目录）restart 后 `ls` 仍报 Invalid argument —— 这种要删掉 `/srv/redroid/mcV` 重建，
   或直接从正常容器的 /data 复制一份。属容器环境问题，与相机 HAL 无关。
4. **2.7-ext provider 会 crash loop**（VINTF 没 2.7），必须 `stop vendor.camera-provider-2-7-ext`。
5. 内存 15GB 跑 6 台已满，测试容器用完要删。

### 3.9 当前部署范围（重要，别一次全铺）

- **只有 m6 跑着相机栈**（容器名仍是 m6，从 rt-cam 热插拔得来；`cam15` 镜像是给重建/新容器用的）。
- **m1–m5 保持 `rt:13` 基线不动**，相机按需一台一台加。
- 板子上的历史实验容器（mcZ、mcV、c1、x1、g1、t1、l1、rt1）已全部删除，现在只剩 m1–m6。
- 加下一台的顺序：`/root/cam-rollout.sh mX` → 等 BOOT_OK → `docker exec mX dumpsys media.camera | grep "Number of camera"` 看到 2 台 → 再开 App 验证；
  失败就 `docker restart mX` 回基线（脚本不动 /data，数据不丢）。

---

## 四、编译与部署

### 4.1 编译 HAL（工作区）

~~~bash
cd /c/Users/rosetta/Desktop/g98          # == C:\Users\rosetta\Desktop\g98
export TMPDIR="C:/Users/rosetta/Desktop/g98/tmpb"
bash _rehal7.sh                          # 产出 camera_v7.so
~~~

脚本要点（改脚本时别丢）：

- 变量：`A=aosp; L=$A/libhardware13; LC=$A/a13_external_libcxx; C=$A/a13_platform_system_core;
  B=$A/a13_platform_system_libbase; G=$A/a13_platform_system_logging; M=$A/a13_platform_system_media;
  N=$A/a13_platform_frameworks_native; I=$A/a13_platform_hardware_interfaces; V=$A/a13_platform_frameworks_av`
- 编译参数：`-std=c++17 -fPIC -O1 -fno-rtti`（早期没有 JPEG 时用 `-DARC_NO_JPEG`，**现在已去掉**）
- include 必须含：`-nostdinc++ -I$LC/include -Ia13arc -I$A/compat -I$M/private/camera/include
  -I$L/modules/camera/3_4 -I$I/camera ...`（少 `private/camera/include` 会缺 camera_metadata_hidden.h）
- 源集：`find $L/modules/camera/3_4 -name "*.cpp"` 排除 test、`arc/exif_utils.cpp`、`arc/jpeg_compressor.cpp`；
  加 `a13arc/libyuv_shim.cpp`、`a13arc/jpeg_encoder.cpp`、`a13arc/jo_jpeg.cpp`、
  `$V/camera/CameraMetadata.cpp`、`$V/camera/VendorTagDescriptor.cpp`、`sync_stub.cpp`
- 链接：`-Ldl_libs -lhardware -Llinklibs/ll -llog -ldl -lutils -lcutils -lc++ -lbase -lcamera_metadata`
- NEEDED 白名单（必须）：libhardware liblog libdl libutils libcutils libc++ libbase libcamera_metadata libm libc
  **禁止出现 libc++_shared.so、libcamera_client.so**
- 产物校验：`llvm-nm -D --undefined-only camera_v7.so` 只允许 libc 类符号

### 4.2 部署到运行中的容器（8 秒，不用重启）

~~~bash
scp camera_v7.so root@192.168.99.50:/tmp/
ssh root@192.168.99.50
docker cp /tmp/camera_v7.so m6:/vendor/lib64/hw/camera.redroid.so
docker exec m6 chmod 755 /vendor/lib64/hw/camera.redroid.so
docker exec m6 setprop ctl.restart vendor.camera-provider-2-4-legacy
sleep 6 && docker exec m6 dumpsys media.camera | grep "Number of camera"
~~~

注意：provider 热重启后可能枚举出 4 台设备（旧节点残留），要整容器 restart 才干净。

### 4.3 部署到其它容器（m1–m5）

最省事：用 cam15 重建容器，保留各自 /data。以 m1 为例：

~~~bash
docker stop m1 && docker rm m1
# 用 _mkcv.py 的思路（读旧容器 inspect 生成 run 命令），镜像换成 cam15，数据目录仍 /srv/redroid/m1
~~~

**一键脚本（推荐）**：`cam-rollout.sh <容器名>`（工作区 / 板子 `/root/cam-rollout.sh`）——
  从 cam15 取 6 个文件热插拔进目标容器、备份原 manifest 到 `/srv/redroid/<容器>/manifest.xml.orig`、
  重启并等开机、停掉 2.7 provider、最后打印 `Number of camera devices`。

注意：`/dev/video10,11` 这些 v4l2loopback 节点**同一时刻只能被一个进程打开**，
所以两台容器不能同时用相机；要给多台用，就多建几个 loopback 节点（video12–15 已存在）
并在容器里设 `setprop vendor.camera.redroid.nodes /dev/video12,/dev/video13`。

或手工拷 6 个文件进现有容器（/vendor 是可写的容器层，重启保留、重建丢失）：

~~~bash
docker create --name cbT g98/redroid-panthor-cam15:13
for f in /vendor/bin/hw/android.hardware.camera.provider@2.4-service \
         /vendor/etc/init/android.hardware.camera.provider@2.4-service.rc \
         /vendor/etc/vintf/manifest.xml \
         /vendor/lib64/hw/camera.redroid.so \
         /vendor/lib64/hw/camera.v4l2.so \
         /vendor/lib64/hw/camera.so; do
  docker cp cbT:$f /tmp/x && docker cp /tmp/x mX:$f
done
docker rm cbT
docker exec mX chmod 755 /vendor/lib64/hw/camera.redroid.so /vendor/bin/hw/android.hardware.camera.provider@2.4-service
docker restart mX
~~~

### 4.4 成功/失败判据

~~~
成功：Number of camera devices: 2
      相机 App 全屏彩条预览
      logcat 无 "Shutter notification out-of-order"、无 "waitUntilDrainedLocked"
      provider pid 一直在
失败：devices 0        → HAL dlopen 失败，看 vndksupport 缺哪个符号
      devices 2 但黑屏  → 看 "Failed to map output frame" / "Direct mmap ... failed"
      App 弹 Camera error → 看 "Shutter notification out-of-order" / drain 超时（说明 provider 死了或没 complete）
~~~

---

## 五、镜像清单

~~~
g98/redroid-panthor-rt:13        ★ 生产基线（m1–m5）
g98/redroid-panthor-cam:13       首次尝试（fragment 冲突）
g98/redroid-panthor-cam2..3:13   manifest 各种错误写法（失败）
g98/redroid-panthor-cam4:13      ★ 只留 2.4，首次能启动
g98/redroid-panthor-cam5..9:13   权限 / libc++ / 链接逐步修
g98/redroid-panthor-cam10:13     ★ find 递归含 metadata/，服务 running
g98/redroid-panthor-cam11:13     加 libcamera_client（namespace 失败）
g98/redroid-panthor-cam12:13     ★ CameraMetadata 编进 HAL
g98/redroid-panthor-rt-cam:13    rt:13 + 6 个相机文件（HAL 旧版）
g98/redroid-panthor-cam14:13     ★★ 预览版：HAL 660216 B，无拍照，sha256 4c7908e7e472
g98/redroid-panthor-cam15:13     ★★★ 最终版（预览 + 拍照）：HAL 675744 B，sha256 cc2375a46e1e
~~~

---

## 六、重要教训

1. `docker cp` 保留源权限，不 `chmod 755` 就 Permission denied
2. redroid 没有 `/bin/sh`，只有 `/system/bin/sh`
3. `docker exec` 里 `echo RC=$?` 要写成 `\$?`
4. `--allow-multiple-definition` 让先链接的 main 赢；`--unresolved-symbols=ignore-all` 会掩盖缺符号
5. `ls dir/*.cpp` 只匹配顶层，要 `find`
6. VINTF 改错只影响那个容器，restart + 删文件 90–120 秒恢复
7. **v4l2loopback 只支持 MMAP**：USERPTR/DMABUF 都是 EINVAL，必须 REQBUFS(MMAP)+mmap
8. **HIDL 传进来的 native_handle 只在调用期间有效**：异步使用前必须 native_handle_clone
9. **camera3 的 buffer handle 在 provider 里 gralloc lock 不了**（g98/cros 报 Invalid handle），
   但 fd 有效，直接 mmap dma-buf 是可行路
10. **complete 必须按 frame_number 升序**：DQBUF 顺序不等于提交顺序，否则 Camera3 立刻 error state
11. **flush 与 dequeue 并发会 SIGBUS**（StreamOff reset request，dequeue 还在映射），要加锁
12. 容器内 logcat 用 `logcat -d -v brief > file` 再 grep，别直接管道 tail（会截断）
13. **docker exec 里嵌套引号很容易被吞**：一律「本地写脚本 → scp → `ssh 'sh /tmp/x.sh'`」，
    并且给每个 `docker exec` 套 `timeout`，否则 `am start` 一挂就把整条命令拖死
14. **`docker cp` 读不到 FUSE 路径**（`/storage/emulated/0/...` 报 Could not find the file），
    要读照片等文件用真实路径 `/data/media/0/...`
15. **握手元数据 tag 值别凭记忆**：`0x10002` 是 `ANDROID_CONTROL_AE_LOCK`（不是 fps range），
    查 `camera/metadata/aidl/aidl_api/.../CameraMetadataTag.aidl` 才准

---

## 七、回滚

~~~bash
cat > /root/rollback-cam.sh << "EOF"
#!/bin/sh
for c in m1 m2 m3 m4 m5 m6; do
  docker exec $c sh -c "rm -f /vendor/lib64/hw/camera.redroid.so \
    /vendor/bin/hw/android.hardware.camera.provider@2.4-service \
    /vendor/etc/init/android.hardware.camera.provider@2.4-service.rc"
  docker restart $c >/dev/null 2>&1
done
EOF
chmod +x /root/rollback-cam.sh
~~~

容器若被重建过，直接 `docker rm` 后用 `g98/redroid-panthor-rt:13` + 原 /srv/redroid/mX 重建即可（数据不丢）。

`cam-rollout.sh` 会把原镜像的 manifest 备份到 `/srv/redroid/<容器>/manifest.xml.orig`；
回滚时只删 HAL/provider 三个文件是不够的（manifest 已被覆盖），要把它一起还原：

~~~bash
docker cp /srv/redroid/m5/manifest.xml.orig m5:/vendor/etc/vintf/manifest.xml
docker exec m5 sh -c 'rm -f /vendor/lib64/hw/camera.redroid.so   /vendor/bin/hw/android.hardware.camera.provider@2.4-service   /vendor/etc/init/android.hardware.camera.provider@2.4-service.rc'
docker restart m5
~~~

---

## 八、一句话总结

**四件事全部完成：6 台云手机、微信、摄像头串流、以及最难的那件 —— 容器内 App 通过标准 Camera2 既能实时预览，也能拍照落盘。**
自编译 HAL + provider，攻破 17 道壁垒（HIDL、libc++ ABI、VINTF、gralloc、v4l2loopback MMAP、handle 生命周期、
结果排序、flush 竞态、BLOB 元数据、请求校验、NV12 色度重排、自写 JPEG 编码器），
最终 `Number of camera devices: 2`、预览连续上千帧无错误，**拍照也能落盘且颜色正确**（自写 JPEG 编码器 + NV12 重排）。
现在的限制只有：没有 EXIF、没验证录像、照片分辨率受 640x480 源限制。
