# tests/ — 端到端验证

板子侧脚本（python3，只用标准库；在板子上以 root 运行）：

| 文件 | 作用 |
| --- | --- |
| verify_camera_e2e.py | 单实例/多实例摄像头全链路验收：节点字符设备、节点属性、devices=2、注入纯红图后中心像素、watchdog 计数、Device 0 是否打开 |
| seed_camera_prefs.py | 把已完成引导的 Camera2 偏好复制到新实例（首次开相机否则会崩在 Remember photo locations 引导） |

## 用法

```bash
python3 /root/verify_camera_e2e.py            # 默认 wx1..wx5
python3 /root/verify_camera_e2e.py wx3        # 只验一台
python3 /root/seed_camera_prefs.py wx4        # 新实例首次开相机前先跑
```

Windows 侧的封装：`run_verify_all.sh`（全量）、`run_verify.sh`（单台）。

## 判定标准

- 每台 `Number of camera devices: 2`、`vendor.camera.redroid.nodes` 与拓扑表一致
- 注入纯红 PNG 后 1–3 秒内 state=streaming，中心像素 (253,0,0)±14
- `CameraServiceWatchdog triggering abort` 计数为 0
- `Device 0 is open`
- 每台最多两轮：首轮只拉起 App，像素不符时 hard_reset（force-stop → 重启 provider → 重启 cameraserver）后重试

## 已知坑

- 相机会话跨 provider 重启后会冻结在最后 dequeue 的帧（常见是 IDLE testsrc2 的蓝条 (52,52,203)），不是注入失败；用 consumer 直接读节点可区分。
- dashboard 的相机操作是串行的，`/api/cam/start` 可能返回 `busy:true`，验证脚本需重试。
