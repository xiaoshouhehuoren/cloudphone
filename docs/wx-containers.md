# wx1–wx5：五台纯微信容器

> 2026-11-22：在 m7 之外新增 4 台只跑微信的 Android 13 容器（2026-11-23 补第 5 台）。每台独立 `/data`、独立
> serial / MAC / Android ID、独立代理端口；镜像复用 m7 的 `13-mt6983-camfix-v5`。
> 这 5 台不挂相机，不启 riskcheck 伪装，只保留真机属性。
>
> **2026-11-23 更新：** m7 已停用；wx1–wx5 已切到微信 **8.0.79 / versionCode 3200**（RT Probe 钉死版本），5 台运行中。
>
> **2026-11-23 代理状态：** 五台全局 `http_proxy` 已清空为 `:0`，走 Docker 直连；`18082–18086` 端口保留，等上游代理服务接入后再启用。

## 1. 规格

| 项 | 值 |
|---|---|
| 镜像 | `g98/redroid-panthor-rt:13-mt6983-camfix-v5`（`d952d822fec3`，1.84GB） |
| 容器 | `wx1`–`wx5` |
| Android | 13；五台均 `fuxi-user`（Xiaomi 13）；`ro.hardware=mt6983` |
| 数据 | `/srv/redroid/wx1`–`/srv/redroid/wx5` |
| 设备 | 仅 `/dev/dri`；无 `/dev/video*` |
| 网络 | bridge；无 published ports；默认路由 `172.17.0.1` |
| 代理 | 当前直连（`http_proxy=:0`）；wx1 18082 / wx2 18083 / wx3 18084 / wx4 18085 / wx5 18086 端口保留 |
| 应用 | `com.tencent.mm` 8.0.79（versionCode 3200，targetSdk 34） |

## 2. 身份

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
> 底层 `ro.hardware` / `ro.boot.hardware` 仍是红roid 的 `mt6983`，不是真机 Xiaomi 13 的 qcom；这是镜像底层，不能完全等同真机。

Android ID 由 `wx-start.sh` 在 boot 完成后执行 `settings put secure android_id` 写入；
即使删容器重建也会回到表里的值。不要清 `/srv/redroid/wxN`（应用数据仍会丢）。

## 3. 启动

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
6. 设 `device_name=WXN`、关闭 `adb_enabled`、启动微信 LauncherUI。

## 4. 验证

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

## 5. 登录

微信首次登录需要手机号/短信或扫码，无法脚本代填。当前 5 台都停在
`WelcomeActivity`，点 **Log In** 后按正常流程操作即可；登录态落在各自的
`/srv/redroid/wxN`，重启容器不会掉。

## 6. 已知限制

- **公网 IP 仍相同**：5 台走同一条宽带出口；端口不同只区分
  容器身份，不改变公网 IP。要一容器一公网 IP，需要给 `g98-proxy.py` 增加
  `upstream`（住宅代理/VPS/4G），当前尚未接。
- **全局代理不是透明代理**：遵守 Android `ProxySelector` 的请求会走代理；
  native 直连/自建网络栈可能绕过。微信目前能到登录页，代理日志有
  `weixin.qq.com:443`；若后续某个功能提示网络异常，需要上 redsocks/iptables
  做容器级透明代理。
- **内存**：m7 已停用；五台 1080p 微信当前约 14GiB used / `available` 约 1.0GiB（含 swap 4.3GiB）；第 6 台会超出安全余量，不建议再加。
- **无相机**：wx1–wx5 没有 `/dev/video*`，Camera2 会报 0 路；这是纯微信容器的预期行为。
- **微信版本**：当前 8.0.79/3200；APK 为 `/srv/redroid/apk/wechat879.apk`。
  8.0.80/3220 不在 RT Probe 支持列表；只有 8.0.80/3200 才是支持版本。升级 APK 时替换该文件，
  删除对应容器后重启服务（或手动 `pm install`）。
