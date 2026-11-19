# 云手机里控制微信

结论先放前面：**能用 adb 全自动控制**。之前 `docker exec ... uiautomator dump` 失败，是因为容器内没有 adb/权限上下文；
改用「宿主机 adb 连容器 IP」后，`uiautomator dump`、`screencap`、`input` 全部正常，微信的登录页、手机号输入页都能按文字精确定位控件。

## 一、可用的控制通道：adb over bridge IP

容器用 bridge 网络，没有 publish 端口，但宿主机（板子）能直连容器 IP：

```bash
IP=$(docker inspect -f '{{.NetworkSettings.IPAddress}}' m6)   # 例如 172.17.0.7
adb connect $IP:5560        # 容器内 service.adb.tcp.port=5560（m1..m6 -> 5555..5560）
adb devices                 # -> 172.17.0.7:5560  device
adb -s $IP:5560 shell getprop ro.serialno
adb -s $IP:5560 shell uiautomator dump /data/local/tmp/ui.xml   # 实测可用
adb -s $IP:5560 exec-out screencap -p > shot.png                # 实测 500KB/帧
```

注意：`docker exec m6 uiautomator dump` 会返回空/旧文件（同一路径的旧 XML 会骗人），**必须走 adb**。

## 二、现成工具（本仓库 tools/）

```bash
scp tools/wechat_adb.sh tools/ui_find.py root@192.168.99.50:/root/
# 板子上：
sh /root/wechat_adb.sh m6 start            # 启动微信
sh /root/wechat_adb.sh m6 focus            # 当前前台 Activity
sh /root/wechat_adb.sh m6 ui               # dump 到 /tmp/ui.xml
python3 /root/ui_find.py /tmp/ui.xml       # 列出所有带文字的控件（文字/坐标/类）
sh /root/wechat_adb.sh m6 tap-text 'Log In'  # 按文字点击（精确>前缀>包含，优先 clickable）
sh /root/wechat_adb.sh m6 text 13800000000   # 往焦点输入框输入
sh /root/wechat_adb.sh m6 shot name          # 截图到板子 /tmp/name.png
```

## 三、实测坐标（720x1280，微信 8.0.80）

| 界面 | 控件 | bounds | 点击点 |
| --- | --- | --- | --- |
| WelcomeActivity（启动页） | Log In | [38,1062][259,1148] | 148,1105 |
| 同上 | Sign Up | [463,1062][684,1148] | 573,1105 |
| MobileInputUI（手机号登录） | Enter phone number | [220,406][669,507] | 440,456 |
| 同上 | Accept and Continue | [174,920][545,1006] | 360,963 |
| 同上 | Other Login Options | [36,585][255,619] | 145,602 |

实测流程：`tap-text 'Log In'` → MobileInputUI → 点输入框 → `input text` → 输入框出现号码、按钮变绿。
截图：`_wx_login.png`（启动页）、`_wx_phone.png`（手机号页）、`_wx_demo.png`（已输入号码）。

## 四、登录这一步需要真人

微信登录必须「手机号 + 短信验证码」或「已登录设备扫码确认」，没有任何凭据可以绕过：

1. 给我一个手机号 → 我点 `Accept and Continue` 触发验证码，你把 6 位码发我 → 我输入并登录；
2. 或者你自己在 m6 里登一次（截图看界面，我按坐标点也行）；
3. 登录后我可以继续写「进聊天 → 发消息 → 读未读」的脚本，全部基于 `uiautomator dump` 的文字定位。

登录后 cookie 存在 `/data/data/com.tencent.mm`，容器重启不掉；`docker commit` 可以把已登录状态固化进镜像（注意：等于把账号凭据写进镜像，谨慎）。

## 五、其他路线（对比）

| 方案 | 现状 | 评价 |
| --- | --- | --- |
| adb + uiautomator（本文） | **实测可用** | 零依赖、最快，够做点击/输入/截图/断言 |
| Appium + UiAutomator2 | 未装（要往容器塞 server APK） | 适合长期稳定的多机回归，但 redroid 里装服务端有坑 |
| 无障碍服务（自写 APK） | 未做 | 能读微信自绘控件，能力最强，成本最高 |
| monkey | **坏**（`exec /bin/monkey: exec format error`） | 不可用 |
| docker exec + input tap | 可用但笨 | 只能盲点坐标，拿不到控件树 |

## 六、踩过的坑

- `docker exec m6 uiautomator dump` 会「成功」但写出 0 字节或旧内容，必须 adb。
- `uiautomator dump --compressed` 在微信手机号页返回 `ERROR: null root node returned by UiTestAutomationBridge`，普通 dump 正常。
- 微信启动页是 `com.tencent.mm.plugin.account.ui.WelcomeActivity`（不是 LauncherUI），`am start -n com.tencent.mm/.ui.LauncherUI` 会转发过去。
- `input text` 输入长数字串偶尔丢首位（实测输入 `10000000000` 显示 `10000000005`），关键字段建议输入后 dump 校验再继续。