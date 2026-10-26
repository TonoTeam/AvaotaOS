#!/usr/bin/env bash
# Install an offline archive as the logged-in ordinary user, without sudo.
set -euo pipefail
if [[ $# -lt 1 || $# -gt 2 || (${2:-} != '' && ${2:-} != --start) ]]; then
    echo "Usage: $0 BUNDLE.tar.gz [--start]" >&2
    exit 2
fi
if [[ $(uname -s) != Linux || $(uname -m) != aarch64 || $(id -u) == 0 ]]; then
    echo "Run as the ordinary device user on Linux ARM64 (do not use sudo)." >&2
    exit 2
fi
archive=$(readlink -f -- "$1")
release=deepseek-harness-0.2.1-rc.2-node24.22.1-kiosk3-linux-arm64
read -r expected _ < "$archive.sha256"
[[ $expected =~ ^[0-9a-f]{64}$ ]] || { echo 'Invalid archive checksum.' >&2; exit 1; }
actual=$(sha256sum "$archive")
[[ ${actual%% *} == "$expected" ]] || { echo 'Archive checksum mismatch.' >&2; exit 1; }
base="$HOME/.local/opt/deepseek-harness"
destination="$base/releases/$release"
launcher="$HOME/.local/bin/tono-dsh"
unit="$HOME/.config/systemd/user/deepseek-harness.service"
for existing in "$launcher" "$unit"; do
    if [[ -e "$existing" ]] && ! grep -q '^# Tono DeepSeek Harness' "$existing"; then
        echo "Refusing to replace an unrelated file: $existing" >&2
        exit 1
    fi
done
if [[ -e "$base/current" && ! -L "$base/current" ]]; then
    echo "Expected a managed symlink at $base/current" >&2
    exit 1
fi
mkdir -p "$base/releases" "$(dirname "$launcher")" "$(dirname "$unit")"
staging=$(mktemp -d "$base/releases/.install.XXXXXX")
trap 'rm -rf -- "$staging"' EXIT
if [[ -e "$destination" ]]; then
    [[ $(cat "$destination/.archive-sha256") == "$expected" ]] || {
        echo "This release already exists with different contents; keep it for rollback." >&2
        exit 1
    }
    (cd "$destination" && sha256sum -c SHA256SUMS > /dev/null)
else
    # Extract directly on the destination filesystem: no second 1 GiB copy on a TF card.
    python3 - "$archive" "$staging" "$release" <<'PY'
import os, pathlib, shutil, sys, tarfile
archive, staging, release = sys.argv[1:]
with tarfile.open(archive, 'r:gz') as tf:
    members = tf.getmembers()
    for member in members:
        p = pathlib.PurePosixPath(member.name)
        if p.is_absolute() or '..' in p.parts or not p.parts or p.parts[0] != release:
            raise SystemExit('Invalid bundle member: ' + member.name)
    fs = os.statvfs(staging)
    block = fs.f_frsize or fs.f_bsize
    required = sum(((m.size + block - 1) // block) * block if m.isfile()
                   else block for m in members) + 256 * 1024 * 1024
    if shutil.disk_usage(staging).free < required:
        raise SystemExit('Insufficient free space for installation plus 256 MiB reserve')
    # Growing an ext4 partition does not guarantee enough inodes for npm trees.
    if fs.f_files and fs.f_favail < len(members) + 4096:
        raise SystemExit('Insufficient free inodes for installation plus 4096 reserve')
    print(f'Extracting {len(members)} entries; capacity and inode checks passed.', flush=True)
    tf.extractall(staging, filter='data')
PY
    candidate="$staging/$release"
    (cd "$candidate" && sha256sum -c SHA256SUMS > /dev/null)
    DSH_HOME="$staging/check-home" "$candidate/bin/tono-dsh" --version
    printf '%s\n' "$expected" > "$candidate/.archive-sha256"
    mv -- "$candidate" "$destination"
fi
# Preserve the previous release pointer and changed service for manual rollback.
if [[ -L "$base/current" ]] && [[ $(readlink "$base/current") != "releases/$release" ]]; then
    ln -s "$(readlink "$base/current")" "$staging/previous"
    mv -Tf -- "$staging/previous" "$base/previous"
fi
if [[ -f "$unit" ]] && ! cmp -s "$destination/deepseek-harness.service" "$unit"; then
    cp -p "$unit" "$unit.backup.$(date +%Y%m%d%H%M%S)"
fi
ln -s "releases/$release" "$staging/current"
mv -Tf -- "$staging/current" "$base/current"
cat > "$staging/tono-dsh" <<'SH'
#!/bin/sh
# Tono DeepSeek Harness launcher
exec "$HOME/.local/opt/deepseek-harness/current/bin/tono-dsh" "$@"
SH
chmod 755 "$staging/tono-dsh"
mv -f -- "$staging/tono-dsh" "$launcher"
cp "$destination/deepseek-harness.service" "$unit"
mkdir -p "$HOME/deepseek-workspace" "$HOME/.local/share/deepseek-harness"
chmod 700 "$HOME/.local/share/deepseek-harness"
printf 'Installed: %s\n' "$destination"
if [[ ${2:-} == --start ]]; then
    # Minimal SSH/PAM sessions may not export the already-running user bus.
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"
    systemctl --user daemon-reload
    systemctl --user enable deepseek-harness.service
    systemctl --user restart deepseek-harness.service
    systemctl --user is-active deepseek-harness.service
else
    printf 'Start manually: cd ~/deepseek-workspace && ~/.local/bin/tono-dsh web --no-open\n'
fi
