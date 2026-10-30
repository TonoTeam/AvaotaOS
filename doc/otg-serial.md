# Avaota A1 OTG 串口登录

Avaota A1 / A527 的镜像默认将 OTG 接口配置为 USB CDC ACM 串口，在 `/dev/ttyGS0` 提供账号密码登录。适用于本仓库的 A1 CLI、桌面和 kiosk 镜像。无需网络，不启用免密登录；账户沿用构建器的 `-u`、`-p` 参数，默认是 `avaota` / `avaota`。

用数据线连接开发板 OTG 接口与电脑。Mac 上运行：

```sh
screen /dev/cu.usbmodemA527CONSOLE1 115200
```

按回车显示登录提示。退出 screen：`Ctrl+A`，再按 `K`，确认 `Y`。Linux 主机通常使用 `/dev/ttyACM0`，实际编号随已连接的串口设备变化。串口使用 115200、8N1、无硬件流控。USB 产品名为 `Avaota A527 USB Console`，序列号为 `A527CONSOLE`；多块板同时连接时以主机枚举出的路径为准。

## 开机配置

- `/usr/local/sbin/avaota-otg-serial`：通过 configfs 创建 `avaota_serial` ACM Gadget，绑定 `4100000.udc-controller`，再通过 A527 BSP 的 `usb_device` 接口切换到设备模式。
- `avaota-otg-serial.service`：等待 configfs 挂载和控制器出现，开机启用串口，停止时释放 Gadget 并恢复 OTG 自动检测。
- `serial-getty@ttyGS0.service.d/otg.conf`：在 Gadget 就绪后启动标准 `agetty` 登录；随 OTG 服务一起停止或重启。

不等待数据线插入，开机时未接电脑也会创建串口；电脑接入后进行 USB 枚举。此端口默认用于设备模式。它提供用户态登录，不输出 BootROM、启动器或早期内核日志；这些仍需板载 UART。

板级 `setup_otg` 同时接入 rootfs 创建与最终镜像打包，因此复用旧 rootfs 缓存也会安装最新脚本和服务，并按需补齐 `python3-minimal`。构建期间仅执行 `systemctl enable`，不启动 Gadget，不操作构建机的 USB 控制器。只有 `avaota-a1` 实现此板级钩子。

A1 内核构建强制启用 configfs、ACM、sunxi UDC 和 OTG manager 所需选项；若 Kconfig 依赖使必要选项失效，构建报错。已有内核 `.deb` 缓存不会被此步骤重新编译，复用自定义内核时需确认包含这些功能，或重建内核。完整 kiosk 镜像检查器会验证服务启用状态、脚本内容及 systemd 依赖。

## 维护

```sh
systemctl status avaota-otg-serial.service serial-getty@ttyGS0.service
journalctl -b -u avaota-otg-serial.service -u serial-getty@ttyGS0.service
cat /sys/class/udc/4100000.udc-controller/state
cat /sys/devices/platform/soc@3000000/10.usbc0/otg_role
```

电脑连接并枚举完成时，预期分别显示 `configured` 和 `usb_device`。脚本检测到其他 Gadget 占用该控制器时会报错，不替换已有功能。

需要改用 OTG 的其他功能时，从 SSH 或板载 UART 停用两个服务：

```sh
sudo systemctl disable --now serial-getty@ttyGS0.service avaota-otg-serial.service
```

重新启用：

```sh
sudo systemctl enable --now avaota-otg-serial.service serial-getty@ttyGS0.service
```

## 验证记录

2026-10-30，在 A527 实机 `tono-a527`（Buildroot 2026.08，Linux `5.15.154-ga464bc4feaff`）验证了本目录所述脚本和服务：Mac 枚举为 USB 2.0 high-speed，双向串口数据收发成功，使用普通用户密码登录后确认终端为 `/dev/ttyGS0`。实际重启后约 20 秒串口重新出现，启动 ID 已变化，两个服务均为 enabled / active，再次登录和退出成功。本仓库的服务文件取自这套已验证配置；该记录不等同于已构建和烧录本次修改后的 Ubuntu 镜像。
