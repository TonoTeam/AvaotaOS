#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0
# Runs on the build host. Never execute a target binary here.
set -euo pipefail
root=${1:?Buildroot target directory required}
board_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$board_dir/../../.." && pwd)
services="$repo/target/services/tono-kiosk"

if [[ -z ${TONO_RUNTIME_ARCHIVE:-} || -z ${TONO_BSP_DIR:-} ]]; then
    echo "Full build requires TONO_RUNTIME_ARCHIVE and TONO_BSP_DIR" >&2
    exit 2
fi
python3 "$repo/scripts/buildroot-install-payload.py" --target "$root" \
    --runtime "$TONO_RUNTIME_ARCHIVE" --bsp "$TONO_BSP_DIR"

install -d "$root/etc/systemd/system/tono-kiosk.service.d" \
    "$root/etc/systemd/system/session-.scope.d" "$root/etc/pam.d" \
    "$root/etc/default" "$root/etc/polkit-1/rules.d" \
    "$root/etc/systemd/system/multi-user.target.wants" \
    "$root/etc/NetworkManager/system-connections" \
    "$root/home/avaota/.config/fcitx5" "$root/opt/tono-runtime"

# Reuse the tested service lifecycle. PAM is opened on the native host, before
# RootDirectory is entered, so logind retains the real device/session identity.
sed 's/^PAMName=login$/PAMName=tono-kiosk/' \
    "$services/tono-kiosk.service" > "$root/etc/systemd/system/tono-kiosk.service"
install -m 644 "$services/session-scope.conf" \
    "$root/etc/systemd/system/session-.scope.d/10-tono-kiosk.conf"
install -m 644 "$services/tono-wifi.service" "$root/etc/systemd/system/tono-wifi.service"
install -m 644 "$services/49-tono-network.rules" "$root/etc/polkit-1/rules.d/"
install -m 644 "$services/fcitx5-profile" "$root/home/avaota/.config/fcitx5/profile"
chmod 700 "$root/etc/NetworkManager/system-connections"
chmod 750 "$root/etc/polkit-1/rules.d"

# These links are independent of host systemctl's version and architecture.
for service in tono-kiosk tono-wifi tono-grow-rootfs NetworkManager; do
    if [[ $service == tono-* ]]; then
        target="../${service}.service"
    else
        target="/usr/lib/systemd/system/${service}.service"
    fi
    ln -sfn "$target" "$root/etc/systemd/system/multi-user.target.wants/${service}.service"
done
ln -sfn /usr/lib/systemd/system/multi-user.target "$root/etc/systemd/system/default.target"
ln -sfn /dev/null "$root/etc/systemd/system/getty@tty1.service"
ln -sfn /dev/null "$root/etc/systemd/system/NetworkManager-wait-online.service"
ln -sfn /dev/null "$root/etc/systemd/system/systemd-networkd-wait-online.service"
ln -sfn /dev/null "$root/etc/systemd/system/systemd-networkd.service"
ln -sfn /dev/null "$root/etc/systemd/system/xorg.service"

# Never ship an image's build identity or host SSH keys to every device.
: > "$root/etc/machine-id"
rm -f "$root"/etc/ssh/ssh_host_*_key "$root"/etc/ssh/ssh_host_*_key.pub
ln -sfn /run/systemd/resolve/stub-resolv.conf "$root/etc/resolv.conf"
