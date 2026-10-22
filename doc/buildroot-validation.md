# Buildroot A527 构建验证

验证日期：2026-10-22。目标为 Avaota A1 / 全志 A527，基于 AvaotaOS main 的 c8dff6bd073fa9a0beddc606dc6dd326b17e88a3。

## 本次产物

完整构建在本地 ARM64 Linux 容器内完成，使用 Buildroot 2026.08 内部 GCC 14 工具链。Buildroot 原生核心实际编译完成，PID 1 为原生 systemd；图形应用使用独立的 Ubuntu 24.04 ARM64 兼容运行时。架构、构建步骤及更新限制见 [构建与维护](buildroot.md)。

| 产物 | 字节数 | SHA256 |
| --- | ---: | --- |
| AvaotaOS-buildroot-2026.08-avaota-a1-arm64.img | 1,979,711,488 | dade50e7502172f41c730af030c9654ae9512045bf493e2f3988e555710e1b5a |
| 同名 .img.xz | 410,018,268 | ae9cf4c34b7f2b4f24b11b45dbbe6b263a2951efc036c774a35c1a2796dcbfee |

XZ 完整性检查通过，解压内容的 SHA256 与 IMG 一致。镜像约 1.98 GB，压缩约 410 MB；首版优先保持现有应用依赖和功能，没有做同条件的体积或帧率提升对比。

发布目录还包含镜像分区清单、发布校验清单、解析后的 Buildroot 配置、原生组件清单、BSP 来源清单与运行时保护补丁。构建输入如下：

| 输入 | 版本 / 校验信息 |
| --- | --- |
| Buildroot | 2026.08；官方归档 SHA256 87aaca4164ea9d5c8085854953018263f7963f07c22e73a2a2185cc98c581c34 |
| App | 1.1.12；ARM64 deb SHA256 4ec22d48155074ac8398161a63cd8e2c3287583e4bea98fbb68fed8e49fd8abb |
| 应用兼容运行时 | 494 个包；归档 SHA256 4a0fe51f1e6dfec1ba245352acbf2c37ddb1450263bd04343c5ed31e69f6135d |
| A527 BSP | 5.15.154-ga464bc4feaff；内核 SHA256 c3b2a73f7a68a8d83fbc6e28346c1cf46510330ac03d6d48d0a7af19890f6b2b |
| DTB | SD 检测修正后 SHA256 0c4f28b470e34d0e61e722f50f57628f0f473dc0af9613b8caabc5a9c2758ee7 |

## 镜像离线检查

- Allwinner eGON 校验、MBR 分区布局、FAT 和 ext4 文件系统检查通过；最终镜像分区内容与清单 SHA256 一致。
- MBR 使用非零磁盘签名 0x6281c35a，根分区 PARTUUID 为 6281c35a-02；实际 blkid / libblkid 与 sfdisk 校验磁盘及分区身份，确保首次扩容所需的 PTUUID / PARTUUID 可读；扩容回归确认磁盘签名、分区起点与启动器不变。
- SyterKit 位于 8 KiB，FAT 从 16 MiB 开始，ext4 从 128 MiB 开始。根文件系统保留日志，初始普通用户可用空间为 411,832,320 字节。
- 从最终 ext4 镜像重新提取应用运行时，12,264 个包内普通文件的 SHA256 全部通过，App 二进制与输入一致。
- 最终镜像内 162 个内核模块、53 个固件文件、9 个 FAT 启动文件和 8 KiB 处的启动加载器逐项与准备好的 BSP 校验一致，模块 ABI 为 5.15.154-ga464bc4feaff。
- 最终镜像的 / 属主为 0:0，/home/avaota 为 1000:1000，Fcitx5 配置也属于该用户。原生 PAM、Polkit、音频服务和维护工具存在；Polkit 链接原生 libsystemd。
- 工厂用户目录仅含输入法初始配置；machine-id 为空，未包含 SSH 主机密钥、个人画板、Wi-Fi 密码或设备备份。

## 软件回归

实际复用缓存执行 target-finalize，并通过普通构建用户完成缓存重建。严格比较应用运行时、模块和固件的文件内容、类型、权限及链接，确认 strip / RPATH 处理没有改写这些载荷。

