# Kiosk 黑屏排查与系统文件恢复

适用于 Avaota A1、Ubuntu 24.04 ARM64 的 Tono Canvas kiosk。先保留串口和 SSH 维护通道，再判断启动失败发生在哪一层。以下记录来自 2026-10-16 的一次实机恢复；设备专用脚本、安装包、画板备份和原始日志仅用于本机维护。

## 判断失败位置

使用串口确认 Linux 是否到达登录提示；可以 SSH 时先读取服务状态和日志：

```sh
systemctl status tono-kiosk.service
journalctl -b -u tono-kiosk.service -n 100 --no-pager
journalctl -b -t tono-kiosk-start -n 100 --no-pager
systemctl status wpa_supplicant.service
getent group tty video render netdev polkitd tono-network
od -An -tx1 -N4 /usr/bin/tono-canvas
dpkg-query -W tono-canvas polkitd 2>/tmp/tono-dpkg-error.log || head -c 512 /tmp/tono-dpkg-error.log
```

正常 ARM64 ELF 程序的前四个字节为 `7f 45 4c 46`。若应用在 Shell 中报二进制内容的语法错误，先检查文件完整性。`216/GROUP`、`Failed to determine group credentials` 或多个基础组无法解析，表示还需检查 `/etc/group`、`/etc/gshadow` 与 NSS 配置。

本次故障中，Linux 可以启动，Xorg 可以创建会话，但 App 无法执行，WPA supplicant 因组解析失败而启动失败。确认损坏的文件包括：

- `/etc/group`、`/etc/gshadow`。
- `/usr/bin/tono-canvas`。
- `/var/lib/dpkg/status`。
- `/var/lib/dpkg/info/tono-canvas.list`、`tono-canvas.md5sums`。

损坏内容包含大量 `0xff`，应用文件长度仍与原程序相同。仅检查文件是否存在或大小是否正确不足以发现此问题。文件损坏的来源尚未确定，此记录不能证明 SD 卡硬件、电源或某个软件更新流程是原因。

## 恢复顺序

1. **备份并校验。** 保留异常系统文件、当前启动配置和已有画板、设置。备份权限应限制为维护用户可读；画板备份可能包含个人内容，不进入 Git 或公共日志。准备与该设备系统和架构一致、已校验的参考文件与应用包。
2. **恢复系统组。** 优先检查设备自己的 `group-`、`gshadow-` 或备份文件，校验组名、GID 和成员。保留现有用户名、UID/GID、家目录和登录凭据，确保 kiosk 用户仍属于 `tono-network`。本机备份也损坏时，才根据已验证的同版本参考文件进行人工恢复，并核对设备新增的组和成员。
3. **恢复可执行程序。** 应用包已校验、软件包数据库暂不可用时，可先用 `dpkg-deb --extract` 提取可信 App 恢复诊断能力；后续仍需通过正常包安装恢复安装记录和其他资源。写入使用同目录临时文件、原子替换和文件及目录同步，再读取校验值。
4. **恢复软件包管理。** 先在临时 `--admindir` 中用 `dpkg-query` 检查 `status-old` 或 `/var/backups/dpkg.status.*`，核对已安装的软件包集合及版本。备份当前损坏数据库和 App 元数据后，再从有效备份恢复数据库，并重新安装已校验的 App 包。本次保留了 584 个包的安装记录，将 App 从备份记录的 1.1.3 正常重装为 1.1.4。不可用参考镜像的整个数据库直接覆盖已维护过的设备。
5. **核对完整性和权限。** 检查 `dpkg --audit`、App 的 `dpkg --verify`、NetworkManager/WPA supplicant，以及普通用户 App 的四项 Wi-Fi 权限。全系统 `dpkg --verify` 的输出需与同版本镜像布局比较：该镜像将内核和 DTB 安装到启动器使用的路径，因此原镜像也报告 46 项旧包路径缺失。
6. **恢复正常启动并验证。** 移除临时启动参数，恢复原始 ramdisk 配置，同步文件系统，记录关键文件哈希；正常关机后断电再上电，重新核对哈希、App 全屏及 Wi-Fi 自动连接。确认后清理本次临时修复入口，保留故障备份和维护记录。

