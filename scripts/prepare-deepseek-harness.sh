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
release=deepseek-harness-0.2.1-rc.2-node24.22.1-kiosk1-linux-arm64
mkdir -p -- "$1"
output=$(cd -- "$1" && pwd)
archive="$output/$release.tar.gz"
[[ ! -e "$archive" ]] || { echo "Output already exists: $archive" >&2; exit 1; }
work=$(mktemp -d "$output/.dsh-build.XXXXXX")
trap 'rm -rf -- "$work"' EXIT
bundle="$work/$release"
mkdir -p "$bundle/node" "$bundle/app" "$bundle/bin" "$bundle/browser-libs" "$bundle/system"
curl --fail --location --retry 3 \
    "https://nodejs.org/dist/v$node_version/node-v$node_version-linux-arm64.tar.xz" \
    --output "$work/node.tar.xz"
printf '%s  %s\n' "$node_sha" "$work/node.tar.xz" | sha256sum -c -
tar -xJf "$work/node.tar.xz" --strip-components=1 -C "$bundle/node"
cp "$templates/package.json" "$templates/package-lock.json" "$bundle/app/"
export PATH="$bundle/node/bin:$PATH"
(cd "$bundle/app" && npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
    --registry=https://registry.npmjs.org)
cp "$templates"/tono-dsh{,-server,-browser,-session} "$bundle/bin/"
cp "$templates/deepseek-harness.service" "$bundle/"
cp "$repo/target/services/tono-kiosk/tono-kiosk-start" "$bundle/system/"
cp "$templates/tono-deepseek-browser.apparmor" "$bundle/system/"
chmod 755 "$bundle/bin/"* "$bundle/system/tono-kiosk-start"
export PLAYWRIGHT_BROWSERS_PATH="$bundle/browser"
node "$bundle/app/node_modules/playwright-core/cli.js" install chromium --no-shell
browser_executable=$(node -e 'console.log(require(process.argv[1]).chromium.executablePath())' \
    "$bundle/app/node_modules/playwright-core")
# Three accessibility libraries are absent from the existing Ubuntu GUI runtime.
# Do not replace its Mesa, glibc or GPU driver libraries.
python3 - "$bundle" "$browser_executable" <<'PY'
import json, pathlib, shutil, subprocess, sys
bundle = pathlib.Path(sys.argv[1])
libraries = []
for soname in ('libatk-1.0.so.0', 'libatk-bridge-2.0.so.0', 'libatspi.so.0'):
    source = pathlib.Path('/usr/lib/aarch64-linux-gnu') / soname
    shutil.copyfile(source, bundle / 'browser-libs' / soname)
    package = subprocess.check_output(['dpkg-query', '-S', str(source)], text=True).split(': ')[0]
    version = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', package], text=True)
    name = package.split(':')[0]
    copyright_file = pathlib.Path('/usr/share/doc') / name / 'copyright'
    shutil.copyfile(copyright_file, bundle / 'browser-libs' / (name + '.copyright'))
    libraries.append({'soname': soname, 'package': package, 'version': version})
browsers = json.loads((bundle / 'app/node_modules/playwright-core/browsers.json').read_text())
chromium = next(x for x in browsers['browsers'] if x['name'] == 'chromium')
manifest = {'executable': str(pathlib.Path(sys.argv[2]).relative_to(bundle)),
            'chromium': chromium, 'extra_libraries': libraries}
(bundle / 'browser.json').write_text(json.dumps(manifest, indent=2) + '\n')
PY
"$browser_executable" --version
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
cp "$repo/scripts/enable-deepseek-kiosk.sh" "$output/enable-deepseek-kiosk.sh"
printf 'Offline bundle: %s\n' "$archive"
