#!/usr/bin/env python3
"""Export a package-owned Ubuntu Qt runtime for a Buildroot RootDirectory unit.

This deliberately does not export an Ubuntu base system. Buildroot owns PID 1,
networking and maintenance; this tree supplies the existing Qt/WebEngine ABI.
Run against a fresh Ubuntu 24.04 ARM64 reference root, never a running device.
"""
import argparse
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys


DEFAULT_SEEDS = """tono-canvas base-files dash bash coreutils util-linux procps sed
grep findutils python3 libc-bin ca-certificates locales tzdata systemd dbus-x11
xserver-xorg-core xserver-xorg-input-libinput xinit xauth x11-xserver-utils openbox
libgles2 libgl1-mesa-dri libegl-mesa0 libglx-mesa0 libgbm1 pipewire pipewire-alsa
pipewire-pulse wireplumber alsa-utils libasound2-plugins gstreamer1.0-plugins-good
qt6-qpa-plugins fonts-noto-cjk fonts-droid-fallback fcitx5 fcitx5-modules fcitx5-pinyin
fcitx5-frontend-qt6""".split()

# Runtime identity and devices come from the native Buildroot host, not the
# reference system. No actual Wi-Fi profiles, SSH keys, accounts or App data.
EXCLUDED_PREFIXES = (
    "/home", "/root", "/run", "/tmp", "/proc", "/sys", "/dev", "/boot",
    "/var/log", "/var/cache/apt", "/var/lib/apt", "/var/lib/dpkg",
    "/var/lib/NetworkManager", "/var/lib/private", "/var/lib/systemd",
    "/etc/NetworkManager/system-connections", "/etc/ssh", "/etc/ssl/private", "/usr/lib/ssl/private",
    "/etc/modules-load.d", "/usr/lib/modules-load.d", "/etc/modprobe.d", "/usr/lib/modprobe.d",
)
EXCLUDED_FILES = {
    "/etc/machine-id", "/var/lib/dbus/machine-id", "/etc/passwd", "/etc/group",
    "/etc/shadow", "/etc/gshadow", "/etc/passwd-", "/etc/group-",
    "/etc/shadow-", "/etc/gshadow-", "/etc/hostname", "/etc/hosts",
    "/etc/resolv.conf", "/etc/fstab", "/etc/subuid", "/etc/subgid",
    "/etc/modules",
}
GENERATED_TREES = ("/etc/ssl/certs", "/var/cache/fontconfig")
GENERATED_FILES = (
    "/etc/ld.so.cache", "/usr/lib/locale/locale-archive", "/etc/environment",
    "/etc/default/locale", "/etc/locale.conf", "/etc/localtime", "/etc/timezone",
    "/usr/share/glib-2.0/schemas/gschemas.compiled",
    "/usr/lib/aarch64-linux-gnu/gtk-3.0/3.0.0/immodules.cache",
    "/usr/lib/aarch64-linux-gnu/gdk-pixbuf-2.0/2.10.0/loaders.cache",
    *("/usr/share/mime/" + name for name in (
        "mime.cache", "aliases", "globs", "globs2", "magic", "subclasses", "icons",
        "generic-icons", "XMLnamespaces", "treemagic", "types", "version")),
)
LICENSE_PREFIXES = ("copyright", "copying", "license", "licence", "notice")
VOLATILE_ALIASES = {
    "/var/run": {"/run"}, "/var/lock": {"/run/lock"},
    "/etc/mtab": {"/proc/mounts", "/proc/self/mounts"},
}


