#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0
# Export only the application dependency closure from a clean Ubuntu ARM64 base.
set -euo pipefail
usage() {
    echo "Usage: $0 --base-rootfs BASE.tar.gz --app-deb APP.deb --output RUNTIME.tar.gz"
}
base="" app="" output=""
while (($#)); do
    case "$1" in
        --base-rootfs|--app-deb|--output)
            [[ $# -ge 2 && -n $2 && $2 != -* ]] || { usage >&2; exit 2; }
            case "$1" in
                --base-rootfs) base=$2 ;; --app-deb) app=$2 ;; --output) output=$2 ;;
            esac
            shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ $(uname -s) == Linux && $EUID == 0 ]] || {
    echo "Run as root on Linux; ARM64 execution (native or binfmt) is required." >&2; exit 2;
}
[[ -f $base && -f $app && -n $output && ! -e $output ]] || {
    echo "Existing base/App inputs and a new output filename are required." >&2; exit 2;
}
repo=$(cd -- "$(dirname -- "$0")/.." && pwd)
base=$(realpath "$base")
app=$(realpath "$app")
output=$(realpath -m "$output")
mkdir -p -- "$(dirname -- "$output")"
work=$(mktemp -d "$(dirname -- "$output")/runtime-prepare.XXXXXX")
source_root=$work/source
runtime_root=$work/runtime
mounted=()
cleanup() {
    local entry index
    for ((index = ${#mounted[@]} - 1; index >= 0; index--)); do
        entry=${mounted[index]}
        if mountpoint -q "$source_root/$entry"; then
            umount "$source_root/$entry" || return 1
        fi
    done
    rm -rf -- "$work"
}
trap cleanup EXIT
mkdir -p "$source_root"
# Base images are build inputs, never device backups. The exporter additionally
# excludes personal data, credentials, machine identity and network profiles.
python3 - "$base" <<'PY'
import pathlib, sys, tarfile
with tarfile.open(sys.argv[1]) as archive:
    for item in archive:
        path = pathlib.PurePosixPath(item.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit("Unsafe base archive member: " + item.name)
PY
python3 - "$repo" "$base" "$source_root" <<'SAFE'
import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location("payload",pathlib.Path(sys.argv[1])/"scripts/buildroot-install-payload.py")
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.safe_extract(pathlib.Path(sys.argv[2]),pathlib.Path(sys.argv[3]),allow_devices=True)
SAFE
chmod 755 "$source_root"
# Subsequent host-side writes/mounts must not follow guest absolute aliases.
# These directories are real directories in the supported Ubuntu 24.04 base.
for directory in etc usr usr/sbin dev proc sys tmp; do
    if [[ -L $source_root/$directory ||
          ( -e $source_root/$directory && ! -d $source_root/$directory ) ]]; then
        echo "Unsafe base staging directory: $directory" >&2
        exit 2
    fi
done
mkdir -p "$source_root/dev" "$source_root/proc" "$source_root/sys" "$source_root/tmp"
[[ -f $source_root/etc/os-release ]] && \
    grep -qx 'VERSION_ID="24.04"' "$source_root/etc/os-release" || {
    echo "The compatibility runtime requires Ubuntu 24.04 ARM64." >&2; exit 2;
}
[[ $(chroot "$source_root" dpkg --print-architecture) == arm64 ]] || exit 2
rm -f "$source_root/usr/sbin/policy-rc.d"
printf '#!/bin/sh\nexit 101\n' > "$source_root/usr/sbin/policy-rc.d"
chmod 755 "$source_root/usr/sbin/policy-rc.d"
rm -f "$source_root/etc/resolv.conf"
cp -L /etc/resolv.conf "$source_root/etc/resolv.conf"
mounted+=(dev)
mount --bind /dev "$source_root/dev"
mounted+=(proc)
mount -t proc proc "$source_root/proc"
mounted+=(sys)
mount -t sysfs sysfs "$source_root/sys"
rm -f "$source_root/tmp/tono-canvas-buildroot.deb"
cp "$app" "$source_root/tmp/tono-canvas-buildroot.deb"
chroot "$source_root" apt-get update
read -r -a seeds < <(python3 "$repo/scripts/buildroot-runtime.py" --default-seeds)
packages=()
for package in "${seeds[@]}"; do
    [[ $package == tono-canvas ]] || packages+=("$package")
done
DEBIAN_FRONTEND=noninteractive LC_ALL=C chroot "$source_root" \
    apt-get install -y --no-install-recommends /tmp/tono-canvas-buildroot.deb "${packages[@]}"
chroot "$source_root" dpkg --audit
python3 "$repo/scripts/buildroot-runtime.py" --source "$source_root" \
    --destination "$runtime_root" --app-deb "$app"
install -d "$runtime_root/usr/local/libexec" "$runtime_root/etc/xdg/tono-kiosk"
install -m 755 "$repo"/target/services/tono-kiosk/tono-kiosk-{start,session,stop} \
    "$repo/target/services/tono-kiosk/tono-gpu-probe" "$runtime_root/usr/local/libexec/"
install -m 755 "$repo/buildroot/board/avaota-a1/runtime-check.py" \
    "$runtime_root/usr/local/libexec/tono-sandbox-preflight"
install -m 644 "$repo/target/services/tono-kiosk/openbox.xml" \
    "$runtime_root/etc/xdg/tono-kiosk/openbox.xml"
(
    ROOTFS=$runtime_root VERSION=noble TYPE=kiosk KERNEL_TARGET=bsp
    source "$repo/boards/avaota-a1.conf"
    setup_display
)
# Never disable Chromium's sandbox as a fallback.
install -d "$runtime_root/home" "$runtime_root/run" "$runtime_root/tmp" \
    "$runtime_root/dev" "$runtime_root/proc" "$runtime_root/sys" \
    "$runtime_root/etc/default"
chmod 1777 "$runtime_root/tmp"
touch "$runtime_root/etc/hosts" "$runtime_root/etc/passwd" "$runtime_root/etc/group" \
    "$runtime_root/etc/machine-id" "$runtime_root/etc/default/tono-kiosk"
rm -f "$runtime_root/etc/resolv.conf"
ln -s /run/NetworkManager/resolv.conf "$runtime_root/etc/resolv.conf"
python3 - "$runtime_root" "$base" "$app" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
def digest(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b""): h.update(chunk)
    return h.hexdigest()
inputs={"ubuntu_base_sha256":digest(sys.argv[2]),"app_deb_sha256":digest(sys.argv[3]),
        "profile":"Ubuntu 24.04 ARM64 application compatibility runtime",
        "sandbox":"required: user namespaces and seccomp"}
(root/"tono-runtime-inputs.json").write_text(json.dumps(inputs,indent=2)+"\n")
PY
tar --numeric-owner --sort=name -czf "$work/runtime.tar.gz" -C "$runtime_root" .
gzip -t "$work/runtime.tar.gz"
ln "$work/runtime.tar.gz" "$output"
sha256sum "$output"
