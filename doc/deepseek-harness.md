# 在 A527 开发板上安装 DeepSeek Harness

这是现有设备上的可选工具，使用官方 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) Web 版。
当前固定 `@deepseek-ai/dsh@0.2.1-rc.2` 与 Node.js `24.22.1` Linux ARM64，依赖版本和 npm integrity 记录在 `target/deepseek-harness/package-lock.json`。
Node.js 归档采用官方 SHA256 校验。它调用配置的模型 API，安装包不包含本地大模型权重。

工具安装在普通用户的家目录。现有 Tono Canvas 开机入口保持原样；本改动没有将 Harness 加入默认 Ubuntu / Buildroot 镜像。
默认 Web 服务仅监听 `127.0.0.1:3080`，通过 SSH 转发访问。使用独立的空工作目录 `~/deepseek-workspace`，首次安装不会导入画板、模型密钥或已有会话。
Harness 的文件访问和命令执行仍遵循其自身权限策略；同一用户账户并不是与白板数据隔离的安全边界。

## 生成离线包

在 Linux ARM64 构建机上以普通用户执行，需要 `bash`、`curl`、`xz`、`tar`、GNU coreutils / findutils。构建过程下载公开 npm 依赖；不运行依赖的安装脚本，使用其中随包发布的 ARM64 本机扩展。

```sh
bash scripts/prepare-deepseek-harness.sh /absolute/path/dsh-artifacts
```

产物包括 `.tar.gz`、对应 `.sha256` 以及 `install-deepseek-harness.sh`。源码分支不提交 Node.js 或 `node_modules`。
包内保留上游许可证和依赖锁文件。后续复现应保留生成的归档及 SHA256；运行时新增插件仍可能需要联网下载。

## 安装到设备

设备需要 ARM64 Linux、glibc、Python 3.12 或更新版本、bash、tar、sha256sum 及用户 systemd 会话。现有 Ubuntu 24.04 和本仓库 Buildroot 配置具备这些基础组件；具体系统仍需实机检查。
安装内容约 1 GiB，另需存放压缩包，并在安装后保留至少 256 MiB 可用空间。

将三份产物复制到设备的用户目录，然后以 `avaota` 用户运行，**不要使用 sudo**：

```sh
scp dsh-artifacts/* avaota@DEVICE_IP:~/
ssh avaota@DEVICE_IP
bash ~/install-deepseek-harness.sh \
  ~/deepseek-harness-0.2.1-rc.2-node24.22.1-linux-arm64.tar.gz
cd ~/deepseek-workspace
~/.local/bin/tono-dsh web --no-open
```

安装器校验归档和内部文件，先验证 Node / CLI 能运行，再切换 `current` 链接。重复安装同一归档会检查现有文件；不会覆盖模型配置和会话。
归档直接解压到安装位置所在文件系统，避免小容量 TF 卡上额外复制一份完整依赖。

服务方式需显式加 `--start`，这会启用并启动用户服务：

```sh
bash ~/install-deepseek-harness.sh \
  ~/deepseek-harness-0.2.1-rc.2-node24.22.1-linux-arm64.tar.gz --start
systemctl --user status deepseek-harness.service
journalctl --user -u deepseek-harness.service -n 60 --no-pager
```

用户服务在用户 systemd 管理器启动后运行；当前 kiosk 的 `avaota` 登录会话通常会启动该管理器。本安装器不更改系统登录或 lingering 设置。
服务使用较低 CPU / I/O 调度权重，具体响应和与白板同时运行的占用仍需 A527 实测。

## 从 Mac 打开

在 Mac 终端建立转发，并保持该窗口打开：

```sh
ssh -N -L 3080:127.0.0.1:3080 avaota@DEVICE_IP
```

在浏览器打开 Harness 启动日志中 `dsh web:` 后的完整链接，地址应为 `http://127.0.0.1:3080/` 并带登录 token。
该链接包含会话凭据，只在自己的浏览器使用。单独打开不带 token 的地址不能完成首次认证。
进入 Settings → Models 设置模型，在界面中输入自己的 API key；密钥不放进 Git 或聊天记录。
随后选择 `~/deepseek-workspace` 开始使用。

## 停止和回退

```sh
systemctl --user disable --now deepseek-harness.service
```

程序位于 `~/.local/opt/deepseek-harness/releases/`，入口为 `~/.local/bin/tono-dsh`，数据位于 `~/.local/share/deepseek-harness/`。
后续切换版本时安装器保留原 release 和 `previous` 链接，服务文件如有变化则留存时间戳备份。回退前先停止服务，再将 `current` 指回旧 release；保留数据目录。

## 当前验证范围

已在本机 ARM64 Ubuntu 24.04 容器中以 UID 1000 验证官方包入口、Web 服务启动、带 token 的认证和 HTML 返回，以及 SIGTERM 正常退出；未配置 API key，没有发起模型请求。
该环境的首次 Web 就绪约 1.5 秒，进程 RSS 约 409 MiB，不能当作 A527 实机结果。
开发板当前无法连接，因此尚未安装到设备，Buildroot 原生环境、本机沙盒执行任务、模型对话、开机启动以及与白板并行运行均待实机验证。

参考：[官方启动说明](https://github.com/deepseek-ai/deepseek-harness)、[Web 使用指南](https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart)、[Node.js 归档校验](https://nodejs.org/dist/v24.22.1/SHASUMS256.txt)。