如果没有网络、串口只有接收线，可在 SD 启动分区准备一次性恢复 ramdisk。恢复钩子在 initramfs 已挂载真实根分区、尚未启动 systemd 时运行，挂载启动分区并绑定所需虚拟文件系统，执行经过哈希校验的修复负载。成功后恢复正常启动配置，卸载分区并将根分区重新挂为只读，再重启；失败时保持串口可见的维护状态，避免自动反复写入。

本次单独设置 `systemd.run=` 没有触发修复，之后改用上述 initramfs 入口完成。必须通过修复日志和恢复后的文件校验确认执行成功，不能只凭启动参数已写入判断。早期启动时系统时钟可能尚未校准，备份目录可能带有 1970 年时间戳，应结合报告与日志识别备份。

## 首页新建提示“无法打开画板”

应用恢复显示后，还需验证实际新建、存取和旧画板打开。文件在修复前后未变，并不表示损坏前的内容仍然可读。

本次进一步确认两份 `nvs/bb_boards.boardN` 索引的全部 24 字节均为 `0xff`。引擎因此读到最大的删除代数，屏蔽现有存档，并拒绝新建时写入该槽位。原始 `boards-v2.bin` 中，两张画板仍各有通过文件头、缩略图和存档 CRC 校验的副本。

[`scripts/repair-kiosk-board-index.py`](../scripts/repair-kiosk-board-index.py) 可处理这个已确认的损坏模式。默认只检查；`--apply` 要求 App 已停止，会先备份原索引，再使用各槽位最近的有效副本重建索引，保留所有原始存档字节和文件所有者。已有的正常删除记录保持原样。没有有效副本或检查后存档发生变化时拒绝写入。

```sh
python3 scripts/repair-kiosk-board-index.py \
  --data-dir /home/avaota/.local/share/TonoTeam/tono-canvas
sudo systemctl stop tono-kiosk.service
sudo python3 scripts/repair-kiosk-board-index.py \
  --data-dir /home/avaota/.local/share/TonoTeam/tono-canvas --apply
sudo systemctl start tono-kiosk.service
```

自定义用户名时调整 `--data-dir`。备份和报告位于数据目录内权限为 `0700` 的 `.index-recovery-*` 目录。损坏索引中的原编辑时间与删除状态已无法确认，恢复后编辑时间记为未知，并可能重新显示原本被删除的画板。工具仅支持此 Linux 版的 2048×2560 存档、每副本 4 MiB 的布局；其余损坏模式需要单独分析。

## 本次验证范围

- 在完整镜像副本中模拟损坏的用户组、NSS、App、软件包数据库和 App 元数据，验证备份回退、参考文件回退、重装及画板保留；另验证 initramfs 的挂载、同步、卸载和正常启动配置恢复流程。
- 实机修复前后 12 个已有画板和设置文件校验未变；App 恢复显示，四项 Wi-Fi 权限均通过。
- 后续发现画板索引原本已损坏；根据有效存档副本重建两份索引，修复前后原始画板存档哈希一致。用户确认能新建、画一笔、保存并重新打开。旧画板的逐张内容验收尚未完成。
- 索引恢复工具的六项回归覆盖最近有效副本、损坏副本回退、正常删除标记保留、缺少有效副本或存档变化时拒绝写入、符号链接拒绝，以及 App 仍运行时拒绝修复。
- 实机全系统软件校验与参考镜像结果一致，未发现额外软件文件差异。
- 正常关机后完成一次断电启动，10 个关键文件哈希未变，App 自动全屏，Wi-Fi 自动重连，服务重启次数为 0，无失败单元。用户确认屏幕再次显示 Tono Canvas。

本次记录验证了恢复后的已保存文件和启动流程，尚未验证运行中直接断电的可靠性、新笔迹强制断电恢复、长期反复断电或存储介质寿命。维护时观察到图形会话停止后的 `xinit` 返回状态 1；关机完成，后续启动和关键文件校验通过。2026-10-20 查明原因是停止时的 `ExecStop=` 挂断 tty1，xinit 收到 SIGHUP 后提前停止 Xorg；停止流程已改为由会话主进程转发 SIGTERM，见 [kiosk.md](kiosk.md)。
