# 待办与阻塞（2026-11-20）

## 已完成
- 6 台云手机（m1–m6）+ 相机 HAL（预览 / 拍照）+ 外部画面注入（静态图已验证）。
- 踩坑记录补到 `G98-踩坑全记录.md` 第九章（Camera2/JPEG/推流/adb/内存）。
- 本地 git 仓库已建：`cloudphone`（2 个 commit，115 个文件，~900KB）。
- 微信控制方案文档 `docs/wechat-control.md` + 脚本 `tools/wx.sh`。

## 阻塞 1：推送到 GitHub
- 目标：`https://github.com/xiaoshouhehuoren/cloudphone.git`（组织 xiaoshouhehuoren，新 repo `cloudphone`）。
- 现状：构建机 **无外网**（curl 到 github/baidu 都返回 000，git 报 `schannel: SEC_E_NO_CREDENTIALS`），
  也没有任何 GitHub 凭据（无 `~/.ssh` 私钥、无 `.git-credentials`、无 gh CLI）。
- 需要：① 能出网的网络/代理；② GitHub token 或 SSH key；或由你在有网的机器上执行：
  ```bash
  git clone C:/Users/rosetta/Desktop/g98/cloudphone
  cd cloudphone && git remote set-url origin <你的仓库地址> && git push -u origin master
  ```
- 离线包：`cloudphone.bundle`（`git clone cloudphone.bundle cloudphone` 即可还原全部提交）。

## 阻塞 2：安装"网络身份认证"App
- 目标：装进云手机并确保能正常启动。
- 现状：本机、板子、容器镜像里都**没有**这个 APK（已全盘搜索），且无法下载（无外网）。
- 需要：把 APK 放到 `C:\Users\rosetta\Desktop\g98\netauth.apk`（或告诉我下载地址 + 可用的网络），
  之后一条命令安装：
  ```bash
  docker cp netauth.apk m6:/data/local/tmp/a.apk
  docker exec m6 pm install -r /data/local/tmp/a.apk
  docker exec m6 monkey -p <包名> -c android.intent.category.LAUNCHER 1
  ```
- 注意：这类 App 常检测 root / 模拟器 / 多开；redroid 需要额外伪装（前面已有设备伪装经验，见踩坑记录第四章）。

## 已在推进：微信控制
- 容器内微信 8.0.80 已装，`am start -n com.tencent.mm/.ui.LauncherUI` 能到登录页（截图 `_wx1.png`）。
- 控制方案见 `docs/wechat-control.md`：docker exec（现在可用）/ UiAutomator / Appium / 无障碍服务。
- 下一步：先人工登录一次（扫码），再打通 adbd TCP 或装 UiAutomator2，写"进聊天→发一条文本"的脚本。
