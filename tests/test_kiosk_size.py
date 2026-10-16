"""Check compaction boundaries and preservation of App runtime/maintenance data."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
TRIM = REPO / "scripts/trim-kiosk-rootfs.py"


class KioskSizeTests(unittest.TestCase):
    def run_trim(self, root):
        return subprocess.run([sys.executable, str(TRIM), str(root)], text=True, capture_output=True)

    def fixture(self, root):
        path = root / "etc/avaota-release"
        path.parent.mkdir(parents=True)
        path.write_text("DISTRIBUTION=noble\nDESKTOP=kiosk\n")

    def test_keeps_runtime_data_licenses_and_chinese_english(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            kept = [
                "usr/share/doc/example/copyright", "usr/share/doc/example/LICENSE.gz",
                "usr/share/doc/example/legal/NOTICE", "usr/share/common-licenses/GPL-3",
                "usr/share/locale/locale.alias", "usr/share/locale/zh_CN/LC_MESSAGES/app.mo",
                "usr/share/locale/zh_TW/LC_MESSAGES/app.mo", "usr/share/locale/en_GB/LC_MESSAGES/app.mo",
                "usr/share/fonts/opentype/noto/cjk.ttc", "usr/share/qt6/resources/qtwebengine_resources.pak",
                "usr/share/qt6/translations/qtwebengine_locales/ja.pak",
                "usr/share/libime/pinyin.dict", "usr/lib/firmware/wifi.bin",
                "usr/lib/aarch64-linux-gnu/libEGL.so.1", "usr/local/share/doc/tono-kiosk/kiosk-recovery.md",
                "var/lib/dpkg/status", "var/cache/fontconfig/fonts.cache",
                "etc/NetworkManager/system-connections/saved.nmconnection",
                "home/avaota/board.bin", "etc/ssh/ssh_host_ed25519_key",
            ]
            removed = [
                "var/lib/apt/lists/packages", "var/lib/apt/lists/partial/download",
                "var/cache/apt/pkgcache.bin", "var/cache/apt/srcpkgcache.bin",
                "var/cache/apt/archives/package.deb", "var/cache/apt/archives/partial/download",
                "usr/share/doc/example/README", "usr/share/doc/example/changelog.gz",
                "usr/share/man/man1/example.1.gz", "usr/share/info/example.info",
                "usr/share/locale/fr/LC_MESSAGES/app.mo", "usr/share/locale/ja/LC_MESSAGES/app.mo",
            ]
            for relative in kept + removed:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode())
            (root / "usr/share/doc/alias").symlink_to("example", target_is_directory=True)
            result = self.run_trim(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            for relative in kept:
                self.assertEqual((root / relative).read_bytes(), relative.encode(), relative)
            for relative in removed:
                self.assertFalse((root / relative).exists(), relative)
            self.assertTrue((root / "usr/share/doc/alias/copyright").exists())
            for path in ("var/lib/apt/lists/partial", "var/cache/apt/archives/partial"):
                self.assertTrue((root / path).is_dir())
            result = self.run_trim(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('"files": 0', result.stdout)

    def test_rejects_non_kiosk_and_symlinked_roots_before_removing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, outside = base / "root", base / "outside"
            self.fixture(root)
            outside.mkdir()
            marker = outside / "keep"
            marker.write_text("keep")
            (root / "var/lib/apt").mkdir(parents=True)
            (root / "var/lib/apt/lists").symlink_to(outside, target_is_directory=True)
            self.assertNotEqual(self.run_trim(root).returncode, 0)
            self.assertEqual(marker.read_text(), "keep")
            (root / "etc/avaota-release").write_text("DESKTOP=gnome\n")
            self.assertNotEqual(self.run_trim(root).returncode, 0)
            self.assertEqual(marker.read_text(), "keep")

    def test_cleanup_does_not_follow_child_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, outside = base / "root", base / "outside"
            self.fixture(root)
            outside.mkdir()
            marker = outside / "keep"
            marker.write_text("keep")
            (root / "var/lib/apt/lists").mkdir(parents=True)
            (root / "var/lib/apt/lists/linked").symlink_to(outside, target_is_directory=True)
            self.assertEqual(self.run_trim(root).returncode, 0)
            self.assertEqual(marker.read_text(), "keep")

    def test_sizing_scales_with_payload_without_the_fixed_gigabyte(self):
        for payload in (100, 1200, 4000):
            command = f'source "$1"; kiosk_image_size {payload} 140 4 128'
            result = subprocess.run(["bash", "-ec", command, "test", str(REPO / "scripts/lib/kiosk.sh")],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            image = int(result.stdout)
            self.assertEqual(image % 16, 0)
            self.assertGreaterEqual(image - 128 - payload - 140 - 4, 256)
            self.assertLess(image, payload + payload // 5 + 140 + 256 + 1024)

    @unittest.skipUnless(sys.platform == "linux" and shutil.which("dpkg-deb"), "requires Linux dpkg")
    def test_dpkg_filters_preserve_runtime_and_license_on_future_installs(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, package = base / "root", base / "package"
            self.fixture(root)
            self.assertEqual(self.run_trim(root).returncode, 0)
            control = package / "DEBIAN/control"
            control.parent.mkdir(parents=True)
            control.write_text("Package: tono-size-fixture\nVersion: 1.0\nArchitecture: all\n"
                               "Maintainer: Test <test@example.invalid>\nDescription: size fixture\n")
            files = {"usr/share/doc/tono-size-fixture/copyright": True,
                     "usr/share/doc/tono-size-fixture/LICENSE": True,
                     "usr/share/doc/tono-size-fixture/README": False,
                     "usr/share/man/man1/fixture.1": False,
                     "usr/share/locale/zh_CN/LC_MESSAGES/fixture.mo": True,
                     "usr/share/locale/fr/LC_MESSAGES/fixture.mo": False,
                     "usr/share/qt6/resources/fixture.pak": True}
            for relative in files:
                path = package / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture")
            deb = base / "fixture.deb"
            subprocess.run(["dpkg-deb", "--build", str(package), str(deb)], check=True, capture_output=True)
            options = ["--" + line for line in (root / "etc/dpkg/dpkg.cfg.d/01-tono-kiosk-size").read_text().splitlines()
                       if line and not line.startswith("#")]
            result = subprocess.run(["dpkg", "--root=" + str(root), "--force-not-root", "--force-bad-path",
                                     *options, "--install", str(deb)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            for relative, present in files.items():
                self.assertEqual((root / relative).exists(), present, relative)


if __name__ == "__main__":
    unittest.main()
