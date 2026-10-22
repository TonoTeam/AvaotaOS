# Avaota A1 / 全志 A527 Buildroot 版本

## 范围

这是独立于 Ubuntu kiosk 的首版构建入口。设备仍只运行 Tono Canvas，保留原生笔迹、画板存取、HTML / WebGL、应用内 Wi-Fi、中文输入、语音和摄像头所需组件。

首版使用两部分用户空间：

- **Buildroot 2026.08 原生核心**：glibc、systemd / PAM / logind、udev、NetworkManager / wpa_supplicant / Polkit、PipeWire / WirePlumber / ALSA、SSH、日志和维护工具。
- **Ubuntu 24.04 ARM64 应用兼容运行时**：已验证的 Qt 6.4.2 / WebEngine、Xorg / Openbox、Mesa / Panfrost、Fcitx5、中文字体及多媒体插件，位于 /opt/tono-runtime。从干净基础系统中按已安装包的依赖闭包导出，记录包版本及文件校验和。

Buildroot 2026.08 没有原生 Qt6 WebEngine / Fcitx5 包。首版保留兼容运行时，避免同时更换浏览器引擎、输入法和 GPU 栈；它不是所有组件均从 Buildroot 源码编译的版本，也不承诺仅换基础系统就提升 HTML 帧率。

沿用已验证的 SyterKit、Linux 5.15.154 BSP、设备树和固件。内核使用 A527 BSP 的 CONFIG_AW_DRM_PANFROST。设备树保留固定 SD 卡的检测修正，不修改 HDMI 的首选分辨率。

## 启动与数据

~~~text
SyterKit → A527 BSP → Buildroot systemd
                       ├─ NetworkManager / Wi-Fi / SSH
                       ├─ avaota 用户的 PipeWire / WirePlumber
                       └─ tono-kiosk.service
                            └─ 私有挂载命名空间 + 应用兼容运行时
                                 └─ Xorg + Openbox + Fcitx5 + Tono Canvas
~~~

服务仍以 UID 1000 的普通用户 avaota 运行。RootDirectory、MountAPIVFS 和 PrivateMounts 配合切换根目录；共享原生系统的设备、用户 D-Bus、音频套接字、用户目录和账户信息。兼容运行时不启动自己的 init、网络管理器或系统音频服务。

每次启动 App 前检查普通用户能否创建 USER / PID 命名空间；失败会停止启动并记录错误，不关闭 Chromium 沙盒。自动 GPU 探测、硬件失败后的软件恢复、应用异常重启和正常停止时的保存流程沿用 kiosk 实现。

/home/avaota 位于原生可写根分区，画板、设置和设备身份不写入兼容运行时。NetworkManager 的连接配置和机器身份也保存在原生系统中。首次启动只扩展当前 SD 根分区的大小，校验起始扇区、分区身份和实际 ext4 容量，成功后记录完成标记。

**完整镜像烧录会覆盖卡上的数据。** 已有设备先备份最新画板、设置和网络配置；本构建流程不读取或导入个人画板，也不自动恢复旧备份。

## 构建输入

需要 Linux 构建机，建议至少 4 核、12 GiB 内存和 60 GiB 可用空间。macOS 使用本地 Linux 虚拟机或容器。系统核心支持在 ARM64 / x86_64 Linux 上用内部工具链交叉编译；准备 Ubuntu 兼容运行时还需 ARM64 本机执行能力，或已配置的 ARM64 binfmt / QEMU。

Ubuntu 构建机的工具：

~~~sh
sudo apt-get update
sudo apt-get install -y build-essential make gcc g++ bison flex patch \
  python3 curl wget rsync cpio unzip bc file libncurses-dev \
  xz-utils mtools dosfstools e2fsprogs fdisk util-linux device-tree-compiler
~~~

准备以下输入，使用绝对路径：

1. rootfs-noble-kiosk.tar.gz：现有 Ubuntu kiosk 构建器生成的干净基础系统缓存；不能使用设备数据备份。
2. tono-canvas_VERSION_arm64.deb：Ubuntu 24.04 ARM64 应用包，保留当前 Linux 功能；本次基线为 1.1.12。
3. A1 BSP 缓存：已有经过实机验证的 bootloader-avaota-a1/、bootloader-syterkit.bin、deb-data/image/ 和 deb-data/dtb/。准备器固定核对当前内核 SHA256 和模块 ABI，不接受未经验证的其他内核。

在原有 Linux 构建目录中可整理 BSP 输入：

~~~sh
tar -czf /absolute/path/avaota-a1-bsp-cache.tar.gz \
  bootloader-avaota-a1 bootloader-syterkit.bin \
  deb-data/image deb-data/dtb
~~~

Ubuntu 基础缓存继续由原有 scripts/mkrootfs.sh 或 build_all.sh 的 kiosk 类型生成。新的 Buildroot 镜像不使用 Ubuntu 根文件系统作为 PID 1 系统。

## 构建完整镜像

