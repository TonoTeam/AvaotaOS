#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0
"""Pack an Avaota A1 Buildroot root tree without loop devices or mounts.

Run as root on Linux so mke2fs preserves numeric ownership, modes, device nodes,
symlinks and extended attributes from the prepared Buildroot target directory.
Inputs are never modified. Existing outputs are never overwritten.
"""
import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import struct
import subprocess
import sys
import tempfile

MIB = 1024 * 1024
SECTOR = 512
LOADER_OFFSET = 8192
BOOT_OFFSET = 16 * MIB
ROOT_OFFSET = 128 * MIB
BOOT_SIZE = ROOT_OFFSET - BOOT_OFFSET
MIN_FREE = 256 * MIB
STAMP = 0x5F0A6C39
DTB = "dtb/allwinner/sun55i-t527-avaota-a1.dtb"


class ImageError(Exception):
    """A rejected input or failed image verification."""


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(arguments):
    result = subprocess.run([str(item) for item in arguments], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise ImageError(f"{arguments[0]} failed ({result.returncode}):\n{result.stdout}")
    return result.stdout


def inside(path, directory):
    return path == directory or directory in path.parents


def output_path(value, inputs):
    raw = Path(value).absolute()
    if raw.is_symlink() or raw.exists():
        raise ImageError(f"Refusing to overwrite existing output: {raw}")
    path = raw.parent.resolve(strict=True) / raw.name
    if any(inside(path, source) for source in inputs):
        raise ImageError(f"Output overlaps an input tree: {path}")
    return path


def validate_loader(path):
    data = path.read_bytes()
    if len(data) < 32 or data[4:12] != b"eGON.BT0":
        raise ImageError("Bootloader lacks the Allwinner eGON.BT0 header")
    checksum, length = struct.unpack_from("<II", data, 12)
    if length < 32 or length % 4 or length > len(data):
        raise ImageError("Invalid Allwinner bootloader checksum length")
    if len(data) > BOOT_OFFSET - LOADER_OFFSET:
        raise ImageError("Bootloader overlaps the first partition")
    payload = bytearray(data[:length])
    struct.pack_into("<I", payload, 12, STAMP)
    actual = sum(word[0] for word in struct.iter_unpack("<I", payload)) & 0xFFFFFFFF
    if checksum != actual:
        raise ImageError("Allwinner bootloader checksum mismatch")
    return {"offset": LOADER_OFFSET, "size": len(data), "checksum_size": length,
            "egon_checksum": f"{checksum:08x}", "sha256": hashlib.sha256(data).hexdigest()}


def boot_file(root, relative):
    relative = relative.lstrip("/")
    path = root / relative
    if not inside(path.resolve(strict=True), root) or not path.is_file() or not path.stat().st_size:
        raise ImageError(f"Missing or external boot file: {relative}")
    return path


def validate_boot(root):
    # FAT cannot preserve Unix symlinks or special files. Reject them explicitly
    # rather than silently changing the meaning of a prepared boot tree.
    total = 0
    folded = set()
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            path = Path(directory) / name
            info = path.lstat()
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ImageError(f"Boot tree contains a symlink or special file: {path}")
            relative = path.relative_to(root).as_posix().casefold()
            if relative in folded:
                raise ImageError(f"Boot filenames collide on FAT: {path}")
            folded.add(relative)
            total += ((info.st_size + 4095) // 4096) * 4096
    # Include ample FAT and directory headroom; rootfs headroom is checked after
    # formatting too, rather than assuming du equals filesystem consumption.
    if total > BOOT_SIZE - 8 * MIB:
        raise ImageError("Boot payload does not fit the fixed 112 MiB FAT partition")
    for relative in ("Image", DTB, "bl31.bin", "scp.bin", "splash.bin"):
        boot_file(root, relative)
    config = boot_file(root, "extlinux/extlinux.conf").read_text()
    if not re.search(r"\broot=LABEL=rootfs(?:\s|$)", config):
        raise ImageError("extlinux must select root=LABEL=rootfs")
    entries = {}
    for line in config.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() in ("kernel", "linux", "fdt", "initrd"):
            entries[parts[0].lower()] = parts[1].strip()
    if entries.get("fdt") != "/" + DTB or not ("kernel" in entries or "linux" in entries):
        raise ImageError("extlinux must name the Avaota A1 DTB and kernel")
    for value in entries.values():
        for relative in value.split(","):
            boot_file(root, relative)
    return total


def root_payload(root):
    size, entries = 0, 1
    inodes = set()
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            info = (Path(directory) / name).lstat()
            entries += 1
            if stat.S_ISSOCK(info.st_mode):
                raise ImageError("Root tree contains a live Unix socket; stop runtime services first")
            key = (info.st_dev, info.st_ino)
            if key in inodes:
                continue
            inodes.add(key)
            # Apparent sizes conservatively cover sparse files as well.
            size += ((info.st_size + 4095) // 4096) * 4096
    return size, entries


def aligned(value):
    return ((value + 16 * MIB - 1) // (16 * MIB)) * 16 * MIB


def root_capacity(payload, entries, free_bytes=MIN_FREE):
    metadata = max(64 * MIB, payload // 20 + entries * 1024)
    # Reserve 1% for privileged recovery and still leave 256 MiB available.
    return aligned((payload + free_bytes + metadata) * 100 // 99 + 1)


def ext4_stats(path):
    with path.open("rb") as stream:
        stream.seek(1024)
        block = stream.read(1024)
    if len(block) != 1024 or struct.unpack_from("<H", block, 56)[0] != 0xEF53:
        raise ImageError("Invalid ext4 superblock")
    blocks, reserved, free = struct.unpack_from("<III", block, 4)
    if struct.unpack_from("<I", block, 96)[0] & 0x80:  # EXT4_FEATURE_INCOMPAT_64BIT
        upper = struct.unpack_from("<III", block, 336)
        blocks |= upper[0] << 32
        reserved |= upper[1] << 32
        free |= upper[2] << 32
    block_size = 1024 << struct.unpack_from("<I", block, 24)[0]
    import uuid
    return {"block_size": block_size, "blocks": blocks,
            "free_bytes": free * block_size,
            "available_bytes": (free - reserved) * block_size,
            "uuid": str(uuid.UUID(bytes=block[104:120]))}


def make_mbr(root_size, disk_id):
    if not isinstance(disk_id, int) or isinstance(disk_id, bool) or not 1 <= disk_id <= 0xFFFFFFFF:
        raise ImageError("MBR disk ID must be a nonzero 32-bit integer")
    if (ROOT_OFFSET + root_size) // SECTOR > 0xFFFFFFFF:
        raise ImageError("Image exceeds the MBR 2 TiB limit")
    mbr = bytearray(SECTOR)
    struct.pack_into("<I", mbr, 440, disk_id)
    for index, (active, kind, offset, size) in enumerate((
            (0x80, 0x0C, BOOT_OFFSET, BOOT_SIZE), (0, 0x83, ROOT_OFFSET, root_size))):
        struct.pack_into("<B3sB3sII", mbr, 446 + index * 16, active, b"\xfe\xff\xff",
                         kind, b"\xfe\xff\xff", offset // SECTOR, size // SECTOR)
    mbr[510:512] = b"\x55\xaa"
    return bytes(mbr)


def partition_ids(path):
    """Read synthesized DOS PARTUUIDs with host libblkid, without loop devices.

    sfdisk --part-uuid exposes stored GPT UUIDs but does not synthesize DOS
    UUIDs on supported Ubuntu hosts. libblkid uses the same DOS identity path
    as blkid does when probing a real MMC partition.
    """
    library = ctypes.CDLL("libblkid.so.1")
    pointer, integer = ctypes.c_void_p, ctypes.c_int
    declarations = (
        ("blkid_new_probe_from_filename", pointer, (ctypes.c_char_p,)),
        ("blkid_probe_enable_partitions", integer, (pointer, integer)),
        ("blkid_probe_get_partitions", pointer, (pointer,)),
        ("blkid_partlist_numof_partitions", integer, (pointer,)),
        ("blkid_partlist_get_partition", pointer, (pointer, integer)),
        ("blkid_partition_get_uuid", ctypes.c_char_p, (pointer,)),
        ("blkid_partition_get_partno", integer, (pointer,)),
        ("blkid_free_probe", None, (pointer,)),
    )
    for name, result, arguments in declarations:
        function = getattr(library, name)
        function.restype, function.argtypes = result, arguments
    probe = library.blkid_new_probe_from_filename(os.fsencode(path))
    if not probe:
        raise ImageError("libblkid could not open the generated image")
    try:
        if library.blkid_probe_enable_partitions(probe, 1) != 0:
            raise ImageError("libblkid could not enable partition probing")
        partitions = library.blkid_probe_get_partitions(probe)
        if not partitions:
            raise ImageError("libblkid could not read the generated partition table")
        values = {}
        for index in range(library.blkid_partlist_numof_partitions(partitions)):
            partition = library.blkid_partlist_get_partition(partitions, index)
            number = library.blkid_partition_get_partno(partition)
            value = library.blkid_partition_get_uuid(partition)
            values[number] = value.decode("ascii").lower() if value else None
        return values
    finally:
        library.blkid_free_probe(probe)


def sparse_copy(source, destination, offset):
    """Copy filesystem extents without allocating all of a mostly empty image."""
    with source.open("rb") as stream:
        length = source.stat().st_size
        position = 0
        while position < length:
            try:
                start = os.lseek(stream.fileno(), position, os.SEEK_DATA)
                end = os.lseek(stream.fileno(), start, os.SEEK_HOLE)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    break
                if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
                start, end = position, length
            stream.seek(start)
            destination.seek(offset + start)
            while start < end:
                chunk = stream.read(min(4 * MIB, end - start))
                if not chunk:
                    raise ImageError(f"Unexpected end of filesystem image: {source}")
                if chunk.count(0) == len(chunk):
                    destination.seek(len(chunk), os.SEEK_CUR)
                else:
                    destination.write(chunk)
                start += len(chunk)
            position = end


def assemble(root, boot, loader, output, manifest):
    loader_info = validate_loader(loader)
    boot_bytes = validate_boot(boot)
    payload, entries = root_payload(root)
    for tool in ("mkfs.vfat", "mcopy", "fsck.vfat", "mkfs.ext4", "e2fsck", "debugfs", "sfdisk", "blkid"):
        if not shutil.which(tool):
            raise ImageError(f"Missing host tool: {tool} (install dosfstools mtools e2fsprogs fdisk util-linux)")
    with tempfile.TemporaryDirectory(prefix=".avaota-buildroot-", dir=output.parent) as scratch:
        scratch = Path(scratch)
        boot_image, root_image = scratch / "boot.fat", scratch / "root.ext4"
        with boot_image.open("wb") as stream:
            stream.truncate(BOOT_SIZE)
        run(["mkfs.vfat", "-F", "32", "-n", "boot", boot_image])
        # Pass filenames as separate argv values; paths with spaces are supported.
        for child in sorted(boot.iterdir()):
            run(["mcopy", "-i", boot_image, "-s", child, "::/"])
        run(["fsck.vfat", "-n", boot_image])
        root_size = root_capacity(payload, entries)
        for attempt in range(3):
            with root_image.open("wb") as stream:
                stream.truncate(root_size)
            run(["mkfs.ext4", "-q", "-F", "-b", "4096", "-m", "1", "-L", "rootfs",
                 "-N", entries + max(16384, entries // 4), "-d", root, root_image])
            # mke2fs populates children; explicitly preserve the source root inode.
            info = root.stat()
            for field, value in (("mode", f"0{info.st_mode:o}"), ("uid", info.st_uid), ("gid", info.st_gid)):
                run(["debugfs", "-w", "-R", f"set_inode_field / {field} {value}", root_image])
            run(["e2fsck", "-fn", root_image])
            root_info = ext4_stats(root_image)
            if root_info["available_bytes"] >= MIN_FREE:
                break
            root_size = aligned(root_size + MIN_FREE - root_info["available_bytes"] + 16 * MIB)
        else:
            raise ImageError("Unable to retain 256 MiB available rootfs space")
        image = scratch / "sdcard.img"
        disk_id = secrets.randbelow(0xFFFFFFFF) + 1
        disk_uuid = f"{disk_id:08x}"
        with image.open("w+b") as stream:
            stream.truncate(ROOT_OFFSET + root_size)
            stream.write(make_mbr(root_size, disk_id))
            sparse_copy(loader, stream, LOADER_OFFSET)
            sparse_copy(boot_image, stream, BOOT_OFFSET)
            sparse_copy(root_image, stream, ROOT_OFFSET)
            stream.flush()
            os.fsync(stream.fileno())
        # A zero MBR signature appears in the kernel's uevent but libblkid and
        # libblkid omits its UUID. The first-boot grower requires both identities.
        if run(["blkid", "--probe", "-s", "PTUUID", "-o", "value", image]).strip().lower() != disk_uuid:
            raise ImageError("libblkid did not recognize the MBR disk identity")
        table = json.loads(run(["sfdisk", "--json", image]))["partitiontable"]
        if table.get("label") != "dos" or table.get("id", "").lower() != "0x" + disk_uuid:
            raise ImageError("sfdisk did not recognize the MBR disk identity")
        identities = partition_ids(image)
        expected_ids = {number: f"{disk_uuid}-{number:02d}" for number in (1, 2)}
        if identities != expected_ids:
            raise ImageError("libblkid did not recognize both MBR partition identities")
        report = {
            "schema_version": 1, "board": "avaota-a1", "soc": "allwinner-a527",
            "distribution": "buildroot", "image": output.name,
            "size_bytes": image.stat().st_size, "sha256": sha256(image),
            "bootloader": loader_info, "disk_id": "0x" + disk_uuid,
            "partitions": [
                {"number": 1, "partuuid": disk_uuid + "-01", "type": "0x0c", "filesystem": "vfat", "label": "boot",
                 "offset": BOOT_OFFSET, "size_bytes": BOOT_SIZE, "payload_bytes": boot_bytes,
                 "sha256": sha256(boot_image)},
                {"number": 2, "partuuid": disk_uuid + "-02", "type": "0x83", "filesystem": "ext4", "label": "rootfs",
                 "offset": ROOT_OFFSET, "size_bytes": root_size,
                 "payload_bytes": payload, "source_entries": entries,
                 "sha256": sha256(root_image), **root_info}],
            "verification": {"fat_fsck": "passed", "ext4_fsck": "passed",
                             "bootrom_checksum": "passed", "partition_ids": "passed", "hardware_boot": "not-tested"},
        }
        manifest_temp = scratch / "manifest.json"
        manifest_temp.write_text(json.dumps(report, indent=2) + "\n")
        # Hard links provide atomic no-replace publication, including against a
        # competing process creating an output after the initial validation.
        published = []
        try:
            for source, target in ((image, output), (manifest_temp, manifest)):
                os.link(source, target)
                published.append(target)
        except OSError:
            for target in published:
                target.unlink()
            raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rootfs", required=True, help="prepared Buildroot target root directory")
    parser.add_argument("--boot", required=True, help="prepared A1 FAT boot tree")
    parser.add_argument("--bootloader", required=True, help="SyterKit *_bin_card.bin")
    parser.add_argument("--output", required=True, help="new complete SD image filename")
    parser.add_argument("--manifest", help="new manifest filename (default: OUTPUT.manifest.json)")
    arguments = parser.parse_args(argv)
    try:
        root, boot, loader = (Path(value).resolve(strict=True) for value in
                              (arguments.rootfs, arguments.boot, arguments.bootloader))
        if not root.is_dir() or not boot.is_dir() or not loader.is_file() or root == Path("/") or boot == Path("/"):
            raise ImageError("Expected prepared rootfs/boot directories and a regular bootloader file")
        output = output_path(arguments.output, (root, boot, loader))
        manifest = output_path(arguments.manifest or str(output) + ".manifest.json", (root, boot, loader))
        if output == manifest:
            raise ImageError("Image and manifest must use different paths")
        # Validate boot inputs on every host, before a potentially expensive build.
        validate_loader(loader)
        validate_boot(boot)
        if sys.platform != "linux" or os.geteuid() != 0:
            raise ImageError("Run this packer as root on Linux to preserve rootfs metadata")
        report = assemble(root, boot, loader, output, manifest)
        print(json.dumps({"image": str(output), "manifest": str(manifest),
                          "size_bytes": report["size_bytes"], "sha256": report["sha256"]}))
        return 0
    except (ImageError, OSError, ValueError) as error:
        print(f"buildroot-pack-image: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
