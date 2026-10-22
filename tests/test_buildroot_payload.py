"""Private runtime installation must never traverse host links or touch user data."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("buildroot_payload", REPO / "scripts/buildroot-install-payload.py")
payload = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(payload)
FAKE_KERNEL = b"isolated kernel fixture"


def write_archive(path, entries):
    with tarfile.open(path, "w:gz") as archive:
        for name, value in entries:
            member = tarfile.TarInfo(name)
            member.mode = 0o755
            if isinstance(value, tuple):
                member.type, member.linkname = value
                archive.addfile(member)
            else:
                member.size = len(value)
                archive.addfile(member, io.BytesIO(value))


def runtime_fixture(root, version="1.1.12"):
    root.mkdir()
    for name in payload.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("runtime asset " + name + " " + version).encode())
        path.chmod(0o755)
    app = bytearray(128)
    app[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", app, 18, 183)
    (root / "usr/bin/tono-canvas").write_bytes(app)
    (root / "lib").symlink_to("/usr/lib")
    (root / "bin").symlink_to("usr/bin")
    for name in ("home/avaota", "root", "run", "tmp", "dev", "proc", "sys", "etc/default", "etc/xdg/tono-kiosk"):
        (root / name).mkdir(parents=True, exist_ok=True)
    for name in payload.EMPTY_IDENTITIES + ("etc/default/tono-kiosk",):
        (root / name).write_text("")
    (root / "etc/xdg/tono-kiosk/openbox.xml").write_text("<openbox_config/>")
    (root / "etc/resolv.conf").symlink_to("/run/NetworkManager/resolv.conf")
    files = {}
    for directory, names, names_files in os.walk(root, followlinks=False):
        for name in names + names_files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if relative in payload.WRAPPER_FILES:
                continue
            if path.is_symlink():
                files["/" + relative] = {"type": "symlink", "target": os.readlink(path)}
            elif path.is_dir():
                files["/" + relative] = {"type": "directory"}
            else:
                files["/" + relative] = {"type": "file", "bytes": path.stat().st_size, "sha256": payload.sha256(path)}
    digest = "1" * 64
    manifest = {"format": 1, "native_system": "Buildroot", "app_deb": {"sha256": digest, "version": version}, "files": files}
    (root / "runtime-manifest.json").write_text(json.dumps(manifest))
    (root / "tono-runtime-inputs.json").write_text(json.dumps({"app_deb_sha256": digest}))
    archive = root.parent / (root.name + ".tar.gz")
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(root, arcname=".")
    return archive


def native_fixture(base):
    target, bsp = base / "target", base / "bsp"
    (target / "etc").mkdir(parents=True)
    (target / "etc/os-release").write_text("ID=buildroot\n")
    (target / "etc/passwd").write_text("native accounts remain\n")
    (target / "home/avaota").mkdir(parents=True)
    (target / "home/avaota/board.bin").write_bytes(b"existing user whiteboard")
    (target / "usr/lib").mkdir(parents=True)
    (target / "lib").symlink_to("usr/lib")
    modules = bsp / "root-overlay/usr/lib/modules" / payload.RELEASE
    modules.mkdir(parents=True)
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, 183)
    module = bytes(header) + b"\0vermagic=" + payload.RELEASE.encode() + b" SMP aarch64\0"
    module_digest = hashlib.sha256()
    for name in ("aic8800_bsp.ko", "aic8800_fdrv.ko"):
        (modules / name).write_bytes(module)
        module_digest.update(name.encode() + b"\0")
        module_digest.update(hashlib.sha256(module).digest())
    firmware = bsp / "root-overlay/usr/lib/firmware/aic8800"
    firmware.mkdir(parents=True)
    (firmware / "fw.bin").write_bytes(b"wifi firmware")
    boot = bsp / "boot"
    (boot / "dtb/allwinner").mkdir(parents=True)
    (boot / "Image").write_bytes(FAKE_KERNEL)
    dtb = boot / "dtb/allwinner/sun55i-t527-avaota-a1.dtb"
    dtb.write_bytes(b"prepared dtb")
    (bsp / "bootloader-syterkit.bin").write_bytes(b"prepared loader")
    (boot / "extlinux").mkdir()
    (boot / "extlinux/extlinux.conf").write_text("append root=LABEL=rootfs\n")
    for name in ("uInitrd", "bl31.bin", "scp.bin", "splash.bin"):
        (boot / name).write_bytes(name.encode())
    manifest = {"board": "avaota-a1", "kernel": {"release": payload.RELEASE, "sha256": payload.KERNEL_SHA256},
        "dtb": {"sha256": payload.sha256(dtb)},
        "bootloader": {"sha256": payload.sha256(bsp / "bootloader-syterkit.bin")},
        "uinitrd_sha256": payload.sha256(boot / "uInitrd"),
        "modules": {"release": payload.RELEASE, "count": 2, "vermagic_verified": 2,
                    "content_sha256": module_digest.hexdigest()},
        "provenance": {"boot_files": {path.relative_to(boot).as_posix(): payload.sha256(path) for path in boot.rglob("*") if path.is_file()},
                       "prebuilt_boot_assets": [{"path": name, "sha256": payload.sha256(boot / name)}
                                               for name in ("uInitrd", "bl31.bin", "scp.bin", "splash.bin")],
                       "firmware": {"files": [{"path": "aic8800/fw.bin", "sha256": payload.sha256(firmware / "fw.bin")}]}}}
    (bsp / "bsp-manifest.json").write_text(json.dumps(manifest))
    return target, bsp


class BuildrootPayloadTests(unittest.TestCase):
    def setUp(self):
        # Input fixtures use a small isolated kernel, never the production pin.
        fixture_pin = patch.object(payload, "KERNEL_SHA256", hashlib.sha256(FAKE_KERNEL).hexdigest())
        fixture_pin.start()
        self.addCleanup(fixture_pin.stop)

    def test_changed_prepared_bsp_rejected_before_native_target_changes(self):
        changes = ("boot/Image", "boot/dtb/allwinner/sun55i-t527-avaota-a1.dtb", "boot/extlinux/extlinux.conf",
                   "root-overlay/usr/lib/firmware/aic8800/fw.bin",
                   "root-overlay/usr/lib/modules/" + payload.RELEASE + "/aic8800_fdrv.ko")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for number, relative in enumerate(changes):
                with self.subTest(relative=relative):
                    base = root / str(number)
                    base.mkdir()
                    target, bsp = native_fixture(base)
                    runtime = runtime_fixture(base / "runtime")
                    path = bsp / relative
                    path.write_bytes(path.read_bytes() + b"changed")
                    with self.assertRaises(payload.PayloadError):
                        payload.install(target, runtime, bsp)
                    self.assertFalse((target / "opt/tono-runtime").exists())
                    self.assertEqual((target / "home/avaota/board.bin").read_bytes(), b"existing user whiteboard")

    def test_safe_extract_preserves_guest_absolute_links_and_tar_hardlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            archive = base / "runtime.tar.gz"
            write_archive(archive, [
                ("lib", (tarfile.SYMTYPE, "/usr/lib")),
                ("lib/drawing", b"whiteboard data"),
                ("usr/lib/drawing-copy", (tarfile.LNKTYPE, "lib/drawing")),
                ("usr/lib/drawing-link", (tarfile.SYMTYPE, "drawing")),
                ("usr/share/icons", (tarfile.SYMTYPE, "/usr/share/iconfiles")),
                ("usr/share/icons/asset", b"icon asset"),
                ("usr/lib/systemd/system/system-systemd\\x2dcryptsetup.slice", b"unit asset"),
            ])
            output = base / "out"
            result = payload.safe_extract(archive, output)
            self.assertEqual(result["files"], 3)
            self.assertEqual(os.readlink(output / "lib"), "/usr/lib")
            self.assertEqual((output / "usr/lib/drawing").read_bytes(), b"whiteboard data")
            self.assertEqual((output / "usr/lib/drawing-copy").stat().st_ino,
                             (output / "usr/lib/drawing").stat().st_ino)
            self.assertEqual(os.readlink(output / "usr/lib/drawing-link"), "drawing")
            self.assertEqual((output / "usr/share/iconfiles/asset").read_bytes(), b"icon asset")
            self.assertEqual((output / "usr/lib/systemd/system/system-systemd\\x2dcryptsetup.slice").read_bytes(), b"unit asset")

    def test_rejects_traversal_cycles_special_files_collisions_and_host_symlinks(self):
        cases = [
            [("../escape", b"bad")], [("/etc/escape", b"bad")],
            [("escape", (tarfile.SYMTYPE, "../../escape"))],
            [("a", (tarfile.SYMTYPE, "b")), ("b", (tarfile.SYMTYPE, "a"))],
            [("a", (tarfile.LNKTYPE, "../escape"))],
            [("a", (tarfile.CHRTYPE, ""))],
            [("a", b"file"), ("a/child", b"bad")],
            [("lib", (tarfile.SYMTYPE, "usr/lib")), ("lib/a", b"one"), ("usr/lib/a", b"two")],
        ]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            marker = base / "escape"
            marker.write_bytes(b"outside unchanged")
            for number, entries in enumerate(cases):
                with self.subTest(number=number):
                    archive = base / f"bad{number}.tar.gz"
                    output = base / f"out{number}"
                    write_archive(archive, entries)
                    with self.assertRaises(payload.PayloadError):
                        payload.safe_extract(archive, output)
                    self.assertFalse(output.exists())
                    self.assertEqual(marker.read_bytes(), b"outside unchanged")
            alias = base / "alias"
            alias.symlink_to(base, target_is_directory=True)
            with self.assertRaises(payload.PayloadError):
                payload.safe_extract(archive, alias)
            nonempty = base / "nonempty"
            nonempty.mkdir()
            (nonempty / "keep").write_text("keep")
            with self.assertRaises(payload.PayloadError):
                payload.safe_extract(archive, nonempty)
            self.assertEqual((nonempty / "keep").read_text(), "keep")

    def test_runtime_rejects_identity_userdata_undeclared_files_and_tampering(self):
        changes = ["etc/passwd", "home/avaota/personal.board", "etc/ssh/ssh_host_ed25519_key",
                   "etc/NetworkManager/system-connections/private.nmconnection", "usr/bin/undeclared"]
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            for number, relative in enumerate(changes):
                with self.subTest(relative=relative):
                    root = base / f"runtime{number}"
                    runtime_fixture(root)
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("private state")
                    with self.assertRaises(payload.PayloadError):
                        payload.validate_runtime(root)
            root = base / "tampered"
            runtime_fixture(root)
            app = root / "usr/bin/tono-canvas"
            data = bytearray(app.read_bytes())
            data[100] = 1
            app.write_bytes(data)
            with self.assertRaisesRegex(payload.PayloadError, "checksum mismatch"):
                payload.validate_runtime(root)
            root = base / "missing-helper"
            runtime_fixture(root)
            (root / payload.HELPERS[-1]).unlink()
            with self.assertRaisesRegex(payload.PayloadError, "required component"):
                payload.validate_runtime(root)

    def test_install_is_private_preserves_native_home_and_replaces_hardlinks_safely(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            target, bsp = native_fixture(base)
            runtime = runtime_fixture(base / "runtime")
            outside = base / "outside"
            outside.write_bytes(b"outside hardlink must remain")
            firmware = target / "usr/lib/firmware/aic8800"
            firmware.mkdir(parents=True)
            os.link(outside, firmware / "fw.bin")
            report = payload.install(target, runtime, bsp)
            self.assertEqual(report["app_version"], "1.1.12")
            self.assertTrue((target / "opt/tono-runtime/usr/bin/tono-canvas").is_file())
            self.assertEqual((target / "home/avaota/board.bin").read_bytes(), b"existing user whiteboard")
            self.assertEqual((target / "etc/passwd").read_text(), "native accounts remain\n")
            self.assertEqual(outside.read_bytes(), b"outside hardlink must remain")
            self.assertEqual((firmware / "fw.bin").read_bytes(), b"wifi firmware")
            self.assertEqual(os.readlink(target / "lib"), "usr/lib")
            self.assertFalse(any(path.name.startswith(".tono-payload-") for path in (target / "opt").iterdir()))

    def test_native_symlink_attack_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            target, bsp = native_fixture(base)
            runtime = runtime_fixture(base / "runtime")
            outside = base / "outside"
            outside.write_bytes(b"keep host file")
            firmware = target / "usr/lib/firmware/aic8800"
            firmware.mkdir(parents=True)
            (firmware / "fw.bin").symlink_to(outside)
            with self.assertRaisesRegex(payload.PayloadError, "symlink"):
                payload.install(target, runtime, bsp)
            self.assertEqual(outside.read_bytes(), b"keep host file")
            self.assertFalse((target / "opt/tono-runtime").exists())

    @unittest.skipUnless(sys.platform == "linux", "atomic cached replacement uses Linux renameat2")
    def test_cached_runtime_swap_and_failed_update_preserve_previous_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary).resolve()
            target, bsp = native_fixture(base)
            first = runtime_fixture(base / "first", "1.1.12")
            second = runtime_fixture(base / "second", "1.1.13")
            payload.install(target, first, bsp)
            payload.install(target, second, bsp)
            installed = target / "opt/tono-runtime"
            self.assertEqual(payload.validate_runtime(installed)["app_version"], "1.1.13")
            bad_root = base / "bad"
            runtime_fixture(bad_root)
            (bad_root / "etc/passwd").write_text("copied build account")
            bad = base / "bad-input.tar.gz"
            with tarfile.open(bad, "w:gz") as stream:
                stream.add(bad_root, arcname=".")
            with self.assertRaises(payload.PayloadError):
                payload.install(target, bad, bsp)
            self.assertEqual(payload.validate_runtime(installed)["app_version"], "1.1.13")
            # Even accidental data inside an old private runtime is preserved.
            private = installed / "home/avaota/keep.board"
            private.write_bytes(b"do not delete")
            with self.assertRaises(payload.PayloadError):
                payload.install(target, first, bsp)
            self.assertEqual(private.read_bytes(), b"do not delete")
            self.assertEqual((target / "home/avaota/board.bin").read_bytes(), b"existing user whiteboard")


if __name__ == "__main__":
    unittest.main()
