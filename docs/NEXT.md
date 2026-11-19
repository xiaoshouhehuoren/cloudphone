# 待办与阻塞（2026-11-20）

## 已完成
- 6 台云手机（m1–m6）+ 相机 HAL（预览 / 拍照）+ 外部画面注入（静态图已验证）。
- 踩坑记录补到 `G98-踩坑全记录.md` 第九章（Camera2/JPEG/推流/adb/内存）。
- 本地 git 仓库已建：`cloudphone`（2 个 commit，115 个文件，~900KB）。
- 微信控制方案文档 `docs/wechat-control.md` + 脚本 `tools/wx.sh`。

## 已完成 1：网络身份认证 App（2026-11-20）
- 已从应用宝下载官方 APK（`cn.cyberIdentity.certification` 1.2.41，68.7MB，MD5 `463F0578DC475E68F7D0A46568FE75F0`）。
- 已装进 m6：`pm install -r` → `Success`；启动 `cn.wh.project.view.v.SplashActivity` → `MainTabActivity`，首页正常渲染。
- 容器默认无外网（板子无默认路由），用 `tools/win_http_proxy.py` + `settings put global http_proxy 192.168.99.1:8899` 让 App 通过 Windows 侧 CONNECT 代理联网，已过网络检查。
- 仍会弹「设备存在安全风险」：redroid 无 TEE、系统被 root，属这类 App 的完整性检测；装/启/联网都已验证，办证需真机安全环境。
- 细节（下载直链、安装、启动、代理、限制）：`docs/identity-app.md`。

## 待办 2：推送到 GitHub
- 目标：`https://github.com/xiaoshouhehuoren/cloudphone`（组织已存在，repo 尚未创建）。
- 阻塞点只有一个：**GitHub token**。git 客户端走 schannel 出网失败（`SEC_E_NO_CREDENTIALS`），但 python 的 OpenSSL 可以出网，
  因此工具链已备好：`vendor/` 里放了 dulwich 1.2.21（纯 python，`PYTHONPATH=vendor`），
  一条命令即可建仓 + 推送：
  ```bash
  set GH_TOKEN=ghp_xxx
  PYTHONPATH=vendor python tools/push_github.py
  ```
- 没有 token 时的离线交付：`cloudphone.bundle`（`git clone cloudphone.bundle cloudphone` 还原全部提交）、`cloudphone.zip`。

## 继续推进：微信控制
- 容器内微信 8.0.80 已装，`am start -n com.tencent.mm/.ui.LauncherUI` → 登录页（`_wx1.png`），`input tap 150 1100` 可进手机号登录页（`_wx2.png`）。
- 现成工具：`tools/wx_ctl.sh start|stop|shot|tap x y|swipe ...|text <str>|key <code>|top`（在板子上跑）。
- 注意：redroid 的 `monkey` 已损坏（`exec /bin/monkey: exec format error`），uiautomator dump 返回 0 但不生成文件，
  所以当前是「坐标点击 + 截图确认」的路线；要稳定自动化需走 Appium/UiAutomator2 或无障碍服务。
- 登录需要手机号 + 验证码（或扫码），这一步得真人参与。