def optional_touch_asset(path):
    # Onboard needs Adwaita and its own icons, not the full Ubuntu desktop theme.
    # Omitting these optional assets saves over 11000 TF filesystem inodes.
    prefixes = ("/usr/share/icons/Humanity", "/usr/share/icons/Humanity-Dark",
                "/usr/share/icons/ubuntu-mono-dark", "/usr/share/icons/ubuntu-mono-light",
                "/usr/share/icons/LoginIcons", "/usr/share/help", "/usr/share/sounds")
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def trimmed_documentation(path):
    """Only omissions made by scripts/trim-kiosk-rootfs.py are permitted."""
    if path.startswith(("/usr/share/man/", "/usr/share/info/")):
        return True
    if path.startswith("/usr/share/doc/"):
        name = path.rsplit("/", 1)[-1].lower()
        license_file = name.startswith(LICENSE_PREFIXES) or any(
            separator + prefix in name for separator in "-_." for prefix in LICENSE_PREFIXES
        )
        return not license_file
    if path.startswith("/usr/share/locale/"):
        language = path[len("/usr/share/locale/"):].split("/", 1)[0]
        kept = language in ("locale.alias", "C", "C.UTF-8", "POSIX", "en", "zh") or language.startswith(("en_", "en@", "zh_", "zh@"))
        return not kept
    return False


def excluded(path):
    return path in EXCLUDED_FILES or any(
        path == prefix or path.startswith(prefix + "/") for prefix in EXCLUDED_PREFIXES
    )


def normal_path(path):
    if not path.startswith("/") or "\x00" in path:
        raise ValueError("package path must be absolute: " + repr(path))
    parts = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError("parent traversal in package path: " + path)
        parts.append(part)
    return "/" + "/".join(parts)


def guest_path(root, guest, follow_final=True):
    """Resolve links as the guest would, never following absolute links on host."""
    pending = deque(normal_path(guest).split("/")[1:])
    parts, links = [], []
    hops = 0
    while pending:
        part = pending.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ValueError("symlink escapes guest root: " + guest)
            parts.pop()
            continue
        candidate = root.joinpath(*parts, part)
        if candidate.is_symlink() and (pending or follow_final):
            hops += 1
            if hops > 64:
                raise ValueError("symlink loop: " + guest)
            target = os.readlink(candidate)
            links.append(("/" + "/".join(parts + [part]), target))
            if target.startswith("/"):
                parts = []
            pending.extendleft(reversed(target.split("/")))
        else:
            parts.append(part)
    return root.joinpath(*parts), links


def control_records(text):
    for paragraph in re.split(r"\n\s*\n", text.strip()):
        record, key = {}, None
        for line in paragraph.splitlines():
            if line.startswith((" ", "\t")):
                if key is None:
                    raise ValueError("invalid Debian control continuation")
                record[key] += " " + line.strip()
            else:
                key, separator, value = line.partition(":")
                if not separator:
                    raise ValueError("invalid Debian control field: " + line)
                record[key] = value.strip()
        if record:
            yield record


def version_compare(left, right):
    """Debian epoch/upstream/revision ordering, including tilde and digit runs."""
    def split(value):
        epoch, separator, rest = value.partition(":")
        if not separator:
            epoch, rest = "0", value
        upstream, separator, revision = rest.rpartition("-")
        return int(epoch), upstream if separator else rest, revision if separator else "0"

    def order(char):
        if char == "~":
            return -1
        if not char or char.isdigit():
            return 0
        return ord(char) if char.isalpha() else ord(char) + 256

    def compare_part(a, b):
        while a or b:
            while (a and not a[0].isdigit()) or (b and not b[0].isdigit()):
                difference = order(a[:1]) - order(b[:1])
                if difference:
                    return 1 if difference > 0 else -1
                a, b = a[1:], b[1:]
            digits_a = re.match(r"\d*", a).group()
            digits_b = re.match(r"\d*", b).group()
            number_a, number_b = int(digits_a or "0"), int(digits_b or "0")
            if number_a != number_b:
                return 1 if number_a > number_b else -1
            a, b = a[len(digits_a):], b[len(digits_b):]
        return 0

    a, b = split(left), split(right)
    if a[0] != b[0]:
        return 1 if a[0] > b[0] else -1
    return compare_part(a[1], b[1]) or compare_part(a[2], b[2])


def dependency(atom):
    match = re.fullmatch(
        r"\s*([a-z0-9][a-z0-9+.-]*)(?::([a-z0-9-]+))?"
        r"\s*(?:\(\s*(<<|<=|=|>=|>>)\s*([^()\s]+)\s*\))?\s*", atom
    )
    if not match:
        raise ValueError("unsupported binary dependency: " + atom)
    return match.groups()


