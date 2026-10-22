#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
"""Reuse the verified A1 BSP and SyterKit in a fresh Buildroot staging directory.

No Debian package is installed and no kernel is recompiled. The output contains
boot/, bootloader-syterkit.bin, root-overlay/usr/lib/{modules,firmware}/ and a
manifest. Use the root overlay before Buildroot's final ownership/device-table
processing, then pass its finalized root tree to buildroot-pack-image.py.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import struct
import shutil
import stat
import subprocess
import sys
import tarfile

RELEASE = "5.15.154-ga464bc4feaff"
KERNEL_COMMIT = "a464bc4feaff7b102dac6362b44ac3303ec7cae1"
SYTERKIT_COMMIT = "0998aeb48289058d26b3c24febb3405e29b66898"
KERNEL_SHA256 = "c3b2a73f7a68a8d83fbc6e28346c1cf46510330ac03d6d48d0a7af19890f6b2b"
DTB_NAME = "sun55i-t527-avaota-a1.dtb"
SD_NODE = "/soc@3000000/sdmmc@4020000"
REMOVED_PROPERTIES = ("cd-gpios", "cd-set-debounce", "cd-used-24M", "broken-cd", "cd-inverted")
REQUIRED_CONFIG = (
    "USER_NS", "NAMESPACES", "PID_NS", "NET_NS", "SECCOMP", "SECCOMP_FILTER",
    "BPF", "BPF_SYSCALL", "CGROUPS", "CGROUP_PIDS", "CGROUP_DEVICE", "DEVTMPFS",
    "FHANDLE", "INOTIFY_USER", "SIGNALFD", "TIMERFD", "EPOLL", "UNIX", "UNIX98_PTYS", "PROC_FS", "SYSFS", "TMPFS",
    "EXT4_FS", "VFAT_FS", "DRM", "AW_DRM", "AW_DRM_PANFROST", "AW_DRM_HDMI_TX",
    "SOUND", "SND", "SND_SOC_SUNXI_SUN55IW3_CODEC", "VIDEO_V4L2", "AIC_INTF_SDIO",
)
BOOTARGS = ("root=LABEL=rootfs rootwait clk_ignore_unused console=tty1 console=ttyAS0,115200 "
            "cma=64M init=/sbin/init rw fsck.fix=yes fsck.repair=yes net.ifnames=0 "
            "quiet loglevel=3 systemd.show_status=auto")


class BspError(Exception):
    """Rejected BSP input or failed staging verification."""


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(arguments):
    result = subprocess.run([str(item) for item in arguments], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise BspError(f"{arguments[0]} failed ({result.returncode}):\n{result.stdout}")
    return result.stdout.strip()


def clean_name(name):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise BspError(f"Unsafe archive member: {name}")
    return path.as_posix().rstrip("/")


def destination(name):
    """Map the small allowlist to output paths; ignore package/header payloads."""
    name = clean_name(name)
    if name == "bootloader-syterkit.bin":
        return name
    boot_prefix = "bootloader-avaota-a1/"
    if name.startswith(boot_prefix):
        item = name[len(boot_prefix):]
        if item in ("bl31.bin", "scp.bin", "splash.bin", "uInitrd"):
            return "boot/" + item
    image_prefix = "deb-data/image/boot/"
    if name.startswith(image_prefix):
        item = name[len(image_prefix):]
        if item == "vmlinuz-" + RELEASE:
            return "boot/Image"
        if item in ("config-" + RELEASE, "System.map-" + RELEASE):
            return "boot/" + item
    module_prefix = "deb-data/image/lib/modules/" + RELEASE
    if name == module_prefix or name.startswith(module_prefix + "/"):
        item = name[len(module_prefix):].lstrip("/")
        if item in ("build", "source") or item.startswith(("build/", "source/")):
            return None
        return "root-overlay/usr/lib/modules/" + RELEASE + ("/" + item if item else "")
    for prefix in ("deb-data/dtb/boot/dtb/", "deb-data/dtb/boot/dtb-" + RELEASE + "/"):
        if name == prefix + "allwinner/" + DTB_NAME:
            return "boot/dtb/allwinner/" + DTB_NAME
    return None


def safe_parent(path, root):
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise BspError(f"Archive member traverses a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)


def extract_selected(archive, output):
    selected = {}
    for member in archive.getmembers():
        mapped = destination(member.name)
        if mapped is None:
            continue
        if mapped in selected:
            raise BspError(f"Duplicate BSP archive destination: {mapped}")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise BspError(f"Unsupported BSP member type: {member.name}")
        selected[mapped] = member
    required = ["boot/Image", "boot/config-" + RELEASE, "boot/dtb/allwinner/" + DTB_NAME,
                "bootloader-syterkit.bin", "boot/bl31.bin", "boot/scp.bin", "boot/splash.bin", "boot/uInitrd"]
    for name in required:
        if name not in selected or not selected[name].isfile() or selected[name].size == 0:
            raise BspError(f"BSP cache lacks required regular file: {name}")
    module_base = "root-overlay/usr/lib/modules/" + RELEASE
    if not any(name.startswith(module_base + "/") and name.endswith(".ko") for name in selected):
        raise BspError("BSP cache lacks modules for the selected kernel ABI")
    deferred = []
    for relative, member in sorted(selected.items(), key=lambda item: (len(PurePosixPath(item[0]).parts), item[0])):
        target = output / relative
        safe_parent(target, output)
        if member.isdir():
            target.mkdir(exist_ok=True)
            continue
        if member.issym() or member.islnk():
            if not relative.startswith(module_base + "/"):
                raise BspError(f"Boot files must not be links: {member.name}")
            if member.issym():
                if PurePosixPath(member.linkname).is_absolute() or "\\" in member.linkname:
                    raise BspError(f"External module symlink: {member.name}")
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(relative), member.linkname))
            else:
                resolved = destination(member.linkname)
            if not resolved or not resolved.startswith(module_base + "/") or resolved not in selected:
                raise BspError(f"Dangling or external module link: {member.name}")
            deferred.append((target, member, output / resolved))
            continue
        with archive.extractfile(member) as source, target.open("xb") as stream:
            shutil.copyfileobj(source, stream)
        target.chmod(member.mode & 0o7777)
        if os.geteuid() == 0:
            os.chown(target, member.uid, member.gid)
    # Resolving links after regular files avoids extracting through a link. The
    # selected target must already exist, so link cycles are rejected too.
    for target, member, resolved in deferred:
        if not resolved.exists() or resolved.is_symlink():
            raise BspError(f"Unresolved module link: {member.name}")
        if member.issym():
            target.symlink_to(member.linkname)
        else:
            os.link(resolved, target)
    return selected


def validate_kernel(image, config):
    with image.open("rb") as stream:
        header = stream.read(64)
    if len(header) != 64 or header[56:60] != b"ARM\x64":
        raise BspError("BSP kernel is not an uncompressed ARM64 Linux Image")
    digest = sha256(image)
    if digest != KERNEL_SHA256:
        raise BspError(f"Kernel SHA256 differs from the verified A1 BSP: {digest}")
    options = {}
    for line in config.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            name, value = line.split("=", 1)
            options[name[7:]] = value
    absent = [name for name in REQUIRED_CONFIG if options.get(name) != "y"]
    if absent:
        raise BspError("BSP kernel lacks required built-in features: " + ", ".join(absent))
    if options.get("AIC8800_WLAN_SUPPORT") not in ("y", "m"):
        raise BspError("BSP kernel lacks the AIC8800 Wi-Fi driver")
    return {"release": RELEASE, "sha256": digest,
            "required_config": {"CONFIG_" + name: options[name] for name in REQUIRED_CONFIG},
            "optional_config": {"CONFIG_" + name: options.get(name, "n")
                                for name in ("DEVTMPFS_MOUNT", "CGROUP_BPF", "DRM_PANFROST", "TMPFS_POSIX_ACL", "TMPFS_XATTR")},
            "notes": ["Panfrost uses CONFIG_AW_DRM_PANFROST in this BSP.",
                      "systemd mounts devtmpfs; CONFIG_DEVTMPFS_MOUNT is optional.",
                      "Linux 5.15 cgroup v2 is provided by CONFIG_CGROUPS; there is no CONFIG_CGROUP2 switch.",
                      "Upstream systemd 258 treats tmpfs ACL/xattr support as optional but strongly recommended."]}


def validate_modules(root):
    modules = sorted(root.rglob("*.ko"))
    names = {module.name for module in modules}
    if not {"aic8800_bsp.ko", "aic8800_fdrv.ko"} <= names:
        raise BspError("BSP cache lacks the AIC8800 SDIO Wi-Fi modules")
    digest = hashlib.sha256()
    for path in modules:
        data = path.read_bytes()
        if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", data, 18)[0] != 183:
            raise BspError(f"Kernel module is not an AArch64 ELF object: {path.name}")
        versions = re.findall(rb"(?:^|\x00)vermagic=([^\x00]+)", data)
        if len(versions) != 1 or versions[0].split(b" ", 1)[0] != RELEASE.encode():
            raise BspError(f"Kernel module ABI differs from {RELEASE}: {path.name}")
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(data).digest())
    return {"release": RELEASE, "count": len(modules), "vermagic_verified": len(modules),
            "content_sha256": digest.hexdigest(), "source_and_build_links": "excluded"}


def patch_dtb(path):
    original = sha256(path)
    compatible = run(["fdtget", "-t", "s", path, SD_NODE, "compatible"])
    if compatible != "allwinner,sunxi-mmc-v5p3x":
        raise BspError("Unexpected A1 SD controller compatible; refusing to patch another board")
    properties = run(["fdtget", "-p", path, SD_NODE]).splitlines()
    removed = []
    for name in REMOVED_PROPERTIES:
        if name in properties:
            run(["fdtput", "-d", path, SD_NODE, name])
            removed.append(name)
    run(["fdtput", path, SD_NODE, "non-removable"])
    result = run(["fdtget", "-p", path, SD_NODE]).splitlines()
    if "non-removable" not in result or any(name in result for name in REMOVED_PROPERTIES):
        raise BspError("A1 permanent SD detection patch failed verification")
    return {"path": "dtb/allwinner/" + DTB_NAME, "sha256_before": original,
            "sha256": sha256(path), "removed_properties": removed, "non_removable": True}


def copy_firmware(source, destination):
    count = 0
    for directory, names, files in os.walk(source, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            info = path.lstat()
            if path.is_symlink():
                resolved = path.resolve(strict=True)
                if source != resolved and source not in resolved.parents:
                    raise BspError(f"Firmware link escapes source directory: {path}")
            elif not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise BspError(f"Unsupported firmware file type: {path}")
            if stat.S_ISREG(info.st_mode):
                count += 1
    if count == 0 or not (source / "aic8800").is_dir():
        raise BspError("Firmware directory lacks the onboard AIC8800 assets")
    shutil.copytree(source, destination, symlinks=True)
    return count


def provenance(firmware, boot, firmware_revision=None):
    if firmware_revision is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", firmware_revision):
        raise BspError("Firmware revision must be a full 40-character Git commit")
    if firmware_revision is None and shutil.which("git"):
        result = subprocess.run(["git", "-C", str(firmware), "rev-parse", "HEAD"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        candidate = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40}", candidate):
            firmware_revision = candidate
    inventory = []
    for path in sorted(firmware.rglob("*")):
        relative = path.relative_to(firmware).as_posix()
        if path.is_symlink():
            inventory.append({"path": relative, "type": "symlink", "target": os.readlink(path), "license": "not-established"})
        elif path.is_file():
            inventory.append({"path": relative, "bytes": path.stat().st_size,
                              "sha256": sha256(path), "license": "not-established"})
    return {
        "kernel": {"repository": "https://github.com/AvaotaSBC/linux", "commit": KERNEL_COMMIT,
                   "source_archive": "https://github.com/AvaotaSBC/linux/archive/" + KERNEL_COMMIT + ".tar.gz",
                   "license": "GPL-2.0-only; individual source notices apply",
                   "license_reference": "https://github.com/AvaotaSBC/linux/blob/" + KERNEL_COMMIT + "/COPYING"},
        "syterkit": {"repository": "https://github.com/YuzukiHD/SyterKit", "commit": SYTERKIT_COMMIT,
                     "source_archive": "https://github.com/YuzukiHD/SyterKit/archive/" + SYTERKIT_COMMIT + ".tar.gz",
                     "license": "GPL-2.0; individual component notices apply",
                     "license_reference": "https://github.com/YuzukiHD/SyterKit/blob/" + SYTERKIT_COMMIT + "/LICENSE"},
        "firmware": {"repository": "https://github.com/TonoTeam/AvaotaOS", "directory": "target/firmware",
                     "repository_commit": firmware_revision, "files": inventory,
                     "license_status": "No per-file license metadata supplied in target/firmware; not covered by the framework GPL license."},
        "prebuilt_boot_assets": [{"path": name, "sha256": sha256(boot / name),
                                  "license": "component-notices-not-collected",
                                  "origin": ("target/boot/uInitrd from the existing BSP cache" if name == "uInitrd" else
                                             "SyterKit board/avaota-a1/extlinux_boot prebuilt asset")}
                                 for name in ("bl31.bin", "scp.bin", "splash.bin", "uInitrd")],
        "boot_files": {path.relative_to(boot).as_posix(): sha256(path) for path in sorted(boot.rglob("*")) if path.is_file()},
        "scope": "Provenance inventory only. Buildroot legal-info covers native packages; this inventory does not collect all corresponding BSP source archives or establish redistribution licenses for binary firmware/prebuilt assets.",
    }


def prepare(cache, firmware, output, firmware_revision=None):
    if output.exists() or output.is_symlink():
        raise BspError(f"Refusing to overwrite existing BSP output: {output}")
    if output == firmware or firmware in output.parents:
        raise BspError("BSP output overlaps firmware source")
    for tool in ("fdtget", "fdtput"):
        if not shutil.which(tool):
            raise BspError(f"Missing device-tree tool: {tool} (install device-tree-compiler)")
    output.mkdir(mode=0o755)
    try:
        with tarfile.open(cache, "r:gz") as archive:
            extract_selected(archive, output)
        boot = output / "boot"
        kernel = validate_kernel(boot / "Image", boot / ("config-" + RELEASE))
        dtb = patch_dtb(boot / "dtb/allwinner" / DTB_NAME)
        (boot / "extlinux").mkdir()
        (boot / "extlinux/extlinux.conf").write_text(
            "label AvaotaOS Buildroot\n  kernel /Image\n  fdt /dtb/allwinner/" + DTB_NAME +
            "\n  initrd /uInitrd\n  append " + BOOTARGS + "\n")
        firmware_count = copy_firmware(firmware, output / "root-overlay/usr/lib/firmware")
        # The loader validator is shared with the SD image packer, avoiding two
        # subtly different interpretations of the BootROM checksum algorithm.
        spec = importlib.util.spec_from_file_location("a1_image_packer", Path(__file__).with_name("buildroot-pack-image.py"))
        packer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(packer)
        try:
            loader = packer.validate_loader(output / "bootloader-syterkit.bin")
        except packer.ImageError as error:
            raise BspError(str(error)) from error
        module_root = output / "root-overlay/usr/lib/modules" / RELEASE
        modules = validate_modules(module_root)
        # Firmware metadata must be for a clean image rather than host user IDs.
        if os.geteuid() == 0:
            for directory, names, files in os.walk(output / "root-overlay/usr/lib/firmware", followlinks=False):
                os.chown(directory, 0, 0)
                for name in names + files:
                    os.chown(Path(directory) / name, 0, 0, follow_symlinks=False)
        report = {"schema_version": 1, "board": "avaota-a1", "soc": "allwinner-a527",
                  "bsp_cache_sha256": sha256(cache), "kernel": kernel, "dtb": dtb,
                  "bootloader": loader, "uinitrd_sha256": sha256(boot / "uInitrd"),
                  "modules": modules,
                  "firmware": {"files": firmware_count, "aic8800": "included"},
                  "provenance": provenance(firmware, boot, firmware_revision),
                  "hardware_boot": "not-tested"}
        (output / "bsp-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    except Exception:
        shutil.rmtree(output)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bsp-cache", required=True, help="verified A1 BSP cache tar.gz")
    parser.add_argument("--firmware", required=True, help="repository target/firmware directory")
    parser.add_argument("--output", required=True, help="fresh staging directory (parent must exist)")
    parser.add_argument("--firmware-revision", help="full firmware repository commit if .git is unavailable")
    args = parser.parse_args(argv)
    try:
        cache, firmware = Path(args.bsp_cache).resolve(strict=True), Path(args.firmware).resolve(strict=True)
        raw_output = Path(args.output).absolute()
        output = raw_output.parent.resolve(strict=True) / raw_output.name
        if raw_output.is_symlink() or not cache.is_file() or not firmware.is_dir():
            raise BspError("Expected regular cache, firmware directory and a new output directory")
        if sys.platform != "linux" or os.geteuid() != 0:
            raise BspError("Run BSP preparation as root on Linux to preserve numeric module ownership")
        report = prepare(cache, firmware, output, args.firmware_revision)
        print(json.dumps({"output": str(output), "kernel_release": report["kernel"]["release"],
                          "kernel_sha256": report["kernel"]["sha256"]}))
        return 0
    except (BspError, OSError, tarfile.TarError, ValueError) as error:
        print(f"buildroot-prepare-bsp: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
