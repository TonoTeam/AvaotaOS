#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0
# Shared build-time helpers. The cached kiosk rootfs never contains the app.

validate_kiosk_profile() {
    if [[ ${TYPE} != kiosk ]]; then
        if [[ -n ${APP_DEB:-} ]]; then
            echo "--app-deb requires -t kiosk." >&2
            return 2
        fi
        return 0
    fi
    if [[ ${BOARD} != avaota-a1 || ${VERSION} != noble ]]; then
        echo "The kiosk profile supports only -b avaota-a1 -v noble." >&2
        return 2
    fi
}

validate_kiosk_app() {
    [[ ${TYPE} == kiosk ]] || return 0
    if [[ ! -f ${APP_DEB:-} ]]; then
        echo "kiosk requires --app-deb pointing to a tono-canvas ARM64 deb." >&2
        return 2
    fi
    APP_DEB="$(cd -- "$(dirname -- "$APP_DEB")" && pwd)/$(basename -- "$APP_DEB")"
    if [[ $(dpkg-deb -f "$APP_DEB" Package) != tono-canvas ||
          $(dpkg-deb -f "$APP_DEB" Architecture) != arm64 ]]; then
        echo "The application package must be tono-canvas, Architecture: arm64." >&2
        return 2
    fi
    if ! dpkg --compare-versions "$(dpkg-deb -f "$APP_DEB" Version)" ge 1.1.5; then
        echo "The GPU kiosk profile requires tono-canvas >= 1.1.5 (--opengl-es)." >&2
        return 2
    fi
}

kiosk_rootfs_signature() {
    local repo=$1
    sha256sum "$repo/os/noble/kiosk-packages.list" "$repo/scripts/mkrootfs.sh" \
        "$repo/boards/avaota-a1.conf" "$repo/scripts/trim-kiosk-rootfs.py" | sha256sum | cut -d' ' -f1
}

compact_kiosk_rootfs() {
    [[ ${TYPE} == kiosk ]] || return 0
    python3 "$2/scripts/trim-kiosk-rootfs.py" "$1"
}

kiosk_image_size() {
    # Account for filesystem metadata/journal, plus at least 256 MiB free
    # before first boot expands the root partition to the installed SD card.
    local payload=$(($1 + $2 + $3)) boot=$4 headroom overhead
    headroom=$((payload / 10))
    (( headroom >= 256 )) || headroom=256
    overhead=$(((payload + 9) / 10 + 128))
    echo $((((boot + payload + headroom + overhead + 15) / 16) * 16))
}

cleanup_kiosk_rootfs() {
    [[ -n ${KIOSK_STAGE:-} && -d ${KIOSK_STAGE} ]] || return 0
    local path
    for path in sys proc dev; do
        if mountpoint -q "$KIOSK_STAGE/$path"; then
            umount "$KIOSK_STAGE/$path" || return 1
        fi
    done
    rm -rf -- "$KIOSK_STAGE"
    KIOSK_STAGE=""
}

prepare_kiosk_rootfs() {
    local workspace=$1
    local repo
    repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
    KIOSK_STAGE=$(mktemp -d "${workspace}/kiosk-rootfs.XXXXXX")
    tar --numeric-owner -xzf "$workspace/rootfs-${VERSION}-${TYPE}.tar.gz" -C "$KIOSK_STAGE"
    # mkrootfs's archive contains the children of /, not its own metadata.
    # cp -a stage/. later copies this mode onto the image's root directory.
    chmod 755 "$KIOSK_STAGE"
    local stage=$KIOSK_STAGE
    # Refresh cached dpkg filters before installing the current App package.
    compact_kiosk_rootfs "$stage" "$repo"
    # Package scripts must not start daemons on the build host.
    if [[ -e $stage/usr/sbin/policy-rc.d ]]; then
        mv "$stage/usr/sbin/policy-rc.d" "$stage/usr/sbin/policy-rc.d.kiosk-build"
    fi
    printf '#!/bin/sh\nexit 101\n' > "$stage/usr/sbin/policy-rc.d"
    chmod 755 "$stage/usr/sbin/policy-rc.d"
    if [[ -e $stage/etc/resolv.conf || -L $stage/etc/resolv.conf ]]; then
        mv "$stage/etc/resolv.conf" "$stage/etc/resolv.conf.kiosk-build"
    fi
    cp -L /etc/resolv.conf "$stage/etc/resolv.conf"
    mount --bind /dev "$stage/dev"
    mount -t proc proc "$stage/proc"
    mount -t sysfs sysfs "$stage/sys"
    cp "$APP_DEB" "$stage/tmp/tono-canvas.deb"
    LC_ALL=C chroot "$stage" apt-get update
    LC_ALL=C DEBIAN_FRONTEND=noninteractive chroot "$stage" \
        apt-get install -y --no-install-recommends /tmp/tono-canvas.deb
    # Resolve the installed executable, including CPack's /usr/local default.
    chroot "$stage" /bin/sh -c 'command -v tono-canvas >/dev/null'
    {
        printf 'Package: tono-canvas\nVersion: %s\n' "$(dpkg-deb -f "$APP_DEB" Version)"
        printf 'SHA256: %s\n' "$(sha256sum "$APP_DEB" | cut -d' ' -f1)"
    } > "$stage/etc/tono-canvas-image"
    rm "$stage/tmp/tono-canvas.deb" "$stage/etc/resolv.conf" "$stage/usr/sbin/policy-rc.d"
    if [[ -e $stage/etc/resolv.conf.kiosk-build || -L $stage/etc/resolv.conf.kiosk-build ]]; then
        mv "$stage/etc/resolv.conf.kiosk-build" "$stage/etc/resolv.conf"
    fi
    if [[ -e $stage/usr/sbin/policy-rc.d.kiosk-build ]]; then
        mv "$stage/usr/sbin/policy-rc.d.kiosk-build" "$stage/usr/sbin/policy-rc.d"
    fi
    chroot "$stage" apt-get clean
    umount "$stage/sys"
    umount "$stage/proc"
    umount "$stage/dev"
    # Also applies when the base archive was produced before compaction.
    compact_kiosk_rootfs "$stage" "$repo"
}

