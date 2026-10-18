"""Host-side regression checks; no mounts, package installation or root required."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import stat
import tarfile
import tempfile
import textwrap
import unittest

REPO = Path(__file__).resolve().parents[1]
LIBRARY = REPO / "scripts/lib/kiosk.sh"


class KioskTests(unittest.TestCase):
    def shell(self, command, **variables):
        environment = dict(os.environ, TYPE="kiosk", BOARD="avaota-a1", VERSION="noble")
        environment.update(variables)
        return subprocess.run(
            ["bash", "-ec", 'source "$1"; ' + command, "test", str(LIBRARY)],
            env=environment, text=True, capture_output=True,
        )

    def test_profile_rejects_unsupported_combinations(self):
        self.assertEqual(self.shell("validate_kiosk_profile").returncode, 0)
        for options in ({"BOARD": "avaota-c1"}, {"VERSION": "bookworm"},
                        {"TYPE": "cli", "APP_DEB": "app.deb"}):
            with self.subTest(options=options):
                self.assertNotEqual(self.shell("validate_kiosk_profile", **options).returncode, 0)


    @unittest.skipUnless(shutil.which("sha256sum"), "requires sha256sum")
    def test_rootfs_cache_changes_when_preservation_policy_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in ("os/noble/kiosk-packages.list", "scripts/mkrootfs.sh",
                             "boards/avaota-a1.conf", "scripts/trim-kiosk-rootfs.py"):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("original")
            first = self.shell('kiosk_rootfs_signature "$TEST_REPO"', TEST_REPO=directory)
            (root / "scripts/trim-kiosk-rootfs.py").write_text("new license preservation policy")
            second = self.shell('kiosk_rootfs_signature "$TEST_REPO"', TEST_REPO=directory)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotEqual(first.stdout, second.stdout)

    def test_missing_app_is_rejected(self):
        self.assertNotEqual(self.shell("validate_kiosk_app", APP_DEB="/missing.deb").returncode, 0)

    @unittest.skipUnless(sys.platform == "linux", "image building requires Linux")
    def test_entrypoint_rejects_missing_app_before_host_installation(self):
        result = subprocess.run(
            ["bash", "build_all.sh", "-b", "avaota-a1", "-v", "noble", "-t", "kiosk"],
            cwd=REPO, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("requires --app-deb", result.stderr)
        self.assertNotIn("sudo:", result.stderr)

    def test_cleanup_preserves_directory_when_unmount_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "keep"
            marker.touch()
            result = self.shell('mountpoint() { return 0; }; umount() { return 1; }; '
                                'cleanup_kiosk_rootfs', KIOSK_STAGE=temporary)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(marker.exists())

    def test_staging_root_is_traversable_by_the_kiosk_user(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = base / "fixture"
            for name in ("etc", "usr/sbin", "dev", "proc", "sys", "tmp"):
                (fixture / name).mkdir(parents=True, exist_ok=True)
            (fixture / "etc/avaota-release").write_text("DESKTOP=kiosk\n")
            # Match mkrootfs: root children are archived, without a '.' entry.
            with tarfile.open(base / "rootfs-noble-kiosk.tar.gz", "w:gz") as archive:
                for child in fixture.iterdir():
                    archive.add(child, arcname=child.name)
            package = base / "app.deb"
            package.write_bytes(b"test package")
            result = self.shell(textwrap.dedent('''
                mount() { :; }
                umount() { :; }
                chroot() {
                    if [[ $* == *"apt-get install"* ]]; then
                        grep -Fq '/*[-_.]' "$KIOSK_STAGE/etc/dpkg/dpkg.cfg.d/01-tono-kiosk-size"
                    fi
                }
                dpkg-deb() { printf '1.1.3\\n'; }
                sha256sum() { printf 'test-hash  test-file\\n'; }
                prepare_kiosk_rootfs "$TEST_WORKSPACE"
                printf '%s' "$KIOSK_STAGE"
            '''), TEST_WORKSPACE=str(base), APP_DEB=str(package))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(stat.S_IMODE(Path(result.stdout.splitlines()[-1]).stat().st_mode), 0o755)

    def test_kiosk_uses_board_xorg_configuration_without_gdm(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.shell('source "$TEST_REPO/boards/avaota-a1.conf"; setup_display',
                                ROOTFS=temporary, TEST_REPO=str(REPO))
            self.assertEqual(result.returncode, 0, result.stderr)
            root = Path(temporary)
            self.assertIn('MatchDriver "sun4i-drm"',
                          (root / "etc/X11/xorg.conf.d/20-avaota-display.conf").read_text())
            self.assertFalse((root / "etc/gdm3").exists())
            self.assertIn('Option "AccelMethod" "none"',
                          (root / "etc/X11/tono-kiosk-software/21-tono.conf").read_text())

    def test_wifi_remains_available_without_blocking_early_module_loading(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "etc/modules-load.d/avaota-wifi.conf"
            config.parent.mkdir(parents=True)
            config.write_text("aic8800_fdrv\n")
            command = 'source "$TEST_REPO/boards/avaota-a1.conf"; setup_wifi'
            result = self.shell(command, ROOTFS=temporary, TEST_REPO=str(REPO))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(config.exists())
            result = self.shell(command, TYPE="gnome", ROOTFS=temporary, TEST_REPO=str(REPO))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("aic8800_fdrv", config.read_text())

    def test_quiet_boot_keeps_root_console_and_other_boot_options(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "boot/extlinux/extlinux.conf"
            config.parent.mkdir(parents=True)
            original = ("label AvaotaOS\n  kernel /Image\n  initrd /uInitrd\n"
                        "  append root=LABEL=rootfs earlycon=uart8250,mmio32,0x02500000 "
                        "console=ttyAS0,115200 loglevel=8 initcall_debug=0 "
                        "rw fsck.repair=yes custom.option=keep\n")
            config.write_text(original)
            result = self.shell('configure_kiosk_boot "$TEST_ROOT"', TEST_ROOT=temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            updated = config.read_text()
            self.assertEqual(updated.splitlines()[:3], original.splitlines()[:3])
            self.assertIn("root=LABEL=rootfs", updated)
            self.assertIn("console=ttyAS0,115200", updated)
            self.assertIn("rw fsck.repair=yes custom.option=keep", updated)
            self.assertIn("quiet loglevel=3 systemd.show_status=auto", updated)
            self.assertNotIn("earlycon=", updated)
            self.assertNotIn("initcall_debug=", updated)
            result = self.shell('configure_kiosk_boot "$TEST_ROOT"', TEST_ROOT=temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(), updated)
            config.write_text(original)
            result = self.shell('configure_kiosk_boot "$TEST_ROOT"', TYPE="gnome", TEST_ROOT=temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(config.read_text(), original)
            config.write_text("label MissingArguments\n")
            result = self.shell('configure_kiosk_boot "$TEST_ROOT"', TEST_ROOT=temporary)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_text(), "label MissingArguments\n")

    @unittest.skipUnless(all(shutil.which(tool) for tool in ("dtc", "fdtget", "fdtput")),
                         "requires device-tree-compiler")
    def test_root_sd_detection_fix_preserves_other_hardware_and_desktops(self):
        with tempfile.TemporaryDirectory() as temporary:
            dtb = Path(temporary) / "boot/dtb/allwinner/sun55i-t527-avaota-a1.dtb"
            dtb.parent.mkdir(parents=True)
            subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(dtb)],
                           input='''/dts-v1/;
                / { soc@3000000 {
                    sdmmc@4020000 {
                        compatible = "allwinner,sunxi-mmc-v5p3x";
                        cd-gpios = <1 5 6 17>;
                        cd-set-debounce = <1>;
                        cd-used-24M;
                        max-frequency = <150000000>;
                        sd-uhs-sdr104;
                    };
                    sdmmc@4022000 { non-removable; bus-width = <8>; };
                }; };''', text=True, check=True, capture_output=True)
            original = dtb.read_bytes()
            command = 'source "$TEST_REPO/boards/avaota-a1.conf"; setup_boot'
            options = dict(ROOTFS=temporary, TEST_REPO=str(REPO))
            result = self.shell(command, TYPE="gnome", **options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(dtb.read_bytes(), original)
            result = self.shell(command, **options)
            self.assertEqual(result.returncode, 0, result.stderr)
            def get(node, *args):
                return subprocess.check_output(["fdtget", *args, str(dtb), node], text=True).split()
            sd = "/soc@3000000/sdmmc@4020000"
            properties = get(sd, "-p")
            self.assertIn("non-removable", properties)
            for prop in ("cd-gpios", "cd-set-debounce", "cd-used-24M"):
                self.assertNotIn(prop, properties)
            for node, prop, expected in ((sd, "max-frequency", "150000000"),
                                          ("/soc@3000000/sdmmc@4022000", "bus-width", "8")):
                value = subprocess.check_output(["fdtget", str(dtb), node, prop], text=True).strip()
                self.assertEqual(value, expected)
            self.assertIn("sd-uhs-sdr104", properties)
            first_pass = dtb.read_bytes()
            result = self.shell(command, **options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(dtb.read_bytes(), first_pass)

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb requires a Debian/Ubuntu host")
    def test_package_identity_architecture_and_path_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix="kiosk test ") as temporary:
            base = Path(temporary)
            control = base / "package/DEBIAN/control"
            control.parent.mkdir(parents=True)
            for name, architecture, version, accepted in (
                ("tono-canvas", "arm64", "1.1.5", True),
                ("tono-canvas", "arm64", "1.1.4", False),
                ("tono-canvas", "amd64", "1.1.5", False),
                ("other-app", "arm64", "1.1.5", False),
            ):
                control.write_text(
                    f"Package: {name}\nVersion: {version}\nArchitecture: {architecture}\n"
                    "Maintainer: Test <test@example.invalid>\nDescription: validation fixture\n"
                )
                package = base / "app package.deb"
                subprocess.run(["dpkg-deb", "--build", str(control.parent.parent), str(package)],
                               check=True, capture_output=True)
                result = self.shell('validate_kiosk_app; printf "%s" "$APP_DEB"', APP_DEB=str(package))
                self.assertEqual(result.returncode == 0, accepted, result.stderr)
                if accepted:
                    self.assertEqual(result.stdout, str(package))

    def test_custom_user_session_preserves_existing_input_method_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile = root / "home/artist/.config/fcitx5/profile"
            profile.parent.mkdir(parents=True)
            profile.write_text("existing settings\n")
            (root / "etc/systemd/system").mkdir(parents=True)
            result = self.shell(textwrap.dedent('''
                chroot() {
                    shift
                    case "$1" in
                        id) echo 1007 ;;
                        getent) echo 'artist:x:1007:1007::/home/artist:/bin/bash' ;;
                        systemctl|chown|groupadd|usermod) return 0 ;;
                        *) return 1 ;;
                    esac
                }
                install_kiosk_session "$TEST_ROOT" "$TEST_REPO"
            '''), SYS_USER="artist", TEST_ROOT=str(root), TEST_REPO=str(REPO))
            self.assertEqual(result.returncode, 0, result.stderr)
            configuration = (root / "etc/systemd/system/tono-kiosk.service.d/user.conf").read_text()
            self.assertIn("User=artist\n", configuration)
            self.assertIn("After=user@1007.service\n", configuration)
            self.assertIn("XDG_RUNTIME_DIR=/run/user/1007\n", configuration)
            self.assertEqual(profile.read_text(), "existing settings\n")
            rule = root / "etc/polkit-1/rules.d/49-tono-network.rules"
            self.assertEqual(rule.read_bytes(),
                             (REPO / "target/services/tono-kiosk/49-tono-network.rules").read_bytes())
            self.assertIn('subject.local && subject.active', rule.read_text())
            self.assertNotIn('settings.modify.system', rule.read_text())

    def test_stop_keeps_session_alive_until_app_has_saved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            session = root / "tono-kiosk"
            session.mkdir()
            saved = root / "saved"
            child = subprocess.Popen(["python3", "-u", "-c", textwrap.dedent('''
                import signal, sys, time
                from pathlib import Path
                def stop(signum, frame):
                    time.sleep(0.2)
                    Path(sys.argv[1]).write_text('saved')
                    raise SystemExit(0)
                signal.signal(signal.SIGTERM, stop)
                print('ready', flush=True)
                while True:
                    time.sleep(1)
            '''), str(saved)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                (session / "app.pid").write_text(str(child.pid) + "\n")
                # Reap the child while the stop script polls kill -0.
                import threading
                reaper = threading.Thread(target=child.wait)
                reaper.start()
                result = subprocess.run(
                    ["bash", str(REPO / "target/services/tono-kiosk/tono-kiosk-stop")],
                    env=dict(os.environ, XDG_RUNTIME_DIR=str(root)),
                    timeout=5, capture_output=True, text=True,
                )
                reaper.join(timeout=2)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(saved.read_text(), "saved")
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
                child.stdout.close()


if __name__ == "__main__":
    unittest.main()
