#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0
# Called under Buildroot's host fakeroot so tar ownership reaches mke2fs -d.
set -euo pipefail
[[ $# == 4 ]] || exit 2
archive=$1 bsp=$2 image=$3 repo=$4
root=$(mktemp -d "$(dirname -- "$image")/image-root.XXXXXX")
cleanup() { rm -rf -- "$root"; }
trap cleanup EXIT
tar --numeric-owner --same-owner --same-permissions -xf "$archive" -C "$root"
chmod 755 "$root"
python3 "$repo/scripts/buildroot-pack-image.py" --rootfs "$root" --boot "$bsp/boot" \
    --bootloader "$bsp/bootloader-syterkit.bin" --output "$image"
