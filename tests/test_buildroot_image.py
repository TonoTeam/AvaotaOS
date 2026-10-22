"""A1 image layout, BootROM checksums, filesystem contents and safe publication."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
PACKER = REPO / "scripts/buildroot-pack-image.py"
SPEC = importlib.util.spec_from_file_location("buildroot_packer", PACKER)
packer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packer)
TOOLS = ("mkfs.vfat", "mcopy", "fsck.vfat", "mkfs.ext4", "e2fsck", "debugfs", "sfdisk", "blkid")
LINUX_TOOLS = sys.platform == "linux" and os.geteuid() == 0 and all(shutil.which(t) for t in TOOLS)


def loader_fixture():
    data = bytearray(4096)
    data[4:12] = b"eGON.BT0"
    struct.pack_into("<II", data, 12, packer.STAMP, len(data))
    checksum = sum(word[0] for word in struct.iter_unpack("<I", data)) & 0xFFFFFFFF
    struct.pack_into("<I", data, 12, checksum)
    return bytes(data)


def fixture(base):
    root, boot, loader = base / "root", base / "boot", base / "loader.bin"
    root.mkdir()
    for name in ("Image", packer.DTB, "bl31.bin", "scp.bin", "splash.bin"):
        path = boot / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"A1 boot fixture\n")
    (boot / "extlinux").mkdir()
    (boot / "extlinux/extlinux.conf").write_text(
        "label AvaotaOS\n  kernel /Image\n  fdt /" + packer.DTB +
        "\n  append root=LABEL=rootfs rootwait rw\n")
    loader.write_bytes(loader_fixture())
    return root, boot, loader


def call(root, boot, loader, output, *extra):
    return subprocess.run([sys.executable, str(PACKER), "--rootfs", str(root), "--boot", str(boot),
                           "--bootloader", str(loader), "--output", str(output), *extra],
                          text=True, capture_output=True)


class BuildrootImageTests(unittest.TestCase):
    def test_mbr_layout_and_bootrom_gap(self):
        root_size = 512 * packer.MIB
        mbr = packer.make_mbr(root_size, 0xA527A101)
        self.assertEqual(len(mbr), 512)
        self.assertEqual(mbr[510:], b"\x55\xaa")
        self.assertEqual(struct.unpack_from("<I", mbr, 440)[0], 0xA527A101)
        self.assertEqual(mbr[444:446], bytes(2))
        for index, expected in enumerate(((0x80, 0x0C, 32768, 229376), (0, 0x83, 262144, 1048576))):
            entry = struct.unpack_from("<B3sB3sII", mbr, 446 + index * 16)
            self.assertEqual((entry[0], entry[2], entry[4], entry[5]), expected)
        self.assertEqual(mbr[478:510], bytes(32))
        with self.assertRaises(packer.ImageError):
            packer.make_mbr(2**41, 0xA527A101)
        for disk_id in (0, -1, 0x100000000, True):
            with self.assertRaises(packer.ImageError):
                packer.make_mbr(root_size, disk_id)

    @unittest.skipUnless(sys.platform == "linux" and all(shutil.which(t) for t in ("sfdisk", "blkid")),
                         "requires Linux sfdisk/blkid")
    def test_real_tools_recognize_nonzero_mbr_identity_required_by_grower(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "identity.img"
            mbr = bytearray(packer.make_mbr(16 * packer.MIB, 0xA527A101))
            with image.open("wb") as stream:
                stream.truncate(packer.ROOT_OFFSET + 16 * packer.MIB)
                stream.write(mbr)
                stream.seek(packer.LOADER_OFFSET)
                stream.write(loader_fixture())
            def probe(*arguments):
                return subprocess.run(arguments, text=True, capture_output=True)
            self.assertEqual(probe("blkid", "--probe", "-s", "PTUUID", "-o", "value", str(image)).stdout.strip(), "a527a101")
            self.assertEqual(packer.partition_ids(image), {1: "a527a101-01", 2: "a527a101-02"})
            before = json.loads(probe("sfdisk", "--json", str(image)).stdout)["partitiontable"]
            self.assertEqual(before["id"], "0xa527a101")
            # The grower's guarded update must preserve identity and loader.
            with image.open("r+b") as stream:
                stream.truncate(packer.ROOT_OFFSET + 32 * packer.MIB)
            changed = subprocess.run(["sfdisk", "--lock=yes", "--no-reread", "--no-tell-kernel",
                                      "--wipe", "never", "--wipe-partitions", "never", "-N", "2", str(image)],
                                     input="size=65536\n", text=True, capture_output=True)
            self.assertEqual(changed.returncode, 0, changed.stderr)
            after = json.loads(probe("sfdisk", "--json", str(image)).stdout)["partitiontable"]
            before["partitions"][1]["size"] = 65536
            self.assertEqual(after, before)
            self.assertEqual(packer.partition_ids(image)[2], "a527a101-02")
            with image.open("rb") as stream:
                stream.seek(packer.LOADER_OFFSET)
                self.assertEqual(stream.read(len(loader_fixture())), loader_fixture())
            # Reproduce the legacy regression: kernel's 00000000-02 uevent is
            # insufficient because these userspace tools reject/omit zero IDs.
            with image.open("r+b") as stream:
                stream.seek(440)
                stream.write(bytes(4))
            self.assertEqual(probe("blkid", "--probe", "-s", "PTUUID", "-o", "value", str(image)).stdout.strip(), "")
            self.assertEqual(packer.partition_ids(image), {1: None, 2: None})

    def test_loader_checksum_and_partition_overlap_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "loader"
            path.write_bytes(loader_fixture())
            info = packer.validate_loader(path)
            self.assertEqual(info["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(info["offset"], 8192)
            changed = bytearray(path.read_bytes())
            changed[32] = 1
            path.write_bytes(changed)
            with self.assertRaisesRegex(packer.ImageError, "checksum mismatch"):
                packer.validate_loader(path)
            path.write_bytes(loader_fixture())
            with path.open("ab") as stream:
                stream.truncate(packer.BOOT_OFFSET)
            with self.assertRaisesRegex(packer.ImageError, "overlaps"):
                packer.validate_loader(path)

    def test_rejects_existing_outputs_before_using_host_tools(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, boot, loader = fixture(base)
            output = base / "card.img"
            output.write_bytes(b"keep existing image")
            result = call(root, boot, loader, output)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("overwrite", result.stderr)
            self.assertEqual(output.read_bytes(), b"keep existing image")
            output.unlink()
            manifest = Path(str(output) + ".manifest.json")
            manifest.write_text("keep existing manifest")
            self.assertNotEqual(call(root, boot, loader, output).returncode, 0)
            self.assertFalse(output.exists())
            self.assertEqual(manifest.read_text(), "keep existing manifest")

    def test_rejects_input_overlap_symlink_and_invalid_boot_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, boot, loader = fixture(base)
            for output in (root / "card.img", boot / "card.img"):
                result = call(root, boot, loader, output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("overlaps", result.stderr)
                self.assertFalse(output.exists())
            alias = base / "root-alias"
            alias.symlink_to(root, target_is_directory=True)
            self.assertNotEqual(call(root, boot, loader, alias / "card.img").returncode, 0)
            output = base / "card.img"
            output.symlink_to(base / "missing")
            self.assertNotEqual(call(root, boot, loader, output).returncode, 0)
            self.assertFalse((base / "missing").exists())
            output.unlink()
            (boot / packer.DTB).unlink()
            self.assertNotEqual(call(root, boot, loader, output).returncode, 0)
            self.assertFalse(output.exists())

    def test_root_sizing_has_recovery_space_and_counts_hardlinks_once(self):
        for payload, entries in ((1, 10), (3 * 1024 * packer.MIB, 200000)):
            capacity = packer.root_capacity(payload, entries)
            self.assertEqual(capacity % (16 * packer.MIB), 0)
            self.assertGreater(capacity * 99 // 100 - payload, packer.MIN_FREE)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original"
            original.write_bytes(b"x" * 5000)
            os.link(original, root / "hardlink")
            (root / "symlink").symlink_to("original")
            size, entries = packer.root_payload(root)
            self.assertEqual(entries, 4)
            self.assertEqual(size, 8192 + 4096)

    @unittest.skipUnless(LINUX_TOOLS, "requires root Linux with dosfstools/mtools/e2fsprogs")
    def test_complete_image_contents_metadata_headroom_and_manifest(self):
        with tempfile.TemporaryDirectory(prefix="a1 image test ") as temporary:
            base = Path(temporary)
            root, boot, loader = fixture(base)
            root.chmod(0o750)
            directory = root / "home/avaota"
            directory.mkdir(parents=True)
            directory.chmod(0o750)
            os.chown(directory, 1000, 1000)
            document = directory / "drawing.txt"
            document.write_text("persistent whiteboard data\n")
            document.chmod(0o640)
            os.chown(document, 1000, 1000)
            (directory / "drawing-link").symlink_to("drawing.txt")
            os.link(document, directory / "drawing-hardlink")
            # Hidden boot files must not disappear through shell '*' expansion.
            (boot / ".fixture").write_text("hidden boot asset")
            output = base / "card.img"
            result = call(root, boot, loader, output)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(Path(str(output) + ".manifest.json").read_text())
            self.assertEqual(report["sha256"], packer.sha256(output))
            self.assertEqual(report["size_bytes"], output.stat().st_size)
            self.assertGreater(int(report["disk_id"], 16), 0)
            self.assertEqual(report["verification"]["partition_ids"], "passed")
            disk_uuid = report["disk_id"][2:]
            self.assertEqual(subprocess.check_output(["blkid", "--probe", "-s", "PTUUID", "-o", "value", str(output)], text=True).strip(), disk_uuid)
            observed = packer.partition_ids(output)
            for partition in report["partitions"]:
                self.assertEqual(observed[partition["number"]], partition["partuuid"])
            self.assertEqual(report["verification"]["hardware_boot"], "not-tested")
            self.assertGreaterEqual(report["partitions"][1]["available_bytes"], packer.MIN_FREE)
            with output.open("rb") as image:
                self.assertEqual(image.read(512), packer.make_mbr(report["partitions"][1]["size_bytes"], int(report["disk_id"], 16)))
                image.seek(packer.LOADER_OFFSET)
                self.assertEqual(image.read(loader.stat().st_size), loader.read_bytes())
                for partition in report["partitions"]:
                    image.seek(partition["offset"])
                    digest = hashlib.sha256()
                    target = base / f"partition{partition['number']}"
                    with target.open("wb") as stream:
                        remaining = partition["size_bytes"]
                        while remaining:
                            chunk = image.read(min(4 * packer.MIB, remaining))
                            self.assertTrue(chunk)
                            stream.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                    self.assertEqual(digest.hexdigest(), partition["sha256"])
            fs = base / "partition2"
            def debug(command):
                return subprocess.run(["debugfs", "-R", command, str(fs)],
                                      capture_output=True, text=True, check=True).stdout
            self.assertIn("Mode:  0750", debug("stat /"))
            metadata = debug("stat /home/avaota/drawing.txt")
            self.assertIn("Mode:  0640", metadata)
            self.assertRegex(metadata, r"User:\s+1000\s+Group:\s+1000")
            self.assertIn('Fast link dest: "drawing.txt"', debug("stat /home/avaota/drawing-link"))
            self.assertIn("persistent whiteboard data", debug("cat /home/avaota/drawing.txt"))
            original_inode = re_inode(metadata)
            self.assertEqual(original_inode, re_inode(debug("stat /home/avaota/drawing-hardlink")))
            content = subprocess.run(["mtype", "-i", str(base / "partition1"), "::/.fixture"],
                                     capture_output=True, text=True, check=True).stdout
            self.assertEqual(content, "hidden boot asset")
            # A repeated invocation must preserve both successfully built outputs.
            old_manifest = Path(str(output) + ".manifest.json").read_bytes()
            self.assertNotEqual(call(root, boot, loader, output).returncode, 0)
            self.assertEqual(Path(str(output) + ".manifest.json").read_bytes(), old_manifest)
            self.assertEqual(packer.sha256(output), report["sha256"])
            self.assertEqual(document.read_text(), "persistent whiteboard data\n")


def re_inode(output):
    import re
    match = re.search(r"Inode:\s+(\d+)", output)
    if not match:
        raise AssertionError(output)
    return int(match.group(1))


if __name__ == "__main__":
    unittest.main()
