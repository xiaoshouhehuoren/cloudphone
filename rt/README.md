# RT 构建与部署辅助脚本

配合 [docs/rt-implementation.md](../docs/rt-implementation.md) 使用。

| 文件 | 用途 |
| --- | --- |
| `fake_lmkd.c` | redroid 的 lmkd 替身：只 accept/read，保证 system_server 的 LmkdConnection 不阻塞 |
| `rt-provision.sh` | 预装 Agent 后写 enrollment prefs 并启动 WorkPhoneAgentService |
| `win_mock_channel.py` | Windows 侧 mock Channel（HTTP 代理 + /api/channel/app/* 最小响应） |
| `build-librt.sh` | 用 NDK r26d 编译 LSPlant + dex_builder 的 `librt.so` |
| `build-runtime.sh` | 编译 SalesPartnerHookRuntime + xposed_api 为 `classes5.dex` |

注意：`build-librt.sh` 不要加 `-I third_party/cxxshim`，否则 `std::same_as` 与 libc++ 冲突。