configure_kiosk_boot() {
    [[ ${TYPE} == kiosk ]] || return 0
    # Keep the full kernel journal and serial login, but do not synchronously
    # print every BSP debug line to the 115200-baud console during startup.
    python3 - "$1/boot/extlinux/extlinux.conf" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
original = path.read_text()
result = []
found = False
for line in original.splitlines(keepends=True):
    fields = line.split()
    if fields and fields[0].lower() == 'append':
        found = True
        args = [arg for arg in fields[1:]
                if not arg.startswith(('earlycon=', 'loglevel=', 'initcall_debug=', 'systemd.show_status='))
                and arg != 'quiet']
        args += ['quiet', 'loglevel=3', 'systemd.show_status=auto']
        indent = line[:len(line) - len(line.lstrip())]
        line = indent + 'append ' + ' '.join(args) + '\n'
    result.append(line)
if not found:
    raise SystemExit('Missing extlinux append line; refusing to change boot configuration')
updated = ''.join(result)
if updated != original:
    path.write_text(updated)
PY
}

install_kiosk_session() {
    local root=$1 repo=$2 uid home
    uid=$(chroot "$root" id -u "$SYS_USER")
    home=$(chroot "$root" getent passwd "$SYS_USER" | cut -d: -f6)
    if [[ $uid == 0 || ! $SYS_USER =~ ^[a-z_][a-z0-9_-]*$ || $home != /home/"$SYS_USER" ]]; then
        echo "kiosk requires a non-root user with a /home/<user> home." >&2
        return 2
    fi
    install -d "$root/etc/default" "$root/usr/local/libexec" "$root/usr/local/sbin" \
        "$root/usr/local/share/doc/tono-kiosk" "$root/etc/systemd/system/tono-kiosk.service.d" \
        "$root/etc/apparmor.d" "$root/etc/xdg/tono-kiosk" "$root/etc/polkit-1/rules.d"
    install -m 755 "$repo"/target/services/tono-kiosk/tono-kiosk-{start,session,stop} \
        "$root/usr/local/libexec/"
    install -m 755 "$repo/target/services/tono-kiosk/tono-gpu-probe" "$root/usr/local/libexec/"
    install -m 755 "$repo/target/services/tono-kiosk/tono-hdmi-watch" "$root/usr/local/libexec/"
    if [[ ! -e $root/etc/default/tono-kiosk ]]; then
        printf '# auto probes GLES; software is the recovery mode.\nTONO_RENDERER=auto\n' \
            > "$root/etc/default/tono-kiosk"
    fi
    install -m 755 "$repo/scripts/repair-kiosk-board-index.py" \
        "$root/usr/local/sbin/tono-repair-board-index"
    install -m 644 "$repo/doc/kiosk-recovery.md" \
        "$root/usr/local/share/doc/tono-kiosk/kiosk-recovery.md"
    install -m 644 "$repo/target/services/tono-kiosk/tono-kiosk.service" "$root/etc/systemd/system/"
    install -m 644 "$repo/target/services/tono-kiosk/tono-wifi.service" "$root/etc/systemd/system/"
    install -m 644 "$repo/target/services/tono-kiosk/tono-canvas.apparmor" \
        "$root/etc/apparmor.d/tono-canvas"
    install -m 644 "$repo/target/services/tono-kiosk/openbox.xml" "$root/etc/xdg/tono-kiosk/"
    install -m 644 "$repo/target/services/tono-kiosk/49-tono-network.rules" "$root/etc/polkit-1/rules.d/"
    chroot "$root" groupadd --system --force tono-network
    chroot "$root" usermod --append --groups tono-network "$SYS_USER"
    cat > "$root/etc/systemd/system/tono-kiosk.service.d/user.conf" <<EOF
[Unit]
Wants=user@${uid}.service
After=user@${uid}.service

[Service]
User=${SYS_USER}
Environment=HOME=${home}
Environment=XDG_RUNTIME_DIR=/run/user/${uid}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/${uid}/bus
WorkingDirectory=${home}
EOF
    # Shutdown stops the kiosk before the logind scope holding Xorg and the app.
    install -d "$root/etc/systemd/system/session-.scope.d"
    install -m 644 "$repo/target/services/tono-kiosk/session-scope.conf" \
        "$root/etc/systemd/system/session-.scope.d/10-tono-kiosk.conf"
    # Seed input methods only on a new profile; never replace existing settings.
    if [[ ! -e $root$home/.config/fcitx5/profile ]]; then
        install -d "$root$home/.config/fcitx5"
        install -m 644 "$repo/target/services/tono-kiosk/fcitx5-profile" "$root$home/.config/fcitx5/profile"
        chroot "$root" chown -R "$SYS_USER:$SYS_USER" "$home/.config/fcitx5"
        chroot "$root" chown "$SYS_USER:$SYS_USER" "$home/.config"
    fi
    chroot "$root" systemctl mask getty@tty1.service
    chroot "$root" systemctl set-default multi-user.target
    chroot "$root" systemctl enable tono-kiosk.service
    chroot "$root" systemctl enable tono-wifi.service
}
