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

生成的 deb 使用 App 自身的依赖声明安装 Qt、WebEngine 和资源，不需要在目标系统编译应用。使用 1.1.5 或更新版本，包含 Linux 保存退出和 `--opengl-es` 支持；旧包会在构建入口被拒绝。

## 构建镜像

在 Linux 构建机的 AvaotaOS 根目录运行（需要 sudo、loop 设备和挂载能力，普通非特权容器不足以打包完整 SD 镜像）：

```sh
sudo bash build_all.sh \
  -b avaota-a1 -v noble -t kiosk \
  --app-deb /absolute/path/to/tono-canvas_VERSION_arm64.deb \
  -k no -g bsp -i no -o no -e no
```

用户名与密码继续使用构建器原有 `-u`、`-p`、`-s` 参数。默认用户仍为 `avaota`；自定义用户会写入 kiosk 服务配置。应用以该普通用户运行。

基础缓存是 `build_dir/rootfs-noble-kiosk.tar.gz`，只包含系统运行组件。包清单、rootfs 构建脚本、板级配置或精简保留规则变化会使该缓存失效。每次打包都在临时根文件系统安装本次 `--app-deb` 及其依赖，再计算镜像容量；修改应用不会重建内核，也不会误用旧 App。安装包版本和 SHA-256 记录在镜像的 `/etc/tono-canvas-image`。

烧写整张镜像会覆盖目标介质；已有设备更新 App 时使用 `sudo apt install ./tono-canvas_VERSION_arm64.deb`，不要为更新 App 重刷系统。

## 镜像体积

Kiosk 在安装本次 App 后、计算容量前清理 APT 索引、下载包和二进制缓存，再在内核包安装结束后清理一次。此步骤也适用于旧的基础 rootfs 缓存。保留 apt、dpkg 状态和软件源；维护时先执行 `sudo apt update`。APT 的两个可再生成的二进制缓存默认关闭，索引仍会在 `apt update` 时重新下载。

`scripts/trim-kiosk-rootfs.py` 仅接受带 `DESKTOP=kiosk` 标记的暂存根目录。它精简手册、Info、普通包文档及中英文以外的系统 gettext 翻译，保留包的 copyright、LICENSE、COPYING、NOTICE 及公共许可证，包括 `cjson-LICENSE`、`opus-COPYING` 等带前缀的名称，安装对应的 dpkg 路径过滤规则。应用安装前先更新暂存根目录中的 dpkg 过滤规则，确保复用旧缓存时也能保留这些文件。英文/中文 gettext 翻译继续安装，Qt 翻译、WebEngine 语言包/资源、全部字体、拼音词库、多媒体、Mesa/LLVM、固件和内核模块均保留。此清理不处理用户目录、Wi-Fi 配置、SSH 密钥或应用身份；它用于构建工厂镜像，不作为已部署设备上的清理命令。

Kiosk 根分区从 128 MiB 开始，保留前 16 MiB 的 BootROM/启动器区域，FAT 启动分区为 112 MiB。其他镜像类型继续使用原布局。容量按精简后的 rootfs、内核和固件大小计算，额外预留约 10%＋128 MiB 给文件系统元数据/日志，再预留至少 256 MiB 或内容的 10%（取较大者）用于首次启动前的维护，按 16 MiB 对齐。去掉旧的固定 1 GiB 加 20% 预留；ext4 保留日志，root 保留块比例从 5% 调整为 1%。

GPU 配置的全新 rootfs 构建曾触发 177 MiB 空闲空间不足检查；容量估算扩大元数据和内核安装预留，仍保留以下实际空间校验。

构建结束检查 root 可用空间至少 256 MiB、boot 至少 32 MiB，不足则报错，不输出容量不足的镜像。首次启动仍由 `init-resize.service` 将根分区扩展到 SD 卡容量。精简减少镜像和烧录体积，不代表内存或启动时间按同样比例下降。恢复工具随镜像安装到 `/usr/local/sbin/tono-repair-board-index`。

2026-10-17 的完整构建将原始镜像从 3.31 GB 缩小到 1.95 GB，XZ 从 486 MB 缩小到 392 MB；软件包及版本保持一致。见[体积与功能验证记录](kiosk-size-validation.md)，其中单独记录了新旧镜像均复现的冻结 HTML 截图测试失败，以及尚未完成的实机验收。

## 启动与维护

`tono-kiosk.service` 在 tty1 创建普通用户的 PAM/logind 会话，启动带 Xauthority 认证的 Xorg、无合成器的 Openbox、Fcitx5 和用户音频服务。启动命令为：

```sh
QT_QPA_PLATFORM=xcb QT_XCB_GL_INTEGRATION=xcb_egl \
  tono-canvas --fullscreen --opengl-es --low-latency
```

App 的 X11/X Shape 原生墨迹路径保持可用。A1 的 Mesa Panfrost 提供 Mali-G57 GLES 3.1；Quick 与 WebEngine 显式使用 GLES 3/EGL，共用硬件纹理。加速覆盖窗口/纹理/HTML/WebGL 合成，原有 C 引擎笔刷计算和原生低延迟 RasterSurface 仍由 CPU 执行，不把它们描述为 GPU 笔刷。

`/etc/default/tono-kiosk` 默认 `TONO_RENDERER=auto`。启动前以服务用户执行最长 8 秒的 GLES 3 着色器和像素读回测试，仅在确认 Mali-G57/Panfrost 硬件后选择 glamor Xorg 配置和 `--opengl-es`。探测失败选择完整的软件 Xorg 配置及原有 `--software-rendering`；硬件 Xorg 或应用启动异常退出时，同次会话再尝试一次软件模式。会话退出时另外记录客户端状态，避免 xinit 把应用的失败退出码吞掉；每次启动前清理旧状态。正常保存退出不触发这次降级。不能检测仍存活但卡住的驱动；这种情况可经 SSH 强制软件模式：

```sh
sudo sed -i 's/^TONO_RENDERER=.*/TONO_RENDERER=software/' /etc/default/tono-kiosk
sudo systemctl restart tono-kiosk
```

恢复自动模式将值改回 `auto`；`hardware` 用于诊断，跳过探测且不自动降级。配置错误直接报错。两套只读 Xorg 配置位于 `/etc/X11/tono-kiosk-{hardware,software}`，均指定 sun4i-drm 为主显示控制器，不依赖 card 编号。`tono-kiosk.service` 的 `SupplementaryGroups=video render` 仅赋予图形会话 DRM 访问权限；rootless Xorg 必须能重新打开主 DRM 节点才能为 DRI3 客户端传递认证 fd。默认用户的全局组成员不增加这些权限。WebEngine 沙盒保持开启；镜像的单应用 AppArmor 配置允许 Chromium 使用用户命名空间，不改变全局策略。实机内核仍须支持 Chromium 使用的命名空间和 seccomp。

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

`tono-wifi.service` 在 `basic.target` 之后加载 AIC8800，与后续启动任务并行，避免阻塞早期 `systemd-modules-load.service`；网络管理与无线功能继续保留。用户音频服务异步启动。硬件模式使用 glamor，软件恢复模式显式设置 `AccelMethod=none`；由同一会话入口同时选择 Xorg 与 App 的渲染方式。

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
python3 tests/test_kiosk_size.py
python3 tests/test_kiosk_gpu.py
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
