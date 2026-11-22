# 每容器独立代理（m7 当前方案）

> 2026-11-22：m7 已通过独立代理出口访问互联网，浦发 App 8.2.7 能进入首页/登录页。
> 每个容器一个代理端口；不要用源 IP 区分容器，Docker NAT 会把容器流量 SNAT 成板子地址。

## 1. 拓扑

```
Android App
  │  Settings.Global.http_proxy = 172.17.0.1:18081
  ▼
m7 容器 eth0 172.17.0.3
  │  default via 172.17.0.1
  ▼
板子 docker0 172.17.0.1:18081
  │  g98-proxy-relay.service
  │  socat TCP-LISTEN:18081,bind=172.17.0.1 -> 127.0.0.1:18081
  ▼
板子 127.0.0.1:18081
  │  sshd remote forward（由 Windows 侧 ssh -R 建立）
  ▼
Windows 127.0.0.1:18081
  │  ssh 客户端转发到 192.168.99.1:18081
  ▼
Windows g98-proxy.py（每容器独立端口）
  │  Windows 默认网关 192.168.6.1
  ▼
互联网
```

为什么走反向 SSH：Windows 防火墙会丢弃板子/容器发往 Windows 的入站 TCP，直连
`192.168.99.1:18081` 会超时。Windows 能主动 SSH 到板子（22 放行），所以用
`ssh -R` 把 Windows 的代理端口挂到板子的 127.0.0.1，再由板子的 socat 暴露到 docker0。

## 2. 组件

| 位置 | 文件/服务 | 作用 |
|---|---|---|
| Windows | `cloudphone/tools/g98-proxy.py` | HTTP/HTTPS CONNECT 代理管理器，每容器一个监听端口 |
| Windows | `cloudphone/tools/g98-proxy.json` | 容器→端口配置，当前 `m7:18081` |
| Windows | `cloudphone/tools/g98-proxy-supervisor.py` | 常驻拉起代理，异常退出后 5 秒重试 |
| Windows | `cloudphone/tools/g98-proxyctl.py` | 运维 CLI：status/add/remove/check/reload |
| Windows | `_tunnel.cmd` | 循环执行 `ssh -R 18081:192.168.99.1:18081`（含 askpass 环境） |
| Windows | `cloudphone/tools/G98Proxy.vbs` | 隐藏启动 supervisor + `_tunnel.cmd`；可放 Startup |
| 板子 | `/srv/redroid/container-proxy.sh` | 给容器加默认路由、设置 Android 全局代理、校验 TCP |
| 板子 | `/srv/redroid/proxies.conf` | 每容器端口表，当前 `m7 18081` |
| 板子 | `/srv/redroid/apply-proxies.sh` | 按 conf 批量重放 |
| 板子 | `g98-proxy-relay.service` | socat: `172.17.0.1:18081 -> 127.0.0.1:18081` |
| 板子 | `m7-start.sh` | 启动 m7 后调用 `container-proxy.sh m7 18081 172.17.0.1` |
| 板子 | `m7-watch.sh` | 每 60 秒检查默认路由/全局代理，缺失时修复 |

代理控制 API 只绑 `127.0.0.1:18080`，不对外；代理监听绑 `192.168.99.1:端口`。

## 3. 当前配置

```json
{
  "bind_host": "192.168.99.1",
  "control_host": "127.0.0.1",
  "control_port": 18080,
  "containers": {
    "m7": { "port": 18081, "enabled": true }
  }
}
```

m7 内：

```bash
ip route
# default via 172.17.0.1 dev eth0
# 172.17.0.0/16 dev eth0 ...

settings get global http_proxy
# 172.17.0.1:18081
```

## 4. 启动与自启

### Windows

正常桌面会话中运行：

```powershell
powershell -ExecutionPolicy Bypass -File cloudphone/tools/g98-proxy-register.ps1
# 代理/隧道已经在跑，只想安装自启：加 -NoStart
powershell -ExecutionPolicy Bypass -File cloudphone/tools/g98-proxy-register.ps1 -NoStart
```

