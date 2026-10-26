# Avaota A1 / 全志 A527：开机进入 DeepSeek Harness

该分支提供新的设备启动方式：

```text
systemd → Xorg + Openbox + Fcitx5 → Chromium 全屏
            ↑                         ↓
      原有显示和音频配置       本机 DeepSeek Harness :3080
```

启用后，开机直接显示 Harness，不再启动 Tono Canvas。沿用现有 GPU 探测、HDMI 分辨率、中文输入法和音频服务；旧应用及画板数据留在原位置，便于回退。
这次提供现有 Ubuntu / Buildroot 设备上的离线安装与启动切换工具，没有重新构建或烧录完整系统镜像。

## 本机自动进入与认证

Harness 和浏览器均以普通用户运行，保留 Chromium 沙盒。Harness 仍监听 `127.0.0.1:3080`，没有开放局域网匿名访问。
服务包装器等到官方就绪提示后，将本次启动的认证链接写到 `/run/user/UID/tono-deepseek-harness/launch.json`，目录权限 0700，文件权限 0600。
浏览器自动使用该链接完成认证，用户不需要 SSH 转发或手动复制 token。日志中的启动链接经过隐藏处理。

每次 Harness 重启都会产生新的认证链接。浏览器监督进程会结束旧浏览器，再以新链接打开；浏览器配置和模型会话保存在用户目录中。
正常关机先结束浏览器，再退出 Xorg。异常退出由 systemd 重新启动。页面启动不等待外网或模型服务器连通。
首次使用仍需在 Settings → Models 配置模型，默认工作目录会自动创建；自动进入页面不等于已经配置模型账号。

## 离线包内容与构建

缺少 `xdg-user-dir` 的精简系统通过官方 `documentsDirectory` 配置指定工作目录，不增加桌面服务。

固定版本为 `@deepseek-ai/dsh@0.2.1-rc.2`、Node.js `24.22.1`、`playwright-core@1.64.1` 对应的 ARM64 Chromium。
Node.js 使用官方归档 SHA256；npm 版本与 integrity 固定在 `target/deepseek-harness/package-lock.json`。Chromium 由固定版本的官方 Playwright 下载器获取，版本和补充库记录在包内 `browser.json`，最终归档另有 SHA256。
保留上游许可证。Node.js、浏览器和 node_modules 不提交到 Git。

在 Ubuntu 24.04 ARM64 构建机上准备工具和补充库：

```sh
sudo apt-get install -y curl xz-utils python3 ca-certificates \
  libatk1.0-0 libatk-bridge2.0-0 libatspi2.0-0
bash scripts/prepare-deepseek-harness.sh /absolute/path/dsh-kiosk-artifacts
```

除 Chromium 外，只额外携带现有图形运行时缺少的三个 accessibility 动态库；不替换 Mesa、glibc 或 GPU 驱动。
构建过程不执行 npm 安装脚本，使用包内预编译的 ARM64 扩展。生成归档、SHA256、普通用户安装器和启动切换脚本。

## 安装和切换开机入口

设备需要已有本仓库的 `tono-kiosk.service`、ARM64 Linux、glibc、Python 3.12+、bash、用户 systemd 会话和 sudo。
若为 Buildroot，继续在 `/opt/tono-runtime` 中运行图形会话；Harness 后端运行于原生系统，通过共享的 `/home`、`/run` 与浏览器协作。
安装前检查 TF 卡剩余空间：浏览器版本约 1.5 GiB，另需存放压缩包，安装后至少保留 256 MiB。安装器直接解压到目标文件系统，不重复复制完整依赖。

将四份产物复制到设备，然后以普通用户安装：

```sh
scp dsh-kiosk-artifacts/* avaota@DEVICE_IP:~/
ssh avaota@DEVICE_IP
bash ~/install-deepseek-harness.sh \
  ~/deepseek-harness-0.2.1-rc.2-node24.22.1-kiosk1-linux-arm64.tar.gz
sudo bash ~/enable-deepseek-kiosk.sh avaota --restart
```

最后一条命令先通过原有服务停止流程保存并关闭白板，再切换图形会话。以后开机直接进入 Harness。
如果只想在下次开机生效，省略 `--restart`。切换器先备份原启动脚本和已有配置，备份目录打印在终端。
普通用户安装器不使用 sudo；只有系统启动入口切换需要管理员权限。
重复安装会校验现有 release，保留模型配置、会话和浏览器配置。

## 数据、网络与维护

- 安装位置：`~/.local/opt/deepseek-harness/releases/`，`current` 指向当前版本。
- Harness 数据：`~/.local/share/deepseek-harness/`。
- 浏览器配置：`~/.local/share/deepseek-harness-browser/`。
- 初始空工作目录：`~/deepseek-workspace`。

模型密钥只在本机 Harness 设置中输入，不写入 Git。Harness 可使用配置允许的用户文件和命令，同一账户不是与旧画板数据隔离的安全边界。
已保存的 Wi-Fi 配置继续由 NetworkManager 管理；原白板内的 Wi-Fi 页面不再是开机入口，必要时通过 SSH 的 `nmcli` 配置网络。

```sh
systemctl status tono-kiosk.service
systemctl --user status deepseek-harness.service
journalctl -b -u tono-kiosk.service -n 80 --no-pager
journalctl --user -u deepseek-harness.service -n 80 --no-pager
```

临时恢复原白板入口：停用 Harness 用户服务，移除本工具创建的 `/etc/systemd/system/tono-kiosk.service.d/60-deepseek-harness.conf`，执行 `sudo systemctl daemon-reload` 和 `sudo systemctl restart tono-kiosk.service`。共享启动脚本在没有该覆盖配置时仍使用原白板会话。

## 验证边界

主机测试覆盖启动链接的本机地址限制、私有权限、日志隐藏、退出清理及浏览器重新认证行为，并保留原 GPU 会话回退测试。
实际 ARM64 图形容器用于检查普通用户浏览器是否能加载 Harness、服务重启后是否重新进入页面，以及浏览器和服务能否正常停止。
开发板目前无法连接，尚未部署。A527 冷启动、GPU 硬件加速、中文输入、音视频、Wi-Fi 和实际模型对话仍需实机验证；容器结果不代表这些硬件功能已通过。

参考：[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)、[官方 Web 使用指南](https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart)、[Playwright 浏览器发行说明](https://playwright.dev/docs/browsers)。