def satisfies(version, operator, required):
    if not operator:
        return True
    if not version:
        return False
    difference = version_compare(version, required)
    return {"<<": difference < 0, "<=": difference <= 0, "=": difference == 0,
            ">=": difference >= 0, ">>": difference > 0}[operator]


class PackageSet:
    def __init__(self, status):
        self.packages, self.providers = {}, {}
        for record in control_records(status):
            if record.get("Status") != "install ok installed":
                continue
            name, arch = record["Package"], record["Architecture"]
            identifier = name + ":" + arch
            self.packages[identifier] = record
            for atom in record.get("Provides", "").split(","):
                if not atom.strip():
                    continue
                provided, _, operator, version = dependency(atom)
                if operator not in (None, "="):
                    raise ValueError("invalid versioned Provides: " + atom)
                self.providers.setdefault(provided, []).append((identifier, version))

    def choose(self, alternatives):
        for atom in alternatives.split("|"):
            name, qualifier, operator, required = dependency(atom)
            arches = ("arm64", "all") if qualifier in (None, "any", "native", "arm64") else (qualifier,)
            for arch in arches:
                identifier = name + ":" + arch
                record = self.packages.get(identifier)
                if record and satisfies(record["Version"], operator, required):
                    return identifier
            for identifier, version in self.providers.get(name, []):
                if self.packages[identifier]["Architecture"] in arches and satisfies(version, operator, required):
                    return identifier
        raise ValueError("no installed ARM64 dependency satisfies: " + alternatives)

    def closure(self, seeds):
        selected, pending = set(), list(seeds)
        while pending:
            identifier = self.choose(pending.pop())
            if identifier in selected:
                continue
            selected.add(identifier)
            record = self.packages[identifier]
            if record["Architecture"] not in ("arm64", "all"):
                raise ValueError("foreign package in runtime: " + identifier)
            for field in ("Pre-Depends", "Depends"):
                pending.extend(value.strip() for value in record.get(field, "").split(",") if value.strip())
        return sorted(selected)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def export_runtime(source, destination, app_deb, seeds):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source.is_symlink() or source.resolve() != source or not source.is_dir():
        raise ValueError("source must be a real, canonical directory")
    if destination.is_symlink() or destination.parent.resolve() != destination.parent:
        raise ValueError("destination parent must be a real, canonical directory")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("source and destination must be separate trees")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise ValueError("destination must be absent or empty")
    release = guest_path(source, "/etc/os-release")[0].read_text()
    if not re.search(r'^ID=["\']?ubuntu["\']?$', release, re.M) or not re.search(
        r'^VERSION_ID=["\']?24\.04["\']?$', release, re.M
    ):
        raise ValueError("reference root must be Ubuntu 24.04")
    package_set = PackageSet(guest_path(source, "/var/lib/dpkg/status")[0].read_text())
    app_fields = subprocess.check_output(
        ["dpkg-deb", "--show", "--showformat=${Package}\t${Version}\t${Architecture}", str(app_deb)],
        text=True,
    ).split("\t")
    if len(app_fields) != 3 or app_fields[0] != "tono-canvas" or app_fields[2] != "arm64":
        raise ValueError("App must be a tono-canvas ARM64 Debian package")
    app = package_set.packages.get("tono-canvas:arm64", {})
    if app.get("Version") != app_fields[1]:
        raise ValueError("installed App version differs from --app-deb")
    selected = package_set.closure(seeds)
    if "tono-canvas:arm64" not in selected:
        raise ValueError("runtime seeds must include tono-canvas")
    touch = "onboard:arm64" in selected
    omitted_touch = []
    allowed, owners, omitted = set(), {}, []
    for identifier in selected:
        record = package_set.packages[identifier]
        candidates = [identifier, record["Package"]]
        info = guest_path(source, "/var/lib/dpkg/info")[0]
        package_list = next((info / (name + ".list") for name in candidates if (info / (name + ".list")).is_file()), None)
        if package_list is None:
            raise ValueError("missing dpkg file list: " + identifier)
        for line in package_list.read_text().splitlines():
            path = normal_path(line)
            if touch and (optional_touch_asset(path) or trimmed_documentation(path)):
                omitted_touch.append(path)
                continue
            if not excluded(path):
                physical, _ = guest_path(source, path, follow_final=False)
                if excluded(normal_path("/" + str(physical.relative_to(source)))):
                    continue
                if not physical.exists() and not physical.is_symlink() and trimmed_documentation(path):
                    omitted.append(path)
                    continue
                allowed.add(path)
                owners.setdefault(path, []).append(identifier)
    # Generated certificate links, font caches and locales are necessary runtime
    # assets. Their provenance is explicit; no general rootfs copy takes place.
    generated = set()
    for tree in GENERATED_TREES:
        directory, _ = guest_path(source, tree)
        if directory.is_dir():
            for current, directories, files in os.walk(directory, followlinks=False):
                for name in directories + files:
                    generated.add("/" + str(Path(current, name).relative_to(source)))
            generated.add(tree)
    for path in GENERATED_FILES:
        physical, _ = guest_path(source, path, follow_final=False)
        if physical.exists() or physical.is_symlink():
            generated.add(path)
    allowed.update(generated)
    # Preserve merged-/usr aliases, rather than creating duplicate /bin and /lib.
    for alias in ("/bin", "/sbin", "/lib", "/lib64"):
        if (source / alias.lstrip("/")).is_symlink():
            allowed.add(alias)
    canonical_allowed = set()
    for path in sorted(allowed):
        physical, _ = guest_path(source, path, follow_final=False)
        canonical_allowed.add(normal_path("/" + str(physical.relative_to(source))))
    destination.mkdir(mode=0o755, parents=False, exist_ok=True)
    entries, copying, optional_locale_targets = {}, set(), set()

    def copy_path(path):
        if path in entries:
            return
        if excluded(path):
            raise ValueError("runtime link points to excluded state: " + path)
        if path in copying:
            raise ValueError("recursive runtime link: " + path)
        copying.add(path)
        physical, parent_links = guest_path(source, path, follow_final=False)
        canonical = normal_path("/" + str(physical.relative_to(source)))
        for link, _ in parent_links:
            copy_path(link)
        # update-alternatives creates both /etc/alternatives/NAME and its
        # public command link, neither of which is in dpkg's package list.
        # Only admit that generated link shape; its final regular target must
        # still belong to the selected package closure below.
        generated_alternative = canonical.startswith("/etc/alternatives/") or (
            physical.is_symlink() and os.readlink(physical).startswith("/etc/alternatives/")
        )
        if canonical not in canonical_allowed and not generated_alternative:
            raise ValueError("link target is not package-owned or generated: " + canonical)
        target = destination / canonical.lstrip("/")
        # The canonical parent path never traverses a guest symlink on the host.
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = physical.lstat().st_mode
        except FileNotFoundError:
            # dpkg may list package-managed config symlinks removed by the
            # reference configuration. Missing regular assets must fail build.
            raise ValueError("installed package file is missing: " + path) from None
        if stat.S_ISLNK(mode):
            link_target = os.readlink(physical)
            resolved, links = guest_path(source, canonical)
            resolved_path = "/" + str(resolved.relative_to(source))
            volatile_alias = resolved_path in VOLATILE_ALIASES.get(canonical, set()) or (
                resolved_path == "/dev/null" and canonical.startswith(("/usr/lib/systemd/", "/etc/systemd/"))
            )
            if excluded(resolved_path) and not volatile_alias:
                raise ValueError("runtime link points to excluded state: " + resolved_path)
            for link, _ in links:
                if link != canonical:
                    copy_path(link)
            trimmed_link = not resolved.exists() and trimmed_documentation(resolved_path)
            # Ubuntu coreutils ships LC_TIME aliases but moves their actual
            # LC_MESSAGES payload into optional language-pack-* packages.
            # Preserve an already-dangling alias only for this precise shape;
            # a missing package-owned Chinese/English .mo still fails closed.
            time_locale = re.fullmatch(r"/usr/share/locale/([^/]+)/LC_TIME/([^/]+\.mo)", canonical)
            optional_locale = bool(
                time_locale and not resolved.exists() and resolved_path not in canonical_allowed
                and resolved_path == "/usr/share/locale/" + time_locale[1] + "/LC_MESSAGES/" + time_locale[2]
            )
            if optional_locale:
                optional_locale_targets.add(resolved_path)
            if trimmed_link:
                omitted.append(resolved_path)
            if not volatile_alias and not trimmed_link and not optional_locale and not resolved.is_dir():
                copy_path(resolved_path)
            if target.exists() or target.is_symlink():
                if not target.is_symlink() or os.readlink(target) != link_target:
                    raise ValueError("conflicting runtime link: " + canonical)
            else:
                target.symlink_to(link_target)
            entry = {"type": "symlink", "target": link_target}
        elif stat.S_ISDIR(mode):
            target.mkdir(exist_ok=True)
            target.chmod(stat.S_IMODE(mode) & 0o777)
            entry = {"type": "directory"}
        elif stat.S_ISREG(mode):
            if target.is_symlink():
                raise ValueError("destination file would follow a symlink: " + canonical)
            shutil.copyfile(physical, target)
            # No setuid/setgid programs are required inside the App runtime.
            target.chmod(stat.S_IMODE(mode) & 0o777)
            entry = {"type": "file", "sha256": sha256(target), "bytes": target.stat().st_size}
        else:
            raise ValueError("special file in runtime package: " + canonical)
        entry["packages"] = sorted(owners.get(path, owners.get(canonical, [])))
        entry["generated"] = path in generated or generated_alternative
        entries[canonical] = entry
        entries[path] = entry
        copying.remove(path)

    for path in sorted(allowed, key=lambda item: (item.count("/"), item)):
        copy_path(path)
    for path in ("dev", "proc", "sys", "run", "tmp", "home/avaota", "root", "var/tmp", "var/log"):
        (destination / path).mkdir(parents=True, exist_ok=True)
    (destination / "tmp").chmod(0o1777)
    (destination / "var/tmp").chmod(0o1777)
    manifest = {
        "format": 1, "runtime": "Ubuntu 24.04 ARM64 Qt compatibility runtime",
        "native_system": "Buildroot", "seeds": seeds,
        "app_deb": {"name": Path(app_deb).name, "version": app_fields[1], "sha256": sha256(Path(app_deb))},
        "packages": [{"name": identifier, "version": package_set.packages[identifier]["Version"]} for identifier in selected],
        "files": dict(sorted(entries.items())),
        "exclusions": sorted(EXCLUDED_FILES) + list(EXCLUDED_PREFIXES),
        "omitted_optional_touch_assets": sorted(set(omitted_touch)),
        "omitted_previously_trimmed_documentation": sorted(set(omitted)),
        "uninstalled_optional_locale_alias_targets": sorted(optional_locale_targets),
    }
    (destination / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return {"packages": len(selected), "files": len(entries), "regular_bytes": sum(
        value.get("bytes", 0) for key, value in entries.items() if str(guest_path(source, key, False)[0].relative_to(source)) == key.lstrip("/")
    ), "app_version": app_fields[1]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--app-deb", type=Path)
    parser.add_argument("--seed", action="append")
    parser.add_argument("--default-seeds", action="store_true")
    args = parser.parse_args()
    if args.default_seeds:
        print(" ".join(DEFAULT_SEEDS))
        return
    if not all((args.source, args.destination, args.app_deb)):
        parser.error("--source, --destination and --app-deb are required")
    try:
        print(json.dumps(export_runtime(args.source, args.destination, args.app_deb, args.seed or DEFAULT_SEEDS), sort_keys=True))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, "runtime export failed: " + str(error) + "\n")


if __name__ == "__main__":
    main()
