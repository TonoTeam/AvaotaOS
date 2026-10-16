# Tono Canvas 单应用镜像

支持 Avaota A1、Ubuntu 24.04 (`noble`)、ARM64 和现有 BSP。保留内核、GPU/HDMI/网卡固件，使用 Xorg、Openbox、Qt Quick 和 WebEngine；不安装 GNOME、登录管理器、桌面面板或桌面应用。镜像保留 apt、SSH、网络、USB、串口、日志、中文字体与拼音输入、音频和摄像头运行依赖。

Kiosk 的 SD 卡作为固定根存储使用：打包阶段将设备树的 `sdc0` 设为 `non-removable`，移除 GPIO 插卡检测，其他节点保持原样。本次 A1 实机的 GPIO 插卡检测报告未插卡，导致启动器能读取内核，但 Linux 关闭 SD 控制器并报 `LABEL=rootfs does not exist`。此设置已在该实机验证可识别 SD 并挂载根分区；只影响 kiosk 镜像，也适用于复用内核或 rootfs 缓存的构建。运行中不能拔出根存储卡。

## 构建应用

在 Ubuntu 24.04 ARM64 上检出 `tono-canvas-app`，按其 `docs/linux-port.md` 安装构建依赖。macOS 不能直接构建 Linux 目标；可以使用 ARM64 Ubuntu 虚拟机或容器。不要把 x86_64 的 deb 放进 ARM64 镜像。

```sh
cmake -S /path/to/tono-canvas-app/linux -B /tmp/tono-linux-build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr
cmake --build /tmp/tono-linux-build --parallel
xvfb-run -a ctest --test-dir /tmp/tono-linux-build --output-on-failure
cd /tmp/tono-linux-build
cpack -G DEB
```

生成的 deb 使用 App 自身的依赖声明安装 Qt、WebEngine 和资源，不需要在目标系统编译应用。使用含 Linux `SIGTERM`/`SIGINT` 保存退出支持的版本。

## 构建镜像

在 Linux 构建机的 AvaotaOS 根目录运行（需要 sudo、loop 设备和挂载能力，普通非特权容器不足以打包完整 SD 镜像）：

```sh
sudo bash build_all.sh \
  -b avaota-a1 -v noble -t kiosk \
  --app-deb /absolute/path/to/tono-canvas_VERSION_arm64.deb \
  -k no -g bsp -i no -o no -e no
```

用户名与密码继续使用构建器原有 `-u`、`-p`、`-s` 参数。默认用户仍为 `avaota`；自定义用户会写入 kiosk 服务配置。应用以该普通用户运行。

基础缓存是 `build_dir/rootfs-noble-kiosk.tar.gz`，只包含系统运行组件。包清单、rootfs 构建脚本和板级配置变化会使该缓存失效。每次打包都在临时根文件系统安装本次 `--app-deb` 及其依赖，再计算镜像容量；修改应用不会重建内核，也不会误用旧 App。安装包版本和 SHA-256 记录在镜像的 `/etc/tono-canvas-image`。

烧写整张镜像会覆盖目标介质；已有设备更新 App 时使用 `sudo apt install ./tono-canvas_VERSION_arm64.deb`，不要为更新 App 重刷系统。

## 启动与维护

`tono-kiosk.service` 在 tty1 创建普通用户的 PAM/logind 会话，启动带 Xauthority 认证的 Xorg、无合成器的 Openbox、Fcitx5 和用户音频服务。启动命令为：

```sh
QT_QPA_PLATFORM=xcb tono-canvas --fullscreen --software-rendering --low-latency
```

App 的 X11/X Shape 原生墨迹路径保持可用。第一版固定软件 OpenGL；验证硬件加速时先记录此基线，再调整启动脚本。WebEngine 沙盒保持开启；镜像的单应用 AppArmor 配置允许 Chromium 使用用户命名空间，不改变全局策略。实机内核仍须支持 Chromium 使用的命名空间和 seccomp。

Openbox 保证文件对话框、输入法候选框和全屏之间的焦点交接；不启动面板或桌面。保留 F11、Escape 和 Alt+F4 等开发操作。App 退出会重启整个图形会话；一分钟内连续失败五次会停止重试，可从 SSH 查看日志并重启服务。tty1 不运行登录提示，串口和其他终端继续可用。

```sh
systemctl status tono-kiosk.service
journalctl -b -u tono-kiosk.service
sudo systemctl stop tono-kiosk.service
sudo apt install ./tono-canvas_VERSION_arm64.deb
sudo sync
sudo systemctl start tono-kiosk.service
```

保留默认 XDG 目录、应用设置、注册身份和原有画板，不指定新的 `--data-dir`，不覆盖 AI 服务地址。离线时 App 仍能进入本地画板，AI 连接由应用处理。

使用带 Wi-Fi 页面支持的 Tono Canvas 1.1.4 或更新版本，可在 App 的「首页 → 设置 → Wi-Fi」扫描热点、连接、断开和忘记当前用户保存的网络。支持开放网络、WPA/WPA2 个人版、WPA3 和 OWE；企业认证、WEP 与手动添加隐藏热点暂不支持。打开页面后才查询 NetworkManager 和扫描，不把无线连接加入应用启动依赖。

