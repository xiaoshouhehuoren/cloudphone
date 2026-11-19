# 国家网络身份认证 App（cn.cyberIdentity.certification）

云手机 m6 内安装并启动验证记录（2026-11-20）。

## 1. 目标 App

| 项 | 值 |
| --- | --- |
| 名称 | 国家网络身份认证（数字身份 / 网号网证） |
| 包名 | `cn.cyberIdentity.certification` |
| 版本 | 1.2.41（应用宝 appId 54243550） |
| 提供方 | 中华人民共和国公安部 / 公安部第一研究所 |
| 大小 | 72,080,111 B（68.7 MB） |
| MD5 | `463F0578DC475E68F7D0A46568FE75F0` |
| 下载 | `http://imtt.dd.qq.com/sjy.00022/sjy.00004/16891/apk/463F0578DC475E68F7D0A46568FE75F0.apk?fsname=cn.cyberIdentity.certification_1.2.41.apk` |

来源页面：`https://a.app.qq.com/o/simple.jsp?pkgname=cn.cyberIdentity.certification`，APK 直链在其 `window.systemData.appDetail.apkUrl` 字段里。

下载（Windows 侧，python 走 OpenSSL 能出网，git/curl 的 schannel 不行）：

```bash
python -c "import urllib.request,hashlib;u='http://imtt.dd.qq.com/sjy.00022/sjy.00004/16891/apk/463F0578DC475E68F7D0A46568FE75F0.apk';d=urllib.request.urlopen(u).read();open('netauth.apk','wb').write(d);print(len(d),hashlib.md5(d).hexdigest().upper())"
```

## 2. 安装（已验证 `Success`）

```bash
scp netauth.apk root@192.168.99.50:/root/netauth.apk
ssh root@192.168.99.50
  docker cp /root/netauth.apk m6:/data/local/tmp/netauth.apk
  docker exec m6 pm install -r /data/local/tmp/netauth.apk   # -> Success
  docker exec m6 pm list packages | grep cyberIdentity       # -> package:cn.cyberIdentity.certification
```

## 3. 启动（已验证）

`monkey` 在 redroid 里坏了（`exec /bin/monkey: exec format error`），用 `am start` 指定 Launcher Activity：

```bash
# 先解析：cmd package resolve-activity --brief cn.cyberIdentity.certification
docker exec m6 am start -n cn.cyberIdentity.certification/cn.wh.project.view.v.SplashActivity
```

- SplashActivity → `cn.wh.project.view.v.MainTabActivity`（首页：登录 / 网号网证申领 / 电子证照领取 / 管理 / 消息中心 / 我的）。
- 首启弹「服务协议与隐私保护规则」，点“同意”进入；随后弹通知权限、再弹「设备存在安全风险」。

## 4. 网络：容器默认没有外网，需要 Windows 侧代理

板子本身没有默认路由（`ping 223.5.5.5` → Network is unreachable），容器桥接 `172.17.0.0/16` 自然也出不去，App 会提示「网络不可用，请检查网络设置」。

做法（已实测通过）：

```bash
# Windows（本机 192.168.99.1，与板子同网段）：起一个最小 CONNECT 代理
python tools/win_http_proxy.py 8899
# 云手机内设置全局代理
docker exec m6 settings put global http_proxy 192.168.99.1:8899
docker exec m6 am force-stop cn.cyberIdentity.certification
docker exec m6 am start -n cn.cyberIdentity.certification/cn.wh.project.view.v.SplashActivity
```

验证：板子上用 python3 发 `CONNECT api.github.com:443` 能收到 `HTTP/1.1 200 Connection Established`，说明代理链路通。

## 5. 已知限制：设备完整性检测

通过网络检查后，App 仍弹「温馨提示 / 设备存在安全风险」。原因：redroid 容器没有 TEE/KeyStore 硬件背书、系统被 root、机型指纹是伪造的，这类 App 会做 root/模拟器/完整性检测。

- 启动、渲染、网络请求都正常；
- 真正“申领网证 / 电子证照”需要可信执行环境与活体/证件核验，容器内基本不可能通过；
- 若只是要“装得上、起得来、界面能点”，当前状态已满足。

截图：`_id2.png`（协议弹窗）、`_id4.png`（首页教程遮罩）、`_id6.png`（通知权限）、`_id7.png`（安全风险提示）、`_id8.png`（风险提示再次出现）。