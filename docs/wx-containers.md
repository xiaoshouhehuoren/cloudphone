# wx1–wx5：五台纯微信容器

> 2026-11-22：在 m7 之外新增 4 台只跑微信的 Android 13 容器（2026-11-23 补第 5 台）。每台独立 `/data`、独立
> serial / MAC / Android ID、独立代理端口；镜像复用 m7 的 `13-mt6983-camfix-v5`。
> 五台已接入板级空闲相机源与常驻触摸桥；不启 riskcheck 伪装，只保留真机属性。
>
> **2026-11-23 更新：** m7 已停用；wx1–wx5 已切到微信 **8.0.79 / versionCode 3200**（RT Probe 钉死版本），5 台运行中。
>
> **2026-11-23 代理状态：** 五台全局 `http_proxy` 已清空为 `:0`，走 Docker 直连；`18082–18086` 端口保留，等上游代理服务接入后再启用。

## 1. 规格

| 项 | 值 |
|---|---|
| 镜像 | `g98/redroid-panthor-rt:13-fuxi-v3`（`823625b504d8`，1.84GB；底层 product/fingerprint 已改 fuxi） |
| 容器 | `wx1`–`wx5` |
| Android | 13；五台均 `fuxi-user`（Xiaomi 13）；`ro.hardware=mt6983` |
| 数据 | `/srv/redroid/wx1`–`/srv/redroid/wx5` |
| 设备 | 显式 `--device /dev/dri`；privileged 下可见宿主 `/dev/video10`/`11`（相机 idle 源与注入共用） |
| 网络 | bridge；无 published ports；默认路由 `172.17.0.1` |
| 代理 | 当前直连（`http_proxy=:0`）；wx1 18082 / wx2 18083 / wx3 18084 / wx4 18085 / wx5 18086 端口保留 |
| 应用 | `com.tencent.mm` 8.0.79（versionCode 3200，targetSdk 34） |
| 相机/触摸 | `g98-camidle.service` + 每容器 `g98-wx-touchd@N.service` |

## 2. 相机与触摸

### 2.1 相机

- 宿主 `g98-camidle.service` + `/root/camidle.sh` 持续给 `/dev/video10`、`/dev/video11` 推 640x480 YU12 空转底图，并用 `keepf` 锁格式；没有 producer 时相机 App 也不会报 "Can't connect to the camera"。
- `wx-start.sh` 每次启动会 `systemctl start g98-camidle`、`chmod 666 /dev/video10/11`、等 `/dev/video10` 的格式变成 `YU12:`、对 10/11 执行 `keepf`，停 `vendor.camera-provider-2-7-ext`，最后 `ctl.restart` 一次 `vendor.camera-provider-2-4-legacy`（只重启一次；不重启会拿到 stale 的 HAL 句柄导致预览黑屏）。
- 验证：`docker exec wx1 dumpsys media.camera | grep 'Number of camera devices'` 应为 1；`getprop init.svc.vendor.camera-provider-2-4-legacy` 为 running。
- 外部画面注入沿用 XSHHR 的 `/api/cam/start name=wx1 node=/dev/video10`（`console3.py`）；容器是 privileged，所以不需要额外 `--device`。
- 限制：provider 2.7-ext 在 redroid 下 SIGABRT，不使用；640x480 在 1080p 预览会拉伸。

### 2.2 触摸

- 每台容器一个 `g98-wx-touchd@N.service`，对应 `/srv/redroid/wx-touchd.py N --tcp 127.0.0.1:1810N`。
- 桥在容器内用 `inputd.jar` 常驻 `app_process`，反射 `InputManager.injectInputEvent`；不再走 `input tap`（约 140ms）。
- 控制面：Unix `/run/g98/wx-touch-N.sock`（0600），或本机 TCP `127.0.0.1:1810N`；JSON 行协议 `{"a":"t","fx":0.5,"fy":0.5}`，也支持 `d/m/u/k/raw`。
- CLI：`python3 /srv/redroid/wx-touch.py 1 tap 0.5 0.5`。
- 验证：返回 `{"ok": true, "line": "T 540 1200", ...}`；`docker exec wx1 ps -A | grep app_process` 有 inputd 进程。
- 注意：`g98-wx-touchd@N` 只 `PartOf=g98-wx@N`，不能加 `After=g98-wx@N`，否则 wx-start 里启动 touchd 会死锁。

## 3. 身份

身份表在板子 `/srv/redroid/wx-identities.conf`，仓库副本
`build/wx-identities.conf`。格式：`容器 serial mac proxy android_id`。

| 容器 | ro.serialno / ro.boot.serialno | eth0 MAC | Android ID | 代理端口 |
|---|---|---|---|---|
| wx1 | `7F2A9C4E1B` | `02:42:ac:11:00:21` | `d8e4c641512cbb3a` | 18082 |
| wx2 | `{{SERIAL_9pkdc4mx}}` | `02:42:ac:11:00:11` | `1a9f6da191432950` | 18083 |
| wx3 | `{{SERIAL_5v7nq2ra}}` | `02:42:ac:11:00:02` | `b8972b504eaf8938` | 18084 |
| wx4 | `{{SERIAL_k3m8wt6x}}` | `02:42:ac:11:00:01` | `ffefed471ece5050` | 18085 |
| wx5 | `G98WX5B7D2E1` | `02:5a:5b:5c:5d:5e` | `5a5b5c5d5e5f6071` | 18086 |

