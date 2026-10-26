#!/usr/bin/env bash
# Build on a Linux ARM64 host; the resulting archive installs without npm/network.
set -euo pipefail

if [[ $# != 1 || $(uname -s) != Linux || $(uname -m) != aarch64 ]]; then
    echo "Usage (Linux ARM64): $0 OUTPUT_DIRECTORY" >&2
    exit 2
fi
if [[ $(id -u) == 0 ]]; then
    echo "Build as an ordinary user." >&2
    exit 2
fi
repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
templates="$repo/target/deepseek-harness"
node_version=24.22.1
node_sha=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e
release=deepseek-harness-0.2.1-rc.2-node24.22.1-linux-arm64
mkdir -p -- "$1"
output=$(cd -- "$1" && pwd)
archive="$output/$release.tar.gz"
[[ ! -e "$archive" ]] || { echo "Output already exists: $archive" >&2; exit 1; }
work=$(mktemp -d "$output/.dsh-build.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
bundle="$work/$release"
mkdir -p "$bundle/node" "$bundle/app" "$bundle/bin"
curl --fail --location --retry 3 \
    "https://nodejs.org/dist/v$node_version/node-v$node_version-linux-arm64.tar.xz" \
    --output "$work/node.tar.xz"
printf '%s  %s\n' "$node_sha" "$work/node.tar.xz" | sha256sum -c -
tar -xJf "$work/node.tar.xz" --strip-components=1 -C "$bundle/node"
cp "$templates/package.json" "$templates/package-lock.json" "$bundle/app/"
export PATH="$bundle/node/bin:$PATH"
(cd "$bundle/app" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    --registry=https://registry.npmjs.org)
cp "$templates/tono-dsh" "$bundle/bin/tono-dsh"
cp "$templates/deepseek-harness.service" "$bundle/"
chmod 755 "$bundle/bin/tono-dsh"
printf '%s\n' "$release" > "$bundle/VERSION"
cp "$repo/doc/deepseek-harness.md" "$bundle/README.md"
# Smoke test with a disposable Harness home. Never package credentials or profiles.
DSH_HOME="$work/check-home" "$bundle/bin/tono-dsh" --version
DSH_HOME="$work/check-home" "$bundle/bin/tono-dsh" web --help
(
    cd "$bundle"
    find . -type f ! -name SHA256SUMS -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > SHA256SUMS
)
tar -C "$work" -czf "$work/bundle.tar.gz" "$release"
mv -- "$work/bundle.tar.gz" "$archive"
(cd "$output" && sha256sum "$release.tar.gz" > "$release.tar.gz.sha256")
cp "$repo/scripts/install-deepseek-harness.sh" "$output/install-deepseek-harness.sh"
printf 'Offline bundle: %s\n' "$archive"
