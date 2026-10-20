"""Host-side regression checks; no mounts, package installation or root required."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import stat
import tarfile
import tempfile
import textwrap
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
LIBRARY = REPO / "scripts/lib/kiosk.sh"
SERVICES = REPO / "target/services/tono-kiosk"
# Headset microphone capture: repository file -> path inside the image.
HEADSET_MIC_FILES = {
    "60-tono-headset-mic.lua": "etc/wireplumber/main.lua.d/60-tono-headset-mic.lua",
    "91-tono-headset-mic.rules": "etc/udev/rules.d/91-tono-headset-mic.rules",
    "tono-headset-mic.service": "etc/systemd/system/tono-headset-mic.service",
    "tono-headset-mic": "usr/local/libexec/tono-headset-mic",
}
# Like xinit: Xorg stops after the client exits, but a signal hangs up the
# client's process group and stops Xorg at once.
FAKE_XINIT = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, signal, subprocess, sys
    def record(event):
        with open(os.environ["EVENTS"], "a") as events:
            events.write(event + "\\n")
    client = None
    def stop(number, frame):
        record("xinit signal %d" % number)
        if client:
            os.killpg(client.pid, signal.SIGHUP)
        record("xorg stopped")
        os._exit(1)
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, stop)
    client = subprocess.Popen([sys.argv[1]], preexec_fn=os.setpgrp)
    record("xinit " + os.environ["TONO_RENDERER"])
    client.wait()
    record("xorg stopped")
''')
# Saves for a moment on SIGTERM, as Tono Canvas does, unless told to hang.
FAKE_APP = textwrap.dedent('''\
    #!/usr/bin/env python3
    import os, signal, sys, time
    def record(event):
        with open(os.environ["EVENTS"], "a") as events:
            events.write(event + "\\n")
    def save(number, frame):
        time.sleep(0.3)
        record("app saved")
        sys.exit(0)
    signal.signal(signal.SIGTERM, save if os.environ["TEST_APP"] == "save" else signal.SIG_IGN)
    record("app running")
    while os.path.exists(os.environ["ALIVE"]):
        time.sleep(0.1)
''')
# Session helpers run until killed, or until the test removes its marker.
FAKE_DAEMON = '#!/bin/sh\nwhile [ -e "$ALIVE" ]; do sleep 0.1; done\n'


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
            scope = root / "etc/systemd/system/session-.scope.d/10-tono-kiosk.conf"
            self.assertEqual(scope.read_bytes(), (SERVICES / "session-scope.conf").read_bytes())
            for name, relative in HEADSET_MIC_FILES.items():
                self.assertEqual((root / relative).read_bytes(), (SERVICES / name).read_bytes(), name)
            self.assertTrue(os.access(root / "usr/local/libexec/tono-headset-mic", os.X_OK))

    def run_headset_mic(self, amixer_body=""):
        """Run tono-headset-mic against a recording amixer stand-in."""
        with tempfile.TemporaryDirectory() as temporary:
            fixtures = Path(temporary)
            calls = fixtures / "calls"
            amixer = fixtures / "amixer"
            amixer.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n' + amixer_body)
            amixer.chmod(0o755)
            result = subprocess.run(
                ["bash", str(SERVICES / "tono-headset-mic")],
                env=dict(os.environ, PATH=f"{fixtures}:{os.environ['PATH']}", CALLS=str(calls)),
                capture_output=True, text=True,
            )
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_headset_microphone_is_the_only_enabled_codec_input(self):
        result, calls = self.run_headset_mic()
        self.assertEqual(result.returncode, 0, result.stderr)
        # Full control names: the codec's simple mixer names are just MIC1..3.
        self.assertEqual(calls, ["-c audiocodec cset name='MIC1 Switch' off",
                                 "-c audiocodec cset name='MIC2 Switch' off",
                                 "-c audiocodec cset name='MIC3 Switch' on"])
        for name in ("HPOUT", "LINEOUT", "SPK", "Volume", "Gain"):
            self.assertFalse(any(name in call for call in calls), name)
        # A control the driver no longer offers must fail loudly, not half-apply.
        result, calls = self.run_headset_mic('case "$*" in *MIC2*) exit 1 ;; esac\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)

    def test_headset_microphone_routing_follows_the_codec_and_precedes_the_kiosk(self):
        unit = (SERVICES / "tono-headset-mic.service").read_text().splitlines()
        for setting in ("After=alsa-restore.service", "Before=tono-kiosk.service", "Type=oneshot",
                        "ExecStart=/usr/local/libexec/tono-headset-mic"):
            self.assertIn(setting, unit)
        # The card's udev event starts it, so another sound card appearing first
        # cannot consume the trigger; a static unit has no [Install] section.
        self.assertNotIn("[Install]", unit)
        rules = [line for line in (SERVICES / "91-tono-headset-mic.rules").read_text().splitlines()
                 if line and not line.startswith("#")]
        self.assertEqual(len(rules), 1)
        for token in ('ACTION=="add"', 'SUBSYSTEM=="sound"', 'KERNEL=="controlC*"',
                      'ATTRS{id}=="audiocodec"', 'TAG+="systemd"',
                      'ENV{SYSTEMD_WANTS}+="tono-headset-mic.service"'):
            self.assertIn(token, rules[0])
        # alsa-utils restores saved state from 90-alsa-restore.rules; sort after it.
        self.assertGreater("91-tono-headset-mic.rules", "90-alsa-restore.rules")
        if shutil.which("udevadm") and subprocess.run(
                ["udevadm", "verify", "--help"], capture_output=True).returncode == 0:
            subprocess.run(["udevadm", "verify", str(SERVICES / "91-tono-headset-mic.rules")],
                           check=True, capture_output=True)

    def test_headset_microphone_node_is_mono_on_mic3(self):
        rule = (SERVICES / "60-tono-headset-mic.lua").read_text()
        self.assertIn("table.insert(alsa_monitor.rules", rule)
        self.assertIn('"node.name", "equals", '
                      '"alsa_input.platform-soc_3000000_codec_mach.stereo-fallback"', rule)
        self.assertIn('["audio.channels"] = 1', rule)
        self.assertIn('["audio.position"] = "MONO"', rule)
        self.assertIn('["node.description"] = "Avaota A1 Headset Microphone"', rule)
        # WirePlumber 0.4 loads main.lua.d in name order: after alsa_monitor is
        # defined (50-alsa-config.lua) and before the monitors start (90-enable-all.lua).
        self.assertTrue("50-alsa-config.lua" < "60-tono-headset-mic.lua" < "90-enable-all.lua")
        luac = next((tool for tool in ("luac", "luac5.4", "luac5.3", "luac5.1")
                     if shutil.which(tool)), None)
        if luac:
            subprocess.run([luac, "-p", str(SERVICES / "60-tono-headset-mic.lua")],
                           check=True, capture_output=True)

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

    def stop_kiosk(self, app="save", attempts=180, idle=False):
        """Run tono-kiosk-start with fixture programs, then stop it as systemd does."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = root / "bin"
            fixtures.mkdir()
            events, alive, log = root / "events", root / "alive", root / "stderr"
            alive.touch()
            for name, body in {
                "timeout": '#!/bin/sh\nshift\nexec "$@"\n', "xauth": "#!/bin/sh\n",
                "mcookie": "#!/bin/sh\necho cookie\n", "tono-gpu-probe": "#!/bin/sh\n",
                "systemctl": "#!/bin/sh\n", "xset": "#!/bin/sh\n",
                "openbox": FAKE_DAEMON, "fcitx5": FAKE_DAEMON, "idle-session": FAKE_DAEMON,
                "xinit": FAKE_XINIT, "tono-canvas": FAKE_APP,
                "tono-kiosk-stop": (SERVICES / "tono-kiosk-stop").read_text().replace(
                    "attempt < 180", f"attempt < {attempts}"),
            }.items():
                (fixtures / name).write_text(body)
                (fixtures / name).chmod(0o755)
            # An idle session stands in for the time before the app has started.
            session = fixtures / "idle-session" if idle else SERVICES / "tono-kiosk-session"
            script = (SERVICES / "tono-kiosk-start").read_text()
            for program, replacement in (
                    ("/usr/bin/xinit", fixtures / "xinit"),
                    ("/usr/local/libexec/tono-kiosk-session", session),
                    ("/usr/local/libexec/tono-kiosk-stop", fixtures / "tono-kiosk-stop"),
                    ("/usr/local/libexec/tono-gpu-probe", fixtures / "tono-gpu-probe")):
                self.assertTrue(program in script, program)
                script = script.replace(program, str(replacement))
            (fixtures / "tono-kiosk-start").write_text(script)
            environment = dict(os.environ, PATH=f"{fixtures}:{os.environ['PATH']}",
                               XDG_RUNTIME_DIR=temporary, TONO_RENDERER="auto",
                               EVENTS=str(events), ALIVE=str(alive), TEST_APP=app)
            started = "xinit hardware" if idle else "app running"
            pid_file = root / "tono-kiosk/app.pid"
            # A file, not a pipe: fixtures left over by a failure must not block the test.
            with log.open("w") as stderr:
                process = subprocess.Popen(["bash", str(fixtures / "tono-kiosk-start")],
                                           env=environment, stderr=stderr)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and not (
                        events.exists() and started in events.read_text().splitlines() and
                        (started != "app running" or pid_file.exists())):
                    time.sleep(0.05)
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=20)
            finally:
                alive.unlink()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            result = root / "tono-kiosk/session.exit"
            return (process.returncode, events.read_text().splitlines(), log.read_text(),
                    result.read_text() if result.exists() else None)

    def test_sigterm_lets_the_app_save_before_xinit_stops_xorg(self):
        status, events, stderr, session = self.stop_kiosk()
        self.assertEqual(status, 0, stderr)
        # No xinit signal and no software retry: the app saved, then Xorg stopped.
        self.assertEqual(events, ["xinit hardware", "app running", "app saved", "xorg stopped"])
        self.assertEqual(session, "0\n")
        self.assertIn("Asked Tono Canvas", stderr)
        self.assertIn("Xorg has exited", stderr)

    def test_sigterm_before_the_app_starts_ends_the_session(self):
        status, events, stderr, _ = self.stop_kiosk(idle=True)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(events, ["xinit hardware", "xinit signal 15", "xorg stopped"])

    def test_app_that_does_not_save_in_time_still_stops_xorg(self):
        status, events, stderr, _ = self.stop_kiosk(app="hang", attempts=2)
        self.assertEqual(status, 1)
        self.assertIn("did not finish saving", stderr)
        self.assertEqual(events, ["xinit hardware", "app running", "xinit signal 15", "xorg stopped"])

    def test_stop_never_hangs_up_tty1_and_shutdown_stops_the_kiosk_first(self):
        unit = (SERVICES / "tono-kiosk.service").read_text().splitlines()
        self.assertEqual([line for line in unit if line.startswith("ExecStop")], [])
        for setting in ("TTYPath=/dev/tty1", "StandardInput=tty", "TTYVHangup=yes",
                        "PAMName=login", "KillMode=mixed", "Restart=always"):
            self.assertIn(setting, unit)
        self.assertIn("Before=tono-kiosk.service", (SERVICES / "session-scope.conf").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
