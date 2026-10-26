#!/usr/bin/env bash
# Switch the existing A527 Xorg session to Harness, keeping the display/GPU setup.
set -euo pipefail
if [[ $(id -u) != 0 || $# -lt 1 || $# -gt 2 || (${2:-} != '' && ${2:-} != --restart) ]]; then
    echo "Usage: sudo bash $0 DEVICE_USER [--restart]" >&2
    exit 2
fi
account=$1
[[ $account =~ ^[a-z_][a-z0-9_-]*$ ]] || exit 2
uid=$(id -u "$account")
# Buildroot's minimal userspace need not ship the getent utility.
user_home=$(python3 - "$account" <<'PYHOME'
import pwd, sys
print(pwd.getpwnam(sys.argv[1]).pw_dir)
PYHOME
)
[[ $uid != 0 && $user_home == /home/"$account" ]] || exit 2
package="$user_home/.local/opt/deepseek-harness/current"
[[ -x $package/bin/tono-dsh-browser && -f $package/browser.json && -f $package/system/tono-kiosk-start ]]
[[ -f $user_home/.config/systemd/user/deepseek-harness.service ]]
rootdir=$(systemctl show tono-kiosk.service -p RootDirectory --value)
case "$rootdir" in
    ''|/) prefix='' ;;
    /opt/tono-runtime) prefix=/opt/tono-runtime ;;
    *) echo "Unsupported kiosk RootDirectory: $rootdir" >&2; exit 1 ;;
esac
start="$prefix/usr/local/libexec/tono-kiosk-start"
[[ -x $start ]] || { echo 'Existing A527 kiosk session not found.' >&2; exit 1; }
dropin=/etc/systemd/system/tono-kiosk.service.d/60-deepseek-harness.conf
if [[ -e $dropin ]] && ! grep -q '^# Tono DeepSeek Harness' "$dropin"; then
    echo "Refusing to replace unrelated configuration: $dropin" >&2
    exit 1
fi
backup=$(mktemp -d /var/tmp/tono-deepseek-kiosk-backup.XXXXXX)
chmod 700 "$backup"
cp -p "$start" "$backup/tono-kiosk-start"
if [[ -f $dropin ]]; then cp -p "$dropin" "$backup/60-deepseek-harness.conf"; fi
apparmor=/etc/apparmor.d/tono-deepseek-browser
if command -v apparmor_parser >/dev/null && [[ $(cat /sys/module/apparmor/parameters/enabled 2>/dev/null || true) == Y ]]; then
    if [[ -e $apparmor ]]; then
        grep -q '^# Tono DeepSeek Harness' "$apparmor" || { echo "Unrelated AppArmor profile exists." >&2; exit 1; }
        cp -p "$apparmor" "$backup/tono-deepseek-browser.apparmor"
    fi
    install -m 644 "$package/system/tono-deepseek-browser.apparmor" "$apparmor"
    apparmor_parser -r "$apparmor"
fi
# Check the real browser's loader inside the configured GUI filesystem before
# stopping the current display session. The probe runs as the device user.
browser=$(python3 - "$package/browser.json" <<'PY'
import json, pathlib, sys
p = pathlib.PurePosixPath(json.load(open(sys.argv[1]))['executable'])
if p.is_absolute() or '..' in p.parts or p.parts[0] != 'browser':
    raise SystemExit('Invalid browser executable path')
print(p)
PY
)
probe_properties=()
if [[ -n $prefix ]]; then
    probe_properties+=(--property="RootDirectory=$prefix" --property=MountAPIVFS=yes \
        --property=PrivateMounts=yes --property=BindPaths=/home)
fi
systemd-run --quiet --wait --pipe --collect --uid="$account" "${probe_properties[@]}" \
    /usr/bin/env "LD_LIBRARY_PATH=$package/browser-libs" "$package/$browser" --version
# Stop through the existing save-before-close path before replacing a running session.
if [[ ${2:-} == --restart ]]; then systemctl stop tono-kiosk.service; fi
install -m 755 "$package/system/tono-kiosk-start" "$start"
mkdir -p "$(dirname "$dropin")"
cat > "$dropin" <<EOF
# Tono DeepSeek Harness browser kiosk
[Unit]
Description=DeepSeek Harness browser kiosk

[Service]
Environment="TONO_KIOSK_SESSION=$package/bin/tono-dsh-session"
EOF
systemctl daemon-reload
systemctl enable tono-kiosk.service
systemctl start "user@$uid.service"
sudo -u "$account" env XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user daemon-reload
sudo -u "$account" env XDG_RUNTIME_DIR="/run/user/$uid" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
    systemctl --user enable deepseek-harness.service
if [[ ${2:-} == --restart ]]; then
    sudo -u "$account" env XDG_RUNTIME_DIR="/run/user/$uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$uid/bus" \
        systemctl --user restart deepseek-harness.service
    systemctl restart tono-kiosk.service
fi
printf 'Boot session switched to DeepSeek Harness. Backup: %s\n' "$backup"