构建、运行时导出、BSP、镜像和服务共 57 项自动化回归全部通过，覆盖损坏输入拒绝、依赖闭包、路径及符号链接、缓存重装、用户数据保留、分区扩容和文件系统打包。

另外从最终 rootfs.tar 启动独立、无外网的 ARM64 systemd 容器，验证真实 Buildroot PID 1：

- systemd 状态为 running，无失败单元；未手动启动的原生 D-Bus、logind、NetworkManager、SSH 和 resolved 自动进入 active，SSH 配置检查通过，DNS stub 监听存在。
- NetworkManager、SSH、resolved、timesyncd 均已启用。测试主动移除 CAP_SYS_TIME，timesyncd 因条件不足跳过，避免修改构建虚拟机时间；实际 NTP 同步仍需联网实机验证。
- 原生 PAM 建立 avaota 会话，使用正式服务的 RootDirectory / 挂载绑定配置后，UID、组和运行时目录正确；命名空间预检与 App 1.1.12 加载通过。
- 兼容运行时中的客户端能启动并连接原生用户 PipeWire、Pulse 协议服务和 WirePlumber，音频套接字正常；此测试没有录音或播放。
- 原生 Polkit 成功映射 PAM / logind 会话及 tono-network 组。无物理 seat 的容器中 subject.local=false，原有受限 Wi-Fi 策略按预期拒绝；真实 seat0 / VT1 上的连接授权仍需实机验证，没有为容器测试放宽规则。

容器只用于验证原生服务与运行时接口，屏蔽了依赖 A1 硬件的启动、扩容和设备初始化单元。

可对最终 rootfs.tar 单独复验默认服务链。此检查读取 fakeroot 已应用 systemd presets 后的产物，核对 DNS、时钟、SSH、NetworkManager 的启动与激活链接，以及原生音频单元和两处 DNS 配置：

~~~sh
TONO_BUILT_ROOTFS_TAR=/absolute/path/output/images/rootfs.tar \
  python3 tests/test_buildroot_services.py \
  ApplianceServiceTests.test_finalized_tar_enables_native_dns_clock_and_ssh -v
~~~

既有 Linux QtTest 在运行时的测试副本中执行，使用 UID 1000、独立挂载根、Xvfb 1280×1400 和软件 OpenGL：

| 测试组 | 通过 | 失败 | 跳过 |
| --- | ---: | ---: | ---: |
| 渲染配置 | 4 | 0 | 0 |
| Wi-Fi 控制器 | 16 | 0 | 0 |
| 正常停止 / 保存 | 7 | 0 | 0 |
| 普通窗口 | 44 | 0 | 34 |
| 低延迟全屏 | 78 | 0 | 0 |
| 合计 | 149 | 0 | 34 |

普通窗口的 34 项跳过由测试自身条件决定；完整逐项日志随本地产物保存。测试库和 Xvfb 只用于测试副本，没有放入发布镜像。

普通用户 USER / PID 命名空间预检通过。实际 QtWebEngine 渲染进程为 UID 1000，拥有独立 USER / PID 命名空间，NoNewPrivs=1、Seccomp=2。普通 chroot 对照无法创建命名空间，因此正式服务使用 systemd 管理的挂载根切换，不采用关闭沙盒的回退方式。

## 仍需 A1 实机验证

本次未烧录 TF 卡，没有将容器验证等同于硬件验收。以下项目尚未在这份 Buildroot 镜像上验证：

- A527 冷启动、HDMI 首选分辨率、Panfrost GPU / HTML-WebGL 硬件加速、手写延迟和温度。
- 物理鼠标 / 触控、文件选择、中文输入、异常重启以及断电后的画板恢复。
- Wi-Fi 扫描及连接真实热点、应用内 IP 显示、DHCP / DNS。
- 实际麦克风、扬声器和摄像头，以及 App 语音对话。既有 Ubuntu 基线中的 3.5 mm 耳机麦克风输入问题不视为已修复。
- 首次启动扩展实际 TF 卡分区、串口维护、保存后正常关机。

保留现有 GPU 栈和音视频组件表示构建兼容范围，不能据此宣称该新系统已通过实机性能或外设验证。
