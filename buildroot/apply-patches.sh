#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0
# Apply only tracked appliance patches to the verified Buildroot source archive.
set -euo pipefail
[[ $# == 1 && -d $1/support/scripts ]] || {
    echo "Usage: $0 BUILDROOT_SOURCE_DIRECTORY" >&2
    exit 2
}
source_root=$(cd -- "$1" && pwd)
external=$(cd -- "$(dirname -- "$0")" && pwd)
for patch_file in "$external"/patches/*.patch; do
    if patch --dry-run --batch --forward --fuzz=0 -p1 -d "$source_root" \
            -i "$patch_file" >/dev/null 2>&1; then
        patch --batch --forward --fuzz=0 -p1 -d "$source_root" -i "$patch_file"
    elif patch --dry-run --batch --reverse --fuzz=0 -p1 -d "$source_root" \
            -i "$patch_file" >/dev/null 2>&1; then
        : # Already applied in the package/toolchain cache.
    else
        echo "Buildroot patch does not match the pinned source: $patch_file" >&2
        exit 1
    fi
done