它会把 `G98Proxy.vbs` 复制到当前用户 Startup，并立即启动代理和隧道；
加 `-NoStart` 时只安装，不重复启动。
DSH 的 workspace-write 沙箱不允许写工作区外，所以在本 agent 里执行会被
`POLICY_DENIED`；这一步需要用户手工运行，或给 DSH 一次 danger-full-access。

等价的手工操作：

1. 把 `cloudphone/tools/G98Proxy.vbs` 复制到
   `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`。
2. 下次登录会自动启动；也可以双击一次 `G98Proxy.vbs` 立即启动。

如果只想临时启动：

```powershell
pythonw cloudphone/tools/g98-proxy-supervisor.py
cmd /c _tunnel.cmd
```

### 板子

```bash
systemctl enable --now g98-proxy-relay.service
systemctl status g98-proxy-relay.service
ss -ltnp | grep 18081
# 127.0.0.1:18081  sshd-session
# 172.17.0.1:18081 socat
```

m7 由 `g98-m7.service` 启动，启动脚本已自动调用 `container-proxy.sh`；
`g98-m7-watch.timer` 每 60 秒自愈默认路由和全局代理。

## 5. 新增容器

假设新容器叫 `m8`，分配端口 18082：

```powershell
# Windows
python cloudphone/tools/g98-proxyctl.py add m8 18082
python cloudphone/tools/g98-proxyctl.py check m8
```

```bash
# 板子
echo 'm8 18082' >> /srv/redroid/proxies.conf
/srv/redroid/container-proxy.sh m8 18082 172.17.0.1
# 新容器的启动脚本里加同一行；m8-watch 也可以照抄 m7-watch 的修复逻辑
```

注意：

- 端口必须唯一；`g98-proxyctl.py add` 会拒绝重复端口。
- 端口就是容器身份；日志里的 client 地址会是板子地址 `192.168.99.50`（Docker SNAT），
  不能靠 client IP 判断容器。
- 需要更强隔离时可在 add 时加 `--auth user:pass`，代理会要求
  `Proxy-Authorization: Basic ...`；不是所有 App 都支持代理认证，默认不加。

## 6. 验证

```powershell
python cloudphone/tools/g98-proxyctl.py status
python cloudphone/tools/g98-proxyctl.py check m7
```

```bash
# 板子
curl -sS -m 8 -x http://127.0.0.1:18081 http://www.baidu.com/ -o /dev/null -w '%{http_code}\n'
# 容器
docker exec m7 toybox nc -w 3 172.17.0.1 18081; echo $?
docker exec m7 settings get global http_proxy
docker exec m7 dumpsys window | grep mCurrentFocus
```

日志：

- Windows 代理：`cloudphone/tools/g98-proxy.log`（JSONL，按 container 字段区分）
- Windows supervisor：`cloudphone/tools/g98-proxy-supervisor.log`
- 板子 relay：`journalctl -u g98-proxy-relay.service`

## 7. 已知限制

- Windows 侧必须在线且隧道/代理进程在跑；Windows 重启后需要 Startup 启动它们。
  当前机器没有管理员权限，无法注册 Windows 服务，只能用当前用户 Startup。
- 反向 SSH 只绑定板子 127.0.0.1；必须靠 `g98-proxy-relay.service` 暴露到 docker0。
- Android 全局代理只覆盖遵守 `ProxySelector` 的请求；native 直连/自建网络栈可能绕过。
  m7 的 DXRisk 仍打印 `getResponseCode:-1`，但主页面/登录页已可用。
- 代理端口不要暴露到公网；当前只绑 `192.168.99.1`（Windows 内网）和
  `172.17.0.1`（docker0）。
- 若 Windows 防火墙策略变化导致直连被挡，反向 SSH 方案仍可用；若 SSH 到板子也不通，
  则需要先恢复 Windows→板子的 22 连通性。
