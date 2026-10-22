#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0
# Build the native core and a complete A1 image; Ubuntu kiosk remains independent.
set -euo pipefail
version=2026.08
archive_sha=87aaca4164ea9d5c8085854953018263f7963f07c22e73a2a2185cc98c581c34
repo=$(cd -- "$(dirname -- "$0")" && pwd)
runtime="" bsp="" output="$repo/build_dir/buildroot-a1" jobs=4
usage() {
    cat <<HELP
Usage: $0 --runtime RUNTIME.tar.gz --bsp PREPARED_BSP_DIR [OPTIONS]
  --output DIR    Build directory (default: build_dir/buildroot-a1)
  --jobs N        Compiler parallelism (default: 4)
  --help          Show help
Prepare inputs using scripts/buildroot-prepare-runtime.sh and
scripts/buildroot-prepare-bsp.py. See doc/buildroot.md.
HELP
}
while (($#)); do
    case "$1" in
        --runtime|--bsp|--output|--jobs)
            [[ $# -ge 2 && -n $2 && $2 != -* ]] || { usage >&2; exit 2; }
            case "$1" in
                --runtime) runtime=$2 ;; --bsp) bsp=$2 ;;
                --output) output=$2 ;; --jobs) jobs=$2 ;;
            esac
            shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done
[[ $(uname -s) == Linux && $jobs =~ ^[1-9][0-9]*$ ]] || {
    echo "Linux and a positive --jobs value are required." >&2; exit 2;
}
[[ -f $runtime && -f $bsp/bsp-manifest.json ]] || {
    echo "A prepared runtime archive and BSP directory are required." >&2; exit 2;
}
runtime=$(realpath "$runtime")
bsp=$(realpath "$bsp")
mkdir -p "$output"
output=$(realpath "$output")
# Buildroot's make variables do not support whitespace in paths.
for path in "$repo" "$runtime" "$bsp" "$output"; do
    [[ $path != *[[:space:]]* ]] || { echo "Paths must not contain whitespace." >&2; exit 2; }
done
for command in make gcc g++ python3 curl tar xz cpio unzip bc rsync patch \
    perl bison flex file mkfs.vfat mcopy mkfs.ext4 e2fsck fsck.vfat debugfs sfdisk blkid; do
    command -v "$command" >/dev/null || { echo "Missing host tool: $command" >&2; exit 2; }
done
source_root=$output/buildroot-$version
build=$output/output
artifacts=$output/artifacts
mkdir -p "$artifacts" "$output/dl"
image=$artifacts/AvaotaOS-buildroot-$version-avaota-a1-arm64.img
[[ ! -e $image && ! -e $image.xz ]] || {
    echo "Image already exists; choose a new --output directory for a new release." >&2; exit 2;
}
archive=$output/buildroot-$version.tar.xz
if [[ ! -f $archive ]]; then
    curl -fL --retry 3 --connect-timeout 20 \
        "https://buildroot.org/downloads/buildroot-$version.tar.xz" -o "$archive.part"
    mv "$archive.part" "$archive"
fi
printf '%s  %s\n' "$archive_sha" "$archive" | sha256sum --check
if [[ ! -d $source_root ]]; then
    tar -xf "$archive" -C "$output"
fi
bash "$repo/buildroot/apply-patches.sh" "$source_root"
export TONO_RUNTIME_ARCHIVE="$runtime" TONO_BSP_DIR="$bsp"
# Persist the chosen build parallelism in the archived resolved configuration.
sed "s/^BR2_JLEVEL=.*/BR2_JLEVEL=$jobs/" \
    "$repo/buildroot/configs/avaota_a1_defconfig" > "$output/avaota_a1_defconfig"
make -C "$source_root" O="$build" BR2_EXTERNAL="$repo/buildroot" \
    BR2_DEFCONFIG="$output/avaota_a1_defconfig" defconfig
# Reuse package/toolchain caches, but run the payload post-build step on every
# build. It replaces only the factory runtime, never /home or device settings.
make -C "$source_root" O="$build" BR2_EXTERNAL="$repo/buildroot" \
    BR2_DL_DIR="$output/dl" BR2_JLEVEL="$jobs"
make -C "$source_root" O="$build" BR2_EXTERNAL="$repo/buildroot" \
    BR2_DL_DIR="$output/dl" BR2_JLEVEL="$jobs" legal-info
"$build/host/bin/fakeroot" -- "$repo/scripts/buildroot-assemble.sh" \
    "$build/images/rootfs.tar" "$bsp" "$image" "$repo"
xz -T "$jobs" -6 -c "$image" > "$image.xz.part"
xz -t "$image.xz.part"
[[ $(xz -dc "$image.xz.part" | sha256sum | cut -d' ' -f1) == \
   $(sha256sum "$image" | cut -d' ' -f1) ]] || {
    echo "Compressed image checksum mismatch." >&2; exit 1;
}
mv "$image.xz.part" "$image.xz"
cp "$build/.config" "$artifacts/buildroot.config"
cp "$build/legal-info/manifest.csv" "$artifacts/native-packages.csv"
cp "$repo/buildroot/patches/0001-preserve-private-runtime-rpath.patch" "$artifacts/"
cp "$bsp/bsp-manifest.json" "$artifacts/"
python3 - "$runtime" "$bsp/bsp-manifest.json" "$archive" "$artifacts" <<'PY'
import hashlib,json,pathlib,sys
def digest(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(4*1024*1024),b""): h.update(chunk)
    return h.hexdigest()
out=pathlib.Path(sys.argv[4])
result={"buildroot":"2026.08","board":"Avaota A1","soc":"Allwinner A527",
        "runtime_kind":"Ubuntu 24.04 ARM64 application compatibility runtime",
        "inputs":{key:digest(path) for key,path in
                  zip(("runtime_archive","bsp_manifest","buildroot_archive"),sys.argv[1:4])},
        "artifacts":{p.name:digest(p) for p in out.iterdir() if p.is_file()}}
(out/"release-manifest.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n")
PY
printf 'Complete image: %s\nCompressed image: %s\n' "$image" "$image.xz"
