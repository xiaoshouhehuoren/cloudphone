# 32 位 SoftAVCEnc：NV12 字节流输入修复

微信人脸验证用 `MediaCodec` + `COLOR_FormatYUV420SemiPlanar (NV12, 21)` 编码 448x640
视频。redroid 只有 32 位 `OMX.google.h264.encoder`，原厂 `SoftAVCEnc.cpp` 把 NV12 配成
`IV_YUV_420SP_UV` 后，第一次 `ive_api_function`（生成 SPS/PPS）就失败：
`SoftAVCEnc: Encode Frame failed = 0x19d` / `ACodec: ERROR(0x80001001)`，
`MPEG4Writer: Stop() called but track is not started or stopped`，最终
`face_detect/fdv_*.mp4` 只有 262B，微信在本地判 **Network error**，网络上没有上传请求。

## 补丁（SoftAVCEnc.cpp / SoftAVCEnc.h 为完整修改后文件）

- 仅对 **bytebuffer NV12** 生效：initEncoder 里仍按 I420 (`IV_YUV_420P`) 初始化 libavc，
  `mNv12Convert = !mInputDataIsMeta`；surface/meta 输入本来就被
  `extractGraphicBuffer()` 转成 I420，不走该分支。
- setEncodeArgs 里从转换池取 buffer：Y 平面 memcpy，UV 交错拆成 U/V 两个平面，
  `mNv12Origin[slot]` 记录原始输入指针。
- 编码完成释放时必须先用 `s_encode_op.s_inp_buf.apv_bufs[0]` 映射回 `mNv12Origin`，
  归还池槽位，再用原始指针匹配 `mInputBufferInfo` 调 `notifyEmptyBufferDone`，
  否则输入 buffer 永不归还、编码器卡死。

## 构建（Windows + NDK r26d + AOSP 13 源码）

```bash
bash cloudphone/tools/soft-avc32/build_soft_avc32.sh   # G98_ROOT 可覆盖 checkout 路径
```

依赖：`aosp/external_libavc13`（tag android-13.0.0_r83）、`aosp/a13_platform_frameworks_av`、
frameworks_native/system_core/system_libbase/system_logging/external_libcxx，以及
`linklibs32/` 下从设备拉取的 32 位库。要点：

- `-std=gnu++17`（不是 c++17，SoftOMXComponent.h 用 `typeof`）。
- 链接 `libstagefright_softomx.so`（不是 libstagefright_omx.so），否则缺
  `_ZTVN7android22SimpleSoftOMXComponentE`。
- 必须 `-fno-rtti -fno-exceptions`，否则缺
  `_ZTIN7android28SoftVideoEncoderOMXComponentE`。

## 部署 / 验证 / 回滚

```bash
docker cp libstagefright_soft_avcenc.so wx1:/vendor/lib/libstagefright_soft_avcenc.so
docker exec wx1 chmod 644 /vendor/lib/libstagefright_soft_avcenc.so
docker exec wx1 sh -c 'kill $(pidof media.codec)'   # ctl.restart media.codec 会被拒绝
python3 cloudphone/tests/verify_nv12_encoder.py wx1  # 板子上为 /root/tests/
```

原厂库备份在设备 `/vendor/lib/libstagefright_soft_avcenc.so.bak_g98`。
`tests/EncProbe.java` 是探针源码，`tests/encprobe.dex` 是编译产物（javac + d8 --min-api 33）。