镜像显式安装 `polkitd`，将 kiosk 用户加入 `tono-network` 组，并安装 `49-tono-network.rules`。仅活动的本地会话可扫描、切换 Wi-Fi、控制连接和修改自己的网络配置；应用仍以普通用户运行，不依赖桌面认证窗口。密码经系统 D-Bus 直接交给 NetworkManager，由系统保存，应用不写入自身设置、命令行或日志。无线操作不修改现有 netplan 有线网络配置。

2026-10-16，A1 kiosk 实机升级至 1.1.4，核实 App 会话的扫描、无线开关、连接控制和自身配置权限，并在页内列出真实热点。用户在设备上输入密码后确认连接成功；安装前后已有画板和 App 设置文件的 SHA-256 一致。同日完成系统文件恢复后的断电启动验证，App 自动显示，Wi-Fi 自动重连。企业认证与隐藏热点未在本次验收中验证。

正常停止或关机时，先向 App 发送 SIGTERM，Qt 将它交给现有保存关闭流程；Xorg、音频和输入法保留至 App 退出，最多等待 90 秒。超时会记录错误再结束会话。异常崩溃、强制断电或保存失败不保证未保存笔迹可恢复；已保存的数据不由启动器删除。Fcitx5 初始配置含美式键盘和拼音，已有输入法配置不覆盖。

如果重启后只有背光、App 未显示，按[黑屏排查与系统文件恢复](kiosk-recovery.md)先检查服务、用户组、应用及软件包数据库，并在修复后验证断电启动。

## 启动速度

Kiosk 打包时默认移除 `earlycon`，使用 `quiet loglevel=3`，继续保留串口控制台与登录。内核消息仍可通过 `dmesg` 和 `journalctl -b -k` 读取；日志级别只减少同步输出到控制台的内容，见[内核日志说明](https://cdn.kernel.org/doc/html/latest/core-api/printk-basics.html)。需要完整串口调试时，可在 `/boot/extlinux/extlinux.conf` 的 `append` 行恢复原来的 `earlycon` 和 `loglevel=8`。

`tono-wifi.service` 在 `basic.target` 之后加载 AIC8800，与后续启动任务并行，避免阻塞早期 `systemd-modules-load.service`；网络管理与无线功能继续保留。用户音频服务异步启动。软件渲染基线中的 Xorg 显式设置 `AccelMethod=none`，跳过每次启动都会失败的 glamor 探测；此设置仅用于 kiosk，验证 GPU 加速时需同时调整 `21-tono-software.conf` 和应用渲染参数。

2026-10-16，在同一 A1 4GB、同一 SD 卡、同一 Tono Canvas 1.1.3 和 2560×1440 输出上，通过两次正常重启对比：

| 测量项 | 优化前 | 优化后 |
| --- | ---: | ---: |
| 内核至 systemd 接管（含 initramfs） | 8.446 秒 | 3.049 秒 |
| systemd 用户空间启动 | 10.918 秒 | 6.498 秒 |
| 内核启动至系统目标完成 | 19.364 秒 | 9.548 秒 |
| 内核启动至应用首帧提交 | 29.794 秒 | 20.095 秒 |

首帧使用临时 GLX/EGL 观察工具记录首次至少 640×480 的画面交换，两次均为 2560×1440；测量后已移除注入，正式镜像不包含该工具。以上时间从 Linux 内核启动计起，未包含约 9 秒的 SyterKit/固件阶段，也不包含触屏输入验收。两次测量均已完成 SD 扩容；首次烧录后的开机还包含一次性初始化。改善的是实际应用首帧，不能用 systemd 完成时间替代可见画面时间。优化后验证全屏、SSH、无线接口识别正常；没有执行无线 AP 连接或音视频外设测试。

## 验证

构建机可运行：

```sh
python3 tests/test_kiosk.py
python3 tests/test_kiosk_board_index.py
bash -n build_all.sh scripts/mkrootfs.sh scripts/pack.sh scripts/lib/kiosk.sh
sudo bash scripts/verify-kiosk-image.sh /absolute/path/to/AvaotaOS-kiosk.img
```

镜像检查以只读方式验证 FAT/ext4、MBR/BootROM 校验和、内核沙盒能力、App 动态库与包状态，以及 kiosk 服务、自定义用户、输入法和音频配置。应用运行其现有 Linux 普通窗口、低延迟全屏 QtTest，以及新增异步保存退出信号测试。静态镜像检查和 Xvfb 测试不能替代以下实机验收：

- 冷启动直接进入 App；断网仍能进入本地画板；SSH、apt、USB、串口正常。
- 画笔、撤销、存取、HTML/WebGL、截图、文件选择、中文输入、F11/Escape 与弹窗焦点。
- App 崩溃后的会话重启，以及服务停止、重启、关机后的已保存画板与设置恢复。
- 实际麦克风录音上传、扬声器朗读、摄像头拍照；没有外设时标记未验证。
- 与原 GNOME 环境比较启动时间、空闲内存、普通画板和 HTML 画板的延迟；不以容器测试数字替代硬件测量。

2026-10-16 的 A1 4GB 实机已验证：冷启动识别 SD 根分区并自动扩容、DHCP 与 SSH、普通用户启动 Tono Canvas 1.1.3、HDMI 2560×1440 全屏，用户确认屏幕已显示 App；运行约 4 分钟服务重启次数为 0。已保存串口、Xorg、服务和窗口属性记录。画笔、弹窗、中文输入、音视频、摄像头、崩溃恢复、关机恢复及与 GNOME 的性能对比仍需单独实机验收。
