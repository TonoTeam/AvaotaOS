#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
"""Install a private Qt compatibility runtime and A1 BSP into Buildroot TARGET.

Run from the post-build hook, before Buildroot's fakeroot ownership/device-table
processing. No target executable is run. Native /home, accounts, networking and
PID 1 are never imported from the compatibility archive.
"""
import argparse
from collections import deque
import ctypes
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import sys
import tarfile
import tempfile

RELEASE = "5.15.154-ga464bc4feaff"
KERNEL_SHA256 = "c3b2a73f7a68a8d83fbc6e28346c1cf46510330ac03d6d48d0a7af19890f6b2b"
HELPERS = tuple("usr/local/libexec/" + name for name in
                ("tono-kiosk-start", "tono-kiosk-session", "tono-kiosk-stop", "tono-gpu-probe", "tono-sandbox-preflight"))
CONFIGS = tuple(f"etc/X11/tono-kiosk-{mode}/{name}" for mode in ("hardware", "software")
                for name in ("20-avaota-display.conf", "21-tono.conf")) + ("etc/xdg/tono-kiosk/openbox.xml",)
REQUIRED = ("usr/bin/tono-canvas", "usr/lib/qt6/libexec/QtWebEngineProcess", "usr/lib/xorg/Xorg",
            "usr/bin/xinit", "usr/bin/xauth", "usr/bin/openbox", "usr/bin/fcitx5",
            "usr/bin/pipewire", "usr/bin/wireplumber", "usr/bin/arecord",
            "usr/lib/aarch64-linux-gnu/dri/panfrost_dri.so") + HELPERS + CONFIGS
EMPTY_IDENTITIES = ("etc/passwd", "etc/group", "etc/machine-id", "etc/hosts")
FORBIDDEN_FILES = ("etc/shadow", "etc/gshadow", "etc/passwd-", "etc/group-", "etc/shadow-", "etc/gshadow-",
                   "etc/subuid", "etc/subgid", "etc/hostname", "var/lib/dbus/machine-id")
EMPTY_TREES = ("home", "root", "run", "tmp", "proc", "sys", "dev", "var/log", "var/tmp",
               "etc/ssh", "etc/ssl/private", "etc/NetworkManager/system-connections", "var/lib/NetworkManager",
               "var/lib/dpkg", "var/lib/apt", "var/cache/apt", "var/lib/systemd", "var/lib/private")
WRAPPER_FILES = set(HELPERS) | set(CONFIGS) | {"etc/X11/xorg.conf.d/20-avaota-display.conf", "etc/xdg/tono-kiosk/openbox.xml", "etc/default/tono-kiosk",
                               "etc/resolv.conf", "tono-runtime-inputs.json", "runtime-manifest.json", *EMPTY_IDENTITIES}


class PayloadError(Exception):
    """Unsafe archive, invalid runtime or incorrect native target."""


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def member_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\x00" in name:
        raise PayloadError(f"Unsafe archive member: {name}")
    return "/".join(part for part in path.parts if part != ".")


def resolve_guest(name, links, follow_final=True):
    """Resolve guest links lexically; absolute targets remain inside the guest."""
    if "\x00" in name:
        raise PayloadError(f"Unsafe guest path: {name}")
    pending, parts, hops = deque(name.split("/")), [], 0
    while pending:
        part = pending.popleft()
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PayloadError(f"Link escapes guest root: {name}")
            parts.pop()
            continue
        candidate = "/".join(parts + [part])
        if candidate in links and (pending or follow_final):
            hops += 1
            if hops > 64:
                raise PayloadError(f"Guest symlink loop: {name}")
            target = links[candidate]
            if "\x00" in target:
                raise PayloadError(f"Unsafe guest link: {name}")
            if target.startswith("/"):
                parts = []
            pending.extendleft(reversed(target.split("/")))
        else:
            parts.append(part)
    return "/".join(parts)


def real_parents(path, root, create=False):
    relative = path.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise PayloadError(f"Destination parent is not a real directory: {current}")
        if create:
            current.mkdir(exist_ok=True)


