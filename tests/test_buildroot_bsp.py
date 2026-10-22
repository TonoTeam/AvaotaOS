"""Safe BSP reuse, ARM64 ABI checks, SD detection patch and fresh outputs."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts/buildroot-prepare-bsp.py"
SPEC = importlib.util.spec_from_file_location("buildroot_bsp", SCRIPT)
bsp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bsp)
TOOLS = sys.platform == "linux" and os.geteuid() == 0 and all(shutil.which(t) for t in ("dtc", "fdtget", "fdtput"))


def archive(path, entries):
    with tarfile.open(path, "w:gz") as stream:
        for name, value in entries.items():
            member = tarfile.TarInfo(name)
            member.mode = 0o644
            if isinstance(value, tarfile.TarInfo):
                stream.addfile(value)
            else:
                member.size = len(value)
                stream.addfile(member, io.BytesIO(value))


def module(release=bsp.RELEASE):
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, 183)
    return bytes(header) + b"\0vermagic=" + release.encode() + b" SMP preempt mod_unload aarch64\0"


def loader():
    data = bytearray(4096)
    data[4:12] = b"eGON.BT0"
    struct.pack_into("<II", data, 12, 0x5F0A6C39, len(data))
    struct.pack_into("<I", data, 12, sum(w[0] for w in struct.iter_unpack("<I", data)) & 0xFFFFFFFF)
    return bytes(data)


class BuildrootBspTests(unittest.TestCase):
    def test_allowlist_excludes_debs_headers_and_host_links(self):
        self.assertIsNone(bsp.destination("avaota-a1-kernel-pkgs/linux-image.deb"))
        self.assertIsNone(bsp.destination("deb-data/headers/usr/src/header.h"))
        self.assertIsNone(bsp.destination("deb-data/image/lib/modules/" + bsp.RELEASE + "/build"))
        self.assertIsNone(bsp.destination("deb-data/image/lib/modules/" + bsp.RELEASE + "/source"))
        self.assertEqual(bsp.destination("deb-data/image/lib/modules/" + bsp.RELEASE + "/kernel/a.ko"),
                         "root-overlay/usr/lib/modules/" + bsp.RELEASE + "/kernel/a.ko")
        self.assertEqual(bsp.destination("deb-data/dtb/boot/dtb-" + bsp.RELEASE + "/allwinner/" + bsp.DTB_NAME),
                         "boot/dtb/allwinner/" + bsp.DTB_NAME)
        for name in ("/bootloader-syterkit.bin", "bootloader-avaota-a1/../../escape", "deb-data/image/boot/../escape"):
            with self.assertRaises(bsp.BspError):
                bsp.destination(name)

    def test_kernel_is_pinned_and_required_sandbox_gpu_audio_flags_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image, config = root / "Image", root / "config"
            content = bytearray(64)
            content[56:60] = b"ARM\x64"
            image.write_bytes(content)
            options = "\n".join("CONFIG_" + name + "=y" for name in bsp.REQUIRED_CONFIG)
            config.write_text(options + "\nCONFIG_AIC8800_WLAN_SUPPORT=m\n")
            with self.assertRaisesRegex(bsp.BspError, "SHA256"):
                bsp.validate_kernel(image, config)
            with patch.object(bsp, "KERNEL_SHA256", hashlib.sha256(content).hexdigest()):
                report = bsp.validate_kernel(image, config)
                self.assertEqual(report["required_config"]["CONFIG_AW_DRM_PANFROST"], "y")
                config.write_text(options.replace("CONFIG_USER_NS=y", "# CONFIG_USER_NS is not set") +
                                  "\nCONFIG_AIC8800_WLAN_SUPPORT=m\n")
                with self.assertRaisesRegex(bsp.BspError, "USER_NS"):
                    bsp.validate_kernel(image, config)
            image.write_bytes(b"compressed or wrong architecture")
            with self.assertRaisesRegex(bsp.BspError, "ARM64"):
                bsp.validate_kernel(image, config)

    def test_module_abi_and_wifi_driver_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(bsp.BspError, "AIC8800"):
                bsp.validate_modules(root)
            for name in ("aic8800_bsp.ko", "aic8800_fdrv.ko"):
                (root / name).write_bytes(module())
            self.assertEqual(bsp.validate_modules(root)["vermagic_verified"], 2)
            (root / "aic8800_bsp.ko").write_bytes(module("5.15.999-wrong"))
            with self.assertRaisesRegex(bsp.BspError, "ABI differs"):
                bsp.validate_modules(root)

    def test_existing_output_and_external_firmware_links_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, firmware = root / "output", root / "firmware"
            output.mkdir()
            marker = output / "keep"
            marker.write_text("existing data")
            with self.assertRaisesRegex(bsp.BspError, "overwrite"):
                bsp.prepare(root / "missing.tar.gz", firmware, output)
            self.assertEqual(marker.read_text(), "existing data")
            (firmware / "aic8800").mkdir(parents=True)
            (firmware / "aic8800/fw.bin").write_bytes(b"firmware")
            (firmware / "external").symlink_to(marker)
            with self.assertRaisesRegex(bsp.BspError, "escapes"):
                bsp.copy_firmware(firmware, root / "copied")
            self.assertFalse((root / "copied").exists())

    def test_provenance_records_pins_and_does_not_assume_firmware_gpl(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            firmware, boot = root / "firmware", root / "boot"
            firmware.mkdir()
            boot.mkdir()
            (firmware / "fw.bin").write_bytes(b"vendor firmware")
            for name in ("bl31.bin", "scp.bin", "splash.bin", "uInitrd"):
                (boot / name).write_bytes(name.encode())
            report = bsp.provenance(firmware, boot, "f" * 40)
            self.assertEqual(report["kernel"]["commit"], bsp.KERNEL_COMMIT)
            self.assertEqual(report["syterkit"]["commit"], bsp.SYTERKIT_COMMIT)
            self.assertEqual(report["firmware"]["repository_commit"], "f" * 40)
            self.assertEqual(report["firmware"]["files"][0]["sha256"], bsp.sha256(firmware / "fw.bin"))
            self.assertEqual(report["firmware"]["files"][0]["license"], "not-established")
            self.assertTrue(all(item["license"] == "component-notices-not-collected"
                                for item in report["prebuilt_boot_assets"]))
            with self.assertRaisesRegex(bsp.BspError, "40-character"):
                bsp.provenance(firmware, boot, "not-a-commit")

    @unittest.skipUnless(TOOLS, "requires root Linux and device-tree-compiler")
    def test_prepare_patches_actual_dtb_preserves_payload_and_excludes_other_archive_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dts = root / "test.dts"
            dts.write_text('/dts-v1/; / { soc@3000000 { sdmmc@4020000 { compatible = "allwinner,sunxi-mmc-v5p3x"; '
                           'cd-gpios = <1 2 3>; cd-set-debounce = <10>; cd-inverted; }; }; };\n')
            dtb = root / "test.dtb"
            subprocess.run(["dtc", "-I", "dts", "-O", "dtb", "-o", str(dtb), str(dts)],
                           capture_output=True, check=True)
            kernel = bytearray(4096)
            kernel[56:60] = b"ARM\x64"
            module_prefix = "deb-data/image/lib/modules/" + bsp.RELEASE + "/"
            entries = {
                "bootloader-syterkit.bin": loader(),
                "bootloader-avaota-a1/bl31.bin": b"bl31",
                "bootloader-avaota-a1/scp.bin": b"scp",
                "bootloader-avaota-a1/splash.bin": b"splash",
                "bootloader-avaota-a1/uInitrd": b"existing tested initramfs",
                "deb-data/image/boot/vmlinuz-" + bsp.RELEASE: bytes(kernel),
                "deb-data/image/boot/config-" + bsp.RELEASE: (
                    "\n".join("CONFIG_" + name + "=y" for name in bsp.REQUIRED_CONFIG) +
                    "\nCONFIG_AIC8800_WLAN_SUPPORT=m\n").encode(),
                "deb-data/dtb/boot/dtb-" + bsp.RELEASE + "/allwinner/" + bsp.DTB_NAME: dtb.read_bytes(),
                module_prefix + "kernel/aic8800_bsp.ko": module(),
                module_prefix + "kernel/aic8800_fdrv.ko": module(),
                "avaota-a1-kernel-pkgs/ignored.deb": b"unneeded package",
                "deb-data/image/etc/kernel/postinst.d/ignored": b"unneeded apt hook",
            }
            host_link = tarfile.TarInfo(module_prefix + "source")
            host_link.type, host_link.linkname = tarfile.SYMTYPE, "/private/build-machine/kernel"
            entries[host_link.name] = host_link
            cache = root / "bsp.tar.gz"
            archive(cache, entries)
            firmware = root / "firmware"
            (firmware / "aic8800").mkdir(parents=True)
            (firmware / "aic8800/fw.bin").write_bytes(b"firmware")
            output = root / "prepared"
            with patch.object(bsp, "KERNEL_SHA256", hashlib.sha256(kernel).hexdigest()):
                report = bsp.prepare(cache, firmware, output)
            self.assertEqual((output / "boot/Image").read_bytes(), kernel)
            self.assertTrue(report["dtb"]["non_removable"])
            props = bsp.run(["fdtget", "-p", output / "boot/dtb/allwinner" / bsp.DTB_NAME, bsp.SD_NODE]).splitlines()
            self.assertIn("non-removable", props)
            self.assertNotIn("cd-gpios", props)
            self.assertNotIn("cd-inverted", props)
            self.assertNotEqual(report["dtb"]["sha256_before"], report["dtb"]["sha256"])
            config = (output / "boot/extlinux/extlinux.conf").read_text()
            self.assertIn("root=LABEL=rootfs", config)
            self.assertIn("quiet loglevel=3 systemd.show_status=auto", config)
            self.assertNotIn("earlycon", config)
            self.assertEqual(report["modules"]["vermagic_verified"], 2)
            self.assertFalse((output / "root-overlay/usr/lib/modules" / bsp.RELEASE / "source").exists())
            self.assertEqual((output / "root-overlay/usr/lib/firmware/aic8800/fw.bin").read_bytes(), b"firmware")
            self.assertFalse((output / "avaota-a1-kernel-pkgs").exists())
            self.assertEqual(json.loads((output / "bsp-manifest.json").read_text())["bsp_cache_sha256"], bsp.sha256(cache))
            # Unsafe selected archive names must fail and remove only fresh output.
            malicious = root / "bad.tar.gz"
            archive(malicious, {**entries, "bootloader-avaota-a1/../../escape": b"bad"})
            with self.assertRaisesRegex(bsp.BspError, "Unsafe"):
                bsp.prepare(malicious, firmware, root / "rejected")
            self.assertFalse((root / "rejected").exists())
            self.assertFalse((root / "escape").exists())


if __name__ == "__main__":
    unittest.main()