先在支持 ARM64 执行的 Linux 环境准备应用运行时。此步骤需要 root 用于临时挂载 /dev、/proc、/sys，包脚本禁止启动守护进程；结束后卸载并删除临时副本。

~~~sh
sudo bash scripts/buildroot-prepare-runtime.sh \
  --base-rootfs /absolute/path/rootfs-noble-kiosk.tar.gz \
  --app-deb /absolute/path/tono-canvas_VERSION_arm64.deb \
  --output /absolute/path/tono-runtime-arm64.tar.gz
~~~

准备 BSP。这一步不重新编译内核，不安装 Debian 包：

~~~sh
sudo python3 scripts/buildroot-prepare-bsp.py \
  --bsp-cache /absolute/path/avaota-a1-bsp-cache.tar.gz \
  --firmware target/firmware \
  --output /absolute/path/a1-buildroot-bsp
~~~

随后以普通用户构建系统核心和完整镜像：

~~~sh
bash build_buildroot.sh \
  --runtime /absolute/path/tono-runtime-arm64.tar.gz \
  --bsp /absolute/path/a1-buildroot-bsp \
  --output /absolute/path/tono-buildroot-build \
  --jobs 4
~~~

入口固定校验官方 Buildroot 2026.08 源码归档，使用 GCC 14 和 Linux 5.15 用户空间头文件。随后幂等应用仓库中的 RPATH 排除补丁，并将应用运行时、BSP 模块和固件排除在原生 strip / RPATH 改写之外，保证复用构建缓存时这些文件的校验和保持一致。依赖源码下载采用 Buildroot 包自带校验。APT 准备阶段使用当时软件仓库版本，实际包版本保存在运行时清单中；保留运行时归档和其 SHA256，才能复用同一组应用依赖。

构建输出位于输出目录的 artifacts/：

- AvaotaOS-buildroot-2026.08-avaota-a1-arm64.img：可直接写入整张 TF / SD 卡的完整镜像。
- 同名 .img.xz：压缩镜像，解压校验与 IMG 一致。
- .img.manifest.json：分区、文件系统和镜像校验信息。
- release-manifest.json：构建输入与发布产物 SHA256。
- buildroot.config、native-packages.csv、bsp-manifest.json：系统配置、原生组件和 BSP 来源。
- 0001-preserve-private-runtime-rpath.patch：本次构建使用的运行时与 BSP 文件保留补丁。

Buildroot 原生包的源码、许可证与主机工具清单保存在 output/legal-info/；该目录的范围为本次选择的原生组件。Ubuntu 包版权文件随兼容运行时保留，BSP 二进制的来源及版本由 BSP 清单单独记录。

分区继续使用已验证的 A1 布局：SyterKit 写入 8 KiB；FAT 从 16 MiB 开始，占 112 MiB；ext4 根分区从 128 MiB 开始，保留日志和至少 256 MiB 可用空间。打包使用 mtools / mke2fs，无需 loop 设备；通过 Buildroot 的 fakeroot 保留 UID、权限、硬链接和符号链接。每次打包生成非零 MBR 磁盘签名，并在发布前用 blkid / libblkid 与 sfdisk 校验磁盘及分区身份；签名与 PARTUUID 记录在镜像清单中，供首次扩容核对。

现有输出不会被静默覆盖。发布另一个版本时使用新的输出目录；构建失败后可以在未生成最终 IMG 的原目录继续使用缓存。

## 维护

开发默认账户沿用 avaota / avaota，root 默认密码也是 avaota。部署时按设备管理要求更改密码；SSH 主机密钥和 machine-id 在设备上生成。

~~~sh
ssh avaota@DEVICE_IP
systemctl status tono-kiosk.service
journalctl -b -u tono-kiosk.service -n 100 --no-pager
journalctl -b -t tono-kiosk-start -n 100 --no-pager
nmcli device status
systemctl --user status pipewire.socket pipewire-pulse.socket wireplumber.service
~~~

渲染及调频配置位于原生 /etc/default/tono-kiosk。普通用户会话通过只读绑定读取配置。软件恢复使用 TONO_RENDERER=software，默认 auto；默认动态 CPU 调频不更改频率上下限。

Buildroot 根系统不提供 Ubuntu 的 apt 更新流程。App 及 Qt 依赖通过重新生成兼容运行时并集成新镜像更新。实际设备若需要单独替换运行时，必须先备份最新用户数据、正常停止 kiosk、验证新运行时，并保留失败恢复入口；本首版没有提供在线 OTA。

## 验证范围

主机回归覆盖运行时包依赖及文件校验、私有数据排除、挂载根切换前提、PAM / 网络权限、保存停止顺序、安全扩容、BSP / DTB / 模块、完整分区打包和 FAT / ext4 检查。

实际构建结果、软件功能回归和未验证硬件项目记录在 [Buildroot 构建验证](buildroot-validation.md)。设备上的 HDMI / GPU、物理输入、Wi-Fi 连接、麦克风、扬声器和摄像头需要独立实机验证；不能用容器结果代替。