def safe_extract(archive, destination, *, allow_devices=False):
    """Extract a tar to a new/empty directory without following host symlinks.

Files are placed at guest-canonical paths before any symlink is created. Tar
hardlinks are resolved against that same guest namespace. Special files are
rejected by default; allow_devices is only for isolated base-rootfs staging.
The caller owns cleanup of the new/empty destination after an error.
"""
    destination = Path(destination).absolute()
    if destination == Path("/") or destination.is_symlink() or destination.parent.resolve() != destination.parent:
        raise PayloadError("Extraction destination must be a real canonical directory")
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise PayloadError("Extraction destination must be absent or empty")
    with tarfile.open(archive, "r:*") as stream:
        raw = {}
        for item in stream.getmembers():
            name = member_name(item.name)
            if not name and not item.isdir():
                raise PayloadError("Archive root entry must be a directory")
            if name in raw:
                raise PayloadError(f"Duplicate archive member: {item.name}")
            if not (item.isfile() or item.isdir() or item.issym() or item.islnk() or
                    (allow_devices and (item.ischr() or item.isblk() or item.isfifo()))):
                raise PayloadError(f"Unsupported archive member type: {item.name}")
            raw[name] = item
        links = {name: item.linkname for name, item in raw.items() if item.issym()}
        # Move symlinks whose parents are aliases into the canonical namespace.
        for _ in range(64):
            updated = {}
            for name, item in raw.items():
                if item.issym():
                    canonical = resolve_guest(name, links, False)
                    if canonical in updated and updated[canonical] != item.linkname:
                        raise PayloadError(f"Conflicting guest symlink aliases: {name}")
                    updated[canonical] = item.linkname
            if updated == links:
                break
            links = updated
        else:
            raise PayloadError("Guest link graph does not converge")
        members = {}
        for name, item in raw.items():
            canonical = resolve_guest(name, links, False)
            if canonical in members:
                raise PayloadError(f"Archive aliases collide: {name}")
            members[canonical] = item
            if item.issym():
                resolve_guest(canonical, links)  # Validate even dangling links.
        # Validate parent types and hardlink targets before writing anything.
        for name in members:
            for parent in PurePosixPath(name).parents:
                key = "" if str(parent) == "." else parent.as_posix()
                if key in members and not members[key].isdir():
                    raise PayloadError(f"Archive parent is not a directory: {name}")
        def hardlink_target(name, seen=None):
            seen = set() if seen is None else seen
            if name in seen or name not in members:
                raise PayloadError(f"Missing or cyclic tar hardlink target: {name}")
            seen.add(name)
            item = members[name]
            if item.isfile():
                return name
            if not item.islnk():
                raise PayloadError(f"Tar hardlink target is not a regular file: {name}")
            target = resolve_guest(item.linkname, links)
            return hardlink_target(target, seen)
        hardlinks = {name: hardlink_target(name) for name, item in members.items() if item.islnk()}
        destination.mkdir(mode=0o755, exist_ok=True)
        ordered = sorted(members, key=lambda name: (name.count("/"), name))
        # Keep gzip reads monotonic. Depth-sorting regular files would make
        # gzip.seek repeatedly decompress a large reference root from its start.
        for name in sorted(members, key=lambda value: members[value].offset_data):
            item, target = members[name], destination / name
            real_parents(target, destination, create=True)
            if item.isdir():
                target.mkdir(exist_ok=True)
            elif item.isfile():
                with stream.extractfile(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
            elif item.ischr() or item.isblk():
                os.mknod(target, (stat.S_IFCHR if item.ischr() else stat.S_IFBLK) | item.mode,
                         os.makedev(item.devmajor, item.devminor))
            elif item.isfifo():
                os.mkfifo(target, item.mode)
        for name, source in hardlinks.items():
            target = destination / name
            real_parents(target, destination, create=True)
            os.link(destination / source, target)
        for name, item in members.items():
            if item.issym():
                target = destination / name
                real_parents(target, destination, create=True)
                target.symlink_to(item.linkname)
        # Directory permissions are applied last so read-only archive directories
        # do not block population. No metadata operation follows symlinks.
        for name in reversed(ordered):
            item, target = members[name], destination / name
            if os.geteuid() == 0:
                os.chown(target, item.uid, item.gid, follow_symlinks=False)
            if not item.issym():
                target.chmod(item.mode & 0o7777)
            os.utime(target, (item.mtime, item.mtime), follow_symlinks=False)
    return {"members": len(members), "files": sum(item.isfile() for item in members.values()), "symlinks": len(links)}


def tree_links(root):
    links = {}
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            if path.is_symlink():
                links[path.relative_to(root).as_posix()] = os.readlink(path)
    return links


def guest_file(root, name, links, follow_final=True):
    return root / resolve_guest(name.lstrip("/"), links, follow_final)


def reject_identity(root):
    links = tree_links(root)
    for name in EMPTY_IDENTITIES:
        path = root / name
        real_parents(path, root)
        if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_size)):
            raise PayloadError(f"Runtime contains a populated identity file: {name}")
    for name in FORBIDDEN_FILES:
        path = guest_file(root, name, links, False)
        if path.exists() or path.is_symlink():
            raise PayloadError(f"Runtime contains a private account or identity: {name}")
    for name in EMPTY_TREES:
        path = root / name
        real_parents(path, root)
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise PayloadError(f"Runtime state path is not an empty directory: {name}")
        if path.exists():
            for directory, names, files in os.walk(path, followlinks=False):
                if files or any((Path(directory) / child).is_symlink() for child in names):
                    raise PayloadError(f"Runtime contains user data, credentials or mutable state: {name}")