> **统一显示 profile（2026-11-23）：** 五台均 Xiaomi 13（`fuxi` / `2211133C`）+ `1080x2400 @ 440dpi / 30fps`。wx5 先改，随后 wx1–wx4 依次重建；数据 `/srv/redroid/wxN` 均保留，删容器重建不丢。
> **底层属性（2026-11-23）：** v3 镜像已把 system/vendor/odm/product/system_ext/vendor_dlkm 的 `ro.product.*` 和分区 fingerprint 全部改成 Xiaomi/fuxi/2211133C；`ro.board.platform=kalama`。`ro.hardware` / `ro.boot.hardware` 仍保留实际 `mt6983`：把两者改成 `qcom` 的测试镜像会让 SurfaceFlinger/HWC 反复崩溃，无法开机，因此这是硬限制。

Android ID 由 `wx-start.sh` 在 boot 完成后执行 `settings put secure android_id` 写入；
即使删容器重建也会回到表里的值。不要清 `/srv/redroid/wxN`（应用数据仍会丢）。

## 4. 启动

```bash
# 单台
systemctl start g98-wx@1.service

# 五台
for n in 1 2 3 4 5; do systemctl start g98-wx@$n.service; done

# 状态
systemctl is-active g98-wx@{1,2,3,4,5}.service
docker ps --filter name=wx --format '{{.Names}} {{.Status}}'
```

`g98-wx@.service` 是 oneshot + RemainAfterExit；`ExecStart` 调用
`/srv/redroid/wx-start.sh %i`，`ExecStop=docker rm -f wx%i`。
stop 直接删容器而不是 `docker stop`，避免 Android init 不响应 SIGTERM 导致
systemd stop 超时和 start/stop 竞态；数据在 bind mount 里，删容器不丢。

启动脚本做这些事：

1. 读 `wx-identities.conf` 取 serial / MAC / proxy；
2. 容器不存在则 `docker run`，存在但停止则 `docker start`，start 失败则删掉重建；
3. 轮询 `sys.boot_completed=1`（最多 180 秒）；
4. 没装微信则 `pm install -r -g wechat879.apk`（8.0.79 / 3200）；
5. 设默认路由并清空 `http_proxy`，走直连；代理端口保留，后续接上游时再启用；
6. 设 `device_name=WXN`、关闭 `adb_enabled`；
7. 起 `g98-camidle.service`，等 YU12 + `keepf` 后重启一次 2.4 camera provider；
8. 起 `g98-wx-touchd@N.service`，建立触摸桥；
9. 启动微信 LauncherUI。

## 5. 验证

```bash
for n in 1 2 3 4 5; do
  echo "--- wx$n ---"
  docker exec wx$n getprop ro.serialno
  docker exec wx$n settings get secure android_id
  docker exec wx$n ip link show eth0 | awk '/ether/{print $2}'
  docker exec wx$n settings get global http_proxy
  docker exec wx$n dumpsys window | grep mCurrentFocus | head -1
done
```

预期：serial/MAC/android_id 与上表一致；`http_proxy=:0`；
前台是 `com.tencent.mm/.plugin.account.ui.WelcomeActivity`。

直连连通性：

```bash
docker exec wx1 toybox nc -w 3 www.baidu.com 80; echo $?
```

当前 18082–18086 的 relay 全部停用，容器 `http_proxy=:0`；如需重新启用代理，先恢复 Windows 代理、隧道和板子 relay。

## 6. 登录

微信首次登录需要手机号/短信或扫码，无法脚本代填。当前 5 台都停在
`WelcomeActivity`，点 **Log In** 后按正常流程操作即可；登录态落在各自的
`/srv/redroid/wxN`，重启容器不会掉。

## 7. 已知限制

- **公网 IP 仍相同**：5 台走同一条宽带出口；端口不同只区分
  容器身份，不改变公网 IP。要一容器一公网 IP，需要给 `g98-proxy.py` 增加
  `upstream`（住宅代理/VPS/4G），当前尚未接。
- **全局代理不是透明代理**：遵守 Android `ProxySelector` 的请求会走代理；
  native 直连/自建网络栈可能绕过。微信目前能到登录页，代理日志有
  `weixin.qq.com:443`；若后续某个功能提示网络异常，需要上 redsocks/iptables
  做容器级透明代理。
- **内存**：m7 已停用；五台 1080p 微信当前约 14GiB used / `available` 约 1.0GiB（含 swap 4.3GiB）；第 6 台会超出安全余量，不建议再加。
- **相机/触摸依赖宿主组件**：`g98-camidle.service`、`/root/keepf`、`inputd.jar` 或 `/dev/video*` 缺失时，wx 容器仍能启动微信，但相机/触摸不可用；wx-start 对这两步失败不阻断。
- **微信版本**：当前 8.0.79/3200；APK 为 `/srv/redroid/apk/wechat879.apk`。
  8.0.80/3220 不在 RT Probe 支持列表；只有 8.0.80/3200 才是支持版本。升级 APK 时替换该文件，
  删除对应容器后重启服务（或手动 `pm install`）。
