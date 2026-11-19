# 云手机里控制微信

现状：m6 里装了 **微信 8.0.80**（`com.tencent.mm`），启动后停在登录页（Log In / Sign Up）。
容器内没有 adbd 的 TCP 监听，所以下面所有操作都先用 `docker exec` 验证；只要能开 adb/Appium，命令可平移。

## 一、已验证可用的最小控制面（docker exec）

```bash
# 启动微信（实测进入 WelcomeActivity 登录页）
docker exec m6 am start -n com.tencent.mm/.ui.LauncherUI

# 当前前台窗口
docker exec m6 dumpsys window | grep mCurrentFocus

# 截图（要落到 /data/local/tmp，/sdcard 是 FUSE，docker cp 读不到）
docker exec m6 screencap -p /data/local/tmp/wx.png
docker cp m6:/data/local/tmp/wx.png ./wx.png

# 点击 / 输入 / 滑动
docker exec m6 input tap 150 1100
docker exec m6 input swipe 360 900 360 300 300
docker exec m6 input text 'hello'

# 返回 / Home
docker exec m6 input keyevent 4
```

## 二、UI 自动化（推荐路线）

1. **UiAutomator + adb**：`uiautomator dump /data/local/tmp/ui.xml` 拿控件树，按 `text`/`resource-id` 定位后 `input tap`。
   注意：微信主界面大量是自绘/加密控件，dump 出来的节点有限，需配合坐标点击兜底。
2. **Appium + UiAutomator2**：在容器里装 `io.appium.settings` + `appium-uiautomator2-server`，宿主机跑 Appium，
   capability `appPackage=com.tencent.mm`、`appActivity=.ui.LauncherUI`。适合"发消息 / 读列表"这类稳定流程。
3. **无障碍服务（AccessibilityService）**：写一个小 App 装进容器，用 `AccessibilityNodeInfo` 读微信界面，
   比 uiautomator 拿到更多节点；能力最强，但要自己写客户端。
4. **坐标脚本**：分辨率固定 720x1280，用截图 + 固定坐标最省事，适合单一机型批量跑。

## 三、必须先解决的前置条件

- **登录**：微信登录要手机扫码/短信，脚本化之前先人工登录一次；登录态存在 `/data/data/com.tencent.mm`，
  容器数据目录 `/srv/redroid/m6` 持久化，重启不丢。
- **ADB over TCP**：容器内 `setprop service.adb.tcp.port 5560; stop adbd; start adbd`（在同一个 `sh -c` 里），
  当前实测未监听，需要再排查 redroid 的 adbd 启动方式；不通就用 `docker exec` 代替。
- **风险**：微信对自动化/多开有风控，批量操作容易被限制登录；建议只做"人工操作加速"，不要做群发/加好友。

## 四、下一步可做

1. 打通 adbd TCP（或装 Appium）→ 用 UiAutomator2 写一条"打开微信 → 进入某个聊天 → 发送一条文本"的脚本。
2. 给 6 台容器统一装控制端 App，做批量管理面板。
