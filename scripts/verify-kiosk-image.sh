#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0
# Read-only verification of a complete Avaota A1 kiosk SD image on Linux.
set -euo pipefail

if [[ $# != 1 || $(uname -s) != Linux || $EUID != 0 ]]; then
    echo "Usage (Linux, root): $0 /path/to/AvaotaOS-kiosk.img" >&2
    exit 2
fi
image=$(realpath "$1")
repo=$(cd -- "$(dirname -- "$0")/.." && pwd)
test -f "$image"
verify_root=$(mktemp -d)
loop=""
cleanup() {
    local status=$?
    trap - EXIT
    if mountpoint -q "$verify_root/rootfs/tmp"; then umount "$verify_root/rootfs/tmp" || exit 1; fi
    if mountpoint -q "$verify_root/boot"; then umount "$verify_root/boot" || exit 1; fi
    if mountpoint -q "$verify_root/rootfs"; then umount "$verify_root/rootfs" || exit 1; fi
    if [[ -n $loop ]]; then
        kpartx -d "$loop"
        losetup -d "$loop"
    fi
    rm -rf "$verify_root"
    exit "$status"
}
trap cleanup EXIT
mkdir "$verify_root/boot" "$verify_root/rootfs"
loop=$(losetup --find --show --partscan --read-only "$image")
[[ $loop =~ ^/dev/loop[0-9]+$ ]]
# Device-mapper also works in builders without a running udev daemon, where
# losetup --partscan does not create /dev/loopNp1 inside the container.
kpartx -ar "$loop"
boot_device="/dev/mapper/${loop##*/}p1"
root_device="/dev/mapper/${loop##*/}p2"
udevadm settle
fsck.vfat -n "$boot_device"
e2fsck -fn "$root_device"
mount -o ro "$boot_device" "$verify_root/boot"
mount -o ro,noload "$root_device" "$verify_root/rootfs"
boot=$verify_root/boot
root=$verify_root/rootfs
test "$(stat -c '%a' "$root")" = 755
test "$(blkid -s LABEL -o value "$boot_device")" = boot
test "$(blkid -s LABEL -o value "$root_device")" = rootfs
test -s "$boot/Image"
test -s "$boot/uInitrd"
test -s "$boot/dtb/allwinner/sun55i-t527-avaota-a1.dtb"
sd_dtb="$boot/dtb/allwinner/sun55i-t527-avaota-a1.dtb"
sd_node=/soc@3000000/sdmmc@4020000
fdtget "$sd_dtb" "$sd_node" non-removable >/dev/null
sd_properties=$(fdtget -p "$sd_dtb" "$sd_node")
! grep -qx cd-gpios <<< "$sd_properties"
grep -q 'fdt /dtb/allwinner/sun55i-t527-avaota-a1.dtb' "$boot/extlinux/extlinux.conf"
grep -q 'root=LABEL=rootfs' "$boot/extlinux/extlinux.conf"
grep -q 'loglevel=3' "$boot/extlinux/extlinux.conf"
! grep -q 'earlycon=' "$boot/extlinux/extlinux.conf"
grep -qx 'BOARD=avaota-a1' "$root/etc/avaota-release"
grep -qx 'DISTRIBUTION=noble' "$root/etc/avaota-release"
grep -qx 'DESKTOP=kiosk' "$root/etc/avaota-release"
test "$(systemctl --root="$root" get-default)" = multi-user.target
test "$(systemctl --root="$root" is-enabled tono-kiosk.service)" = enabled
test "$(systemctl --root="$root" is-enabled tono-wifi.service)" = enabled
test "$(systemctl --root="$root" is-enabled getty@tty1.service || true)" = masked
test ! -e "$root/etc/systemd/system/display-manager.service"
systemd-analyze --root="$root" verify tono-kiosk.service tono-wifi.service

user=$(sed -n 's/^User=//p' "$root/etc/systemd/system/tono-kiosk.service.d/user.conf")
uid=$(chroot "$root" id -u "$user")
test "$uid" != 0
chroot "$root" id -nG "$user" | tr ' ' '\n' | grep -qx tono-network
test "$(chroot "$root" dpkg-query -W -f='${db:Status-Status}' polkitd)" = installed
cmp "$repo/target/services/tono-kiosk/49-tono-network.rules" \
    "$root/etc/polkit-1/rules.d/49-tono-network.rules"
test "$(stat -c '%u' "$root/home/$user")" = "$uid"
test "$(stat -c '%u' "$root/home/$user/.config/fcitx5/profile")" = "$uid"
grep -qx "Environment=XDG_RUNTIME_DIR=/run/user/$uid" \
    "$root/etc/systemd/system/tono-kiosk.service.d/user.conf"
grep -q 'MatchDriver "sun4i-drm"' "$root/etc/X11/xorg.conf.d/20-avaota-display.conf"
test ! -e "$root/etc/modules-load.d/avaota-wifi.conf"
grep -qx 'ExecStart=/usr/sbin/modprobe aic8800_fdrv' "$root/etc/systemd/system/tono-wifi.service"
grep -q 'Option "AccelMethod" "none"' "$root/etc/X11/tono-kiosk-software/21-tono.conf"
grep -q 'Option "AccelMethod" "glamor"' "$root/etc/X11/tono-kiosk-hardware/21-tono.conf"
grep -qx 'TONO_RENDERER=auto' "$root/etc/default/tono-kiosk"
grep -qx 'SupplementaryGroups=video render' "$root/etc/systemd/system/tono-kiosk.service"
test -x "$root/usr/local/libexec/tono-gpu-probe"
test "$(chroot "$root" dpkg-query -W -f='${db:Status-Status}' libgles2)" = installed
test -d "$root/lib/firmware"
test -e "$root/usr/lib/systemd/user/pipewire.socket"
test -e "$root/usr/lib/systemd/user/pipewire-pulse.socket"
test -e "$root/usr/lib/systemd/user/wireplumber.service"
test -e "$root/usr/lib/systemd/user/dbus.socket"
test -f "$root/etc/pam.d/login"
for script in start session stop; do
    test -x "$root/usr/local/libexec/tono-kiosk-$script"
    bash -n "$root/usr/local/libexec/tono-kiosk-$script"
done
grep -q -- '--opengl-es' "$root/usr/local/libexec/tono-kiosk-session"
grep -q -- '--software-rendering' "$root/usr/local/libexec/tono-kiosk-session"
test -x "$root/usr/lib/xorg/Xorg"
test -x "$root/usr/bin/xinit"
test -x "$root/usr/bin/fcitx5"
test -x "$root/usr/bin/openbox"
app=$(chroot "$root" /bin/sh -c 'command -v tono-canvas')
chroot "$root" /usr/bin/ldd "$app" > "$verify_root/app-libraries.txt"
if grep -q 'not found' "$verify_root/app-libraries.txt"; then
    cat "$verify_root/app-libraries.txt" >&2
    exit 1
fi
# Allow Qt runtime/cache scratch files without modifying the image or user data.
mount -t tmpfs -o mode=1777 tmpfs "$root/tmp"
chroot "$root" /usr/sbin/runuser -u "$user" -- env \
    QT_QPA_PLATFORM=offscreen XDG_CACHE_HOME=/tmp/tono-verify-cache \
    "${app}" --software-rendering --version
umount "$root/tmp"
audit=$(chroot "$root" dpkg --audit)
test -z "$audit" || { printf '%s\n' "$audit" >&2; exit 1; }
if [[ -f $root/etc/dpkg/dpkg.cfg.d/01-tono-kiosk-size ]]; then
    # Compaction must leave working upgrades, CJK/IME, GL and browser assets.
    test ! -s "$root/var/cache/apt/pkgcache.bin"
    test ! -s "$root/var/cache/apt/srcpkgcache.bin"
    test -z "$(find "$root/var/lib/apt/lists" -type f -print -quit)"
    test "$(df -Pm "$root" | awk 'NR == 2 { print $4 }')" -ge 256
    test "$(df -Pm "$boot" | awk 'NR == 2 { print $4 }')" -ge 32
    test "$(systemctl --root="$root" is-enabled init-resize.service)" = enabled
    chroot "$root" tono-repair-board-index --help >/dev/null
    for package in apt network-manager openssh-server fonts-noto-cjk fcitx5-pinyin \
        libqt6webenginecore6 libgl1-mesa-dri pipewire wireplumber; do
        test "$(chroot "$root" dpkg-query -W -f='${db:Status-Status}' "$package")" = installed
    done
    test -s "$root/usr/share/doc/apt/copyright"
    test -s "$root/usr/local/share/doc/tono-kiosk/kiosk-recovery.md"
    chroot "$root" apt-config dump | grep -qx 'Dir::Cache::pkgcache "";'
    chroot "$root" apt-config dump | grep -qx 'Dir::Cache::srcpkgcache "";'
fi
dpkg-query --admindir="$root/var/lib/dpkg" -W \
    -f='${binary:Package} ${Version} ${db:Status-Status}\n' \
    tono-canvas xserver-xorg-core openbox fcitx5 pipewire wireplumber openssh-server \
    linux-image-sun55i-t527-bsp
for package in gdm3 gnome-shell ubuntu-desktop; do
    state=$(dpkg-query --admindir="$root/var/lib/dpkg" -W \
        -f='${db:Status-Status}' "$package" 2>/dev/null || true)
    test "$state" != installed
done
# Keep the namespace and seccomp facilities needed by Chromium's sandbox.
config=$(find "$boot" -maxdepth 1 -name 'config-*' -print -quit)
test -n "$config"
for option in USER_NS NAMESPACES SECCOMP SECCOMP_FILTER; do
    grep -qx "CONFIG_${option}=y" "$config"
done
python3 - "$image" "$root" <<'PY'
import pathlib
import struct
import sys

image = pathlib.Path(sys.argv[1])
with image.open('rb') as stream:
    mbr = stream.read(512)
    assert mbr[510:512] == b'\x55\xaa', 'Missing MBR signature'
    assert mbr[450] == 0x0c and mbr[466] == 0x83, 'Unexpected partition types'
    if (pathlib.Path(sys.argv[2]) / 'etc/dpkg/dpkg.cfg.d/01-tono-kiosk-size').is_file():
        boot_start, boot_sectors = struct.unpack_from('<II', mbr, 454)
        root_start, = struct.unpack_from('<I', mbr, 470)
        assert boot_start == 32768, 'BootROM gap must remain 16 MiB'
        assert root_start == 262144 and boot_start + boot_sectors == root_start, 'Expected compact boot layout'
    stream.seek(8192)
    header = stream.read(32)
    assert header[4:12] == b'eGON.BT0', 'Missing Allwinner BootROM header'
    checksum, size = struct.unpack_from('<II', header, 12)
    assert size % 4 == 0 and 0 < size < 16 * 1024 * 1024 - 8192
    stream.seek(8192)
    loader = bytearray(stream.read(size))
    struct.pack_into('<I', loader, 12, 0x5f0a6c39)
    assert sum(struct.unpack(f'<{size // 4}I', loader)) & 0xffffffff == checksum
print('MBR and Allwinner BootROM checksum verified; image bytes:', image.stat().st_size)
PY
cat "$root/etc/tono-canvas-image"
cat "$boot/extlinux/extlinux.conf"
printf 'Kiosk image verification passed (read-only; hardware boot not tested).\n'