def validate_runtime(root):
    reject_identity(root)
    links = tree_links(root)
    for name in REQUIRED:
        path = guest_file(root, name, links)
        if not path.is_file() or path.stat().st_size == 0:
            raise PayloadError(f"Runtime lacks required component: {name}")
        if name.endswith(("tono-canvas", "QtWebEngineProcess", "Xorg")) or name in HELPERS:
            if not path.stat().st_mode & 0o111:
                raise PayloadError(f"Runtime component is not executable: {name}")
    app = guest_file(root, "usr/bin/tono-canvas", links)
    with app.open("rb") as stream:
        header = stream.read(64)
    if len(header) < 64 or header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 18)[0] != 183:
        raise PayloadError("Runtime App is not an AArch64 ELF executable")
    for name in ("runtime-manifest.json", "tono-runtime-inputs.json"):
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise PayloadError(f"Runtime lacks a regular provenance manifest: {name}")
    manifest = json.loads((root / "runtime-manifest.json").read_text())
    inputs = json.loads((root / "tono-runtime-inputs.json").read_text())
    if manifest.get("format") != 1 or manifest.get("native_system") != "Buildroot" or not isinstance(manifest.get("files"), dict):
        raise PayloadError("Runtime package provenance manifest has an unsupported format")
    app_info = manifest.get("app_deb", {})
    app_digest = app_info.get("sha256", "")
    if len(app_digest) != 64 or inputs.get("app_deb_sha256") != app_digest:
        raise PayloadError("Runtime input manifest does not match the exported App package")
    verified, declared = {}, set()
    for name, metadata in manifest["files"].items():
        canonical = resolve_guest(name.lstrip("/"), links, False)
        path = root / canonical
        declared.add(canonical)
        kind = metadata.get("type")
        if kind == "file":
            if path.is_symlink() or not path.is_file():
                raise PayloadError(f"Runtime package file changed type: {name}")
            if canonical not in verified:
                verified[canonical] = sha256(path)
            if verified[canonical] != metadata.get("sha256") or path.stat().st_size != metadata.get("bytes"):
                raise PayloadError(f"Runtime package file checksum mismatch: {name}")
        elif kind == "symlink":
            if not path.is_symlink() or os.readlink(path) != metadata.get("target"):
                raise PayloadError(f"Runtime package symlink changed: {name}")
        elif kind == "directory":
            if path.is_symlink() or not path.is_dir():
                raise PayloadError(f"Runtime package directory changed: {name}")
        else:
            raise PayloadError(f"Unknown package manifest entry type: {name}")
    if resolve_guest("usr/bin/tono-canvas", links, False) not in declared:
        raise PayloadError("App binary is absent from package provenance manifest")
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if (not path.is_dir() or path.is_symlink()) and relative not in declared and relative not in WRAPPER_FILES:
                raise PayloadError(f"Runtime contains an undeclared file: {relative}")
            if not path.is_symlink() and path.stat().st_mode & 0o6000:
                raise PayloadError(f"Runtime contains an unexpected setuid/setgid file: {relative}")
    return {"app_version": app_info.get("version"), "package_files_verified": len(verified),
            "app_deb_sha256": app_digest}


def exchange_runtime(incoming, target):
    """Publish a new runtime, atomically exchanging a previous build if present."""
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise PayloadError("Existing private runtime must be a real directory")
    if not target.exists():
        os.rename(incoming, target)
        return
    if any(target.iterdir()):
        validate_runtime(target)
    library = ctypes.CDLL(None, use_errno=True)
    exchange = getattr(library, "renameat2", None)
    if sys.platform != "linux" or exchange is None:
        raise PayloadError("Atomic cached runtime replacement requires Linux renameat2")
    exchange.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    exchange.restype = ctypes.c_int
    if exchange(-100, os.fsencode(incoming), -100, os.fsencode(target), 2) != 0:
        code = ctypes.get_errno()
        raise PayloadError("Atomic runtime exchange failed: " + os.strerror(code))
    # The previous private, data-free runtime is now at the incoming path.
    shutil.rmtree(incoming)


def verify_bsp(bsp, report):
    """Detect prepared-cache mutations before installing or publishing assets."""
    def asset(relative, expected):
        path = bsp / relative
        real_parents(path, bsp)
        if path.is_symlink() or not path.is_file() or sha256(path) != expected:
            raise PayloadError(f"Prepared BSP asset checksum mismatch: {relative}")
    asset("boot/Image", report["kernel"]["sha256"])
    asset("boot/dtb/allwinner/sun55i-t527-avaota-a1.dtb", report.get("dtb", {}).get("sha256"))
    asset("bootloader-syterkit.bin", report.get("bootloader", {}).get("sha256"))
    asset("boot/uInitrd", report.get("uinitrd_sha256"))
    boot_assets = report.get("provenance", {}).get("prebuilt_boot_assets", [])
    if {item.get("path") for item in boot_assets} != {"bl31.bin", "scp.bin", "splash.bin", "uInitrd"}:
        raise PayloadError("Prepared BSP lacks prebuilt-asset provenance")
    for item in boot_assets:
        asset("boot/" + item["path"], item.get("sha256"))
    boot_files = report.get("provenance", {}).get("boot_files", {})
    if not isinstance(boot_files, dict) or not boot_files:
        raise PayloadError("Prepared BSP lacks complete boot-file checksums")
    for name, digest in boot_files.items():
        if not name or member_name(name) != name:
            raise PayloadError("Invalid BSP boot-file inventory path")
        asset("boot/" + name, digest)
    actual_boot = {path.relative_to(bsp / "boot").as_posix() for path in (bsp / "boot").rglob("*") if path.is_file() or path.is_symlink()}
    if actual_boot != set(boot_files):
        raise PayloadError("Prepared BSP boot-file inventory changed")
    spec = importlib.util.spec_from_file_location("a1_bsp_verifier", Path(__file__).with_name("buildroot-prepare-bsp.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        modules = module.validate_modules(bsp / "root-overlay/usr/lib/modules" / RELEASE)
    except module.BspError as error:
        raise PayloadError(str(error)) from error
    recorded = report.get("modules", {})
    if any(modules.get(key) != recorded.get(key) for key in ("release", "count", "vermagic_verified", "content_sha256")):
        raise PayloadError("Prepared BSP kernel module content changed")
    firmware = bsp / "root-overlay/usr/lib/firmware"
    inventory = report.get("provenance", {}).get("firmware", {}).get("files", [])
    if not inventory:
        raise PayloadError("Prepared BSP lacks firmware checksums")
    declared = set()
    for item in inventory:
        name = item.get("path", "")
        if not name or member_name(name) != name or name in declared:
            raise PayloadError("Invalid BSP firmware inventory path")
        declared.add(name)
        path = firmware / name
        real_parents(path, firmware)
        if item.get("type") == "symlink":
            if not path.is_symlink() or os.readlink(path) != item.get("target"):
                raise PayloadError(f"Prepared firmware symlink changed: {name}")
        elif path.is_symlink() or not path.is_file() or sha256(path) != item.get("sha256"):
            raise PayloadError(f"Prepared firmware checksum mismatch: {name}")
    actual = {path.relative_to(firmware).as_posix() for path in firmware.rglob("*")
              if path.is_symlink() or path.is_file()}
    if actual != declared:
        raise PayloadError("Prepared firmware inventory changed")


def merge_overlay(source, destination, target):
    """Replace individual files without following existing symlinks/hardlinks."""
    destination.mkdir(parents=True, exist_ok=True)
    for directory, names, files in os.walk(source, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            output = destination / path.relative_to(source)
            real_parents(output, target, create=True)
            if output.is_symlink():
                raise PayloadError(f"Native BSP destination is a symlink: {output}")
            if path.is_dir() and not path.is_symlink():
                output.mkdir(exist_ok=True)
            elif path.is_symlink():
                descriptor, temporary = tempfile.mkstemp(prefix=".tono-bsp-", dir=output.parent)
                os.close(descriptor)
                os.unlink(temporary)
                try:
                    os.symlink(os.readlink(path), temporary)
                    os.replace(temporary, output)
                finally:
                    if os.path.lexists(temporary):
                        os.unlink(temporary)
            else:
                descriptor, temporary = tempfile.mkstemp(prefix=".tono-bsp-", dir=output.parent)
                os.close(descriptor)
                try:
                    shutil.copy2(path, temporary)
                    os.replace(temporary, output)
                finally:
                    if os.path.lexists(temporary):
                        os.unlink(temporary)


def install(target, runtime, bsp):
    target = target.absolute()
    if target.is_symlink() or target.parent.resolve() != target.parent:
        raise PayloadError("Native target must be a real canonical directory")
    target = target.resolve(strict=True)
    if target in runtime.parents or target == runtime or target in bsp.parents or target == bsp:
        raise PayloadError("Runtime archive and BSP inputs must be outside the native target")
    if target == Path("/") or not target.is_dir():
        raise PayloadError("Expected a Buildroot target directory")
    links = tree_links(target)
    release_file = guest_file(target, "etc/os-release", links)
    if not release_file.is_file() or not any(line.strip() in ("ID=buildroot", 'ID="buildroot"', "ID='buildroot'") for line in release_file.read_text().splitlines()):
        raise PayloadError("Native target is not identified as Buildroot")
    manifest_path = bsp / "bsp-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise PayloadError("BSP provenance manifest must be a regular file")
    report = json.loads(manifest_path.read_text())
    if report.get("board") != "avaota-a1" or report.get("kernel", {}).get("release") != RELEASE or report.get("kernel", {}).get("sha256") != KERNEL_SHA256:
        raise PayloadError("BSP manifest does not match the verified A1 kernel")
    overlay = bsp / "root-overlay"
    if overlay.is_symlink() or not overlay.is_dir():
        raise PayloadError("BSP root overlay must be a real directory")
    sources = [overlay / "usr/lib/modules" / RELEASE, overlay / "usr/lib/firmware"]
    for source in sources:
        real_parents(source, overlay)
        if source.is_symlink() or not source.is_dir():
            raise PayloadError(f"Missing real BSP overlay directory: {source}")
        for directory, names, files in os.walk(source, followlinks=False):
            for name in names + files:
                path = Path(directory) / name
                if path.is_symlink():
                    resolved = path.resolve(strict=True)
                    if source not in resolved.parents:
                        raise PayloadError(f"BSP overlay link leaves its payload: {path}")
                elif not (path.is_dir() or path.is_file()):
                    raise PayloadError(f"BSP overlay special file: {path}")
                relative = path.relative_to(overlay)
                real_parents(target / relative, target)
                if (target / relative).is_symlink():
                    raise PayloadError(f"Native BSP destination is a symlink: {relative}")
    verify_bsp(bsp, report)
    opt = target / "opt"
    if opt.is_symlink() or (opt.exists() and not opt.is_dir()):
        raise PayloadError("Native opt must be a real directory")
    opt.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tono-payload-", dir=opt) as temporary:
        incoming = Path(temporary) / "runtime"
        safe_extract(runtime, incoming)
        runtime_info = validate_runtime(incoming)
        # Stage BSP files too, before touching any previous build's payload.
        for source in sources:
            destination = target / source.relative_to(overlay)
            real_parents(destination, target, create=True)
            if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
                raise PayloadError(f"Native BSP destination is not a real directory: {destination}")
            merge_overlay(source, destination, target)
        exchange_runtime(incoming, opt / "tono-runtime")
    return {"runtime": "opt/tono-runtime", **runtime_info, "kernel_release": RELEASE,
            "runtime_archive_sha256": sha256(runtime)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    parser.add_argument("--bsp", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.target.is_symlink():
            raise PayloadError("Native target must not be a symlink")
        print(json.dumps(install(args.target, args.runtime.resolve(strict=True), args.bsp.resolve(strict=True))))
        return 0
    except (PayloadError, OSError, ValueError, tarfile.TarError) as error:
        print(f"buildroot-install-payload: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
