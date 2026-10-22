#!/usr/bin/env python3
"""Buildroot appliance integration and guarded SD expansion regression checks."""
import copy
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import sys
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
BOARD = REPO / "buildroot/board/avaota-a1"
OVERLAY = BOARD / "overlay"
GROW_SCRIPT = OVERLAY / "usr/local/libexec/tono-grow-rootfs"
loader = importlib.machinery.SourceFileLoader("tono_grow_rootfs", str(GROW_SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
grow_module = importlib.util.module_from_spec(spec)
loader.exec_module(grow_module)


class SDCommands:
    """Emulate the block/partition tools without opening a real block device."""

    def __init__(self):
        self.disk = "/dev/mmcblk0"
        self.source = self.disk + "p2"
        self.fs_type = "ext4"
        self.label = "rootfs"
        self.sectors = 4 * 1024 * 1024
        self.kernel_size = 2 * 1024 * 1024
        self.fs_blocks = self.kernel_size * 512 // 4096
        self.partuuid = "1234abcd-02"
        self.table = {
            "label": "dos", "id": "0x1234abcd", "device": self.disk,
            "unit": "sectors", "sectorsize": 512,
            "partitions": [
                {"node": self.disk + "p1", "start": 32768, "size": 229376, "type": "c"},
                {"node": self.source, "start": 262144, "size": self.kernel_size, "type": "83"},
            ],
        }
        self.calls = []
        self.refuse_kernel_update = False
        self.refuse_resize = False
        self.partial_resize = False
        self.corrupt_identity = False

    def __call__(self, *args, input=None):
        self.calls.append((args, input))
        if args[0] == "findmnt":
            return self.fs_type if "FSTYPE" in args else self.source
        if args[0] == "blkid":
            return self.label if "LABEL" in args else self.partuuid
        if args[:2] == ("blockdev", "--getss"):
            return "512"
        if args[:2] == ("blockdev", "--getsz"):
            return str(self.sectors if args[-1] == self.disk else self.kernel_size)
        if args[:2] == ("sfdisk", "--json"):
            return json.dumps({"partitiontable": self.table})
        if args[:2] == ("sfdisk", "--dump"):
            return "label: dos\nlabel-id: 0x1234abcd\n" + json.dumps(self.table)
        if args[0] == "sfdisk":
            self.table["partitions"][1]["size"] = int(input.removeprefix("size=").strip())
            if self.corrupt_identity:
                self.table["id"] = "0x87654321"
            return ""
        if args[0] == "partx":
            if not self.refuse_kernel_update:
                self.kernel_size = self.table["partitions"][1]["size"]
            return ""
        if args[0] == "resize2fs":
            if self.refuse_resize:
                raise subprocess.CalledProcessError(1, args)
            if not self.partial_resize:
                self.fs_blocks = self.kernel_size * 512 // 4096
            return ""
        if args[0] == "dumpe2fs":
            return f"Block count:              {self.fs_blocks}\nBlock size:               4096\n"
        raise AssertionError(f"Unexpected command: {args!r}")

    def partition_writes(self):
        return [(args, data) for args, data in self.calls
                if args[0] == "sfdisk" and args[1] not in ("--json", "--dump")]


class ExpansionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="tono-grow-test-")
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "state"
        self.sd = SDCommands()
        self.patch = mock.patch.object(grow_module, "command", side_effect=lambda *args, **kwargs: self.sd(*args, **kwargs))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.uid = mock.patch.object(grow_module.os, "geteuid", return_value=0)
        self.uid.start()
        self.addCleanup(self.uid.stop)
        self.sync = mock.patch.object(grow_module.os, "sync")
        self.sync_mock = self.sync.start()
        self.addCleanup(self.sync.stop)

    def test_grow_updates_only_size_and_marks_after_ext4_verification(self):
        before = copy.deepcopy(self.sd.table)
        grow_module.grow(self.state)
        expected = copy.deepcopy(before)
        expected["partitions"][1]["size"] = self.sd.sectors - grow_module.ROOT_START
        self.assertEqual(self.sd.table, expected)
        writes = self.sd.partition_writes()
        self.assertEqual(len(writes), 1)
        args, data = writes[0]
        self.assertIn("--no-reread", args)
        self.assertIn("--no-tell-kernel", args)
        self.assertIn("--wipe-partitions", args)
        self.assertEqual(args[args.index("--wipe-partitions") + 1], "never")
        self.assertEqual(args[args.index("--wipe") + 1], "never")
        self.assertEqual(args[-3:], ("-N", "2", "/dev/mmcblk0"))
        self.assertEqual(data, "size=3932160\n")
        marker = (self.state / "complete").read_text()
        self.assertIn("partuuid=1234abcd-02\n", marker)
        self.assertIn("sectors=3932160\n", marker)
        self.assertEqual((self.state / "partition-table.before").stat().st_mode & 0o777, 0o600)
        self.sync_mock.assert_called_once()

    def test_completed_partition_still_recovers_an_unexpanded_filesystem(self):
        self.sd.table["partitions"][1]["size"] = self.sd.sectors - grow_module.ROOT_START
        grow_module.grow(self.state)
        self.assertEqual(self.sd.partition_writes(), [])
        self.assertTrue((self.state / "complete").exists())
        self.assertEqual(self.sd.fs_blocks * 4096, self.sd.kernel_size * 512)

    def test_wrong_device_filesystem_label_layout_or_identity_never_writes(self):
        mutations = {
            "non-root SD device": lambda sd: setattr(sd, "source", "/dev/sda2"),
            "wrong partition": lambda sd: setattr(sd, "source", "/dev/mmcblk0p1"),
            "non-ext4": lambda sd: setattr(sd, "fs_type", "vfat"),
            "wrong label": lambda sd: setattr(sd, "label", "other"),
            "unexpected start": lambda sd: sd.table["partitions"][1].update(start=131072),
            "overlapping boot": lambda sd: sd.table["partitions"][0].update(size=262144),
            "partition after root": lambda sd: sd.table["partitions"].append(
                {"node": "/dev/mmcblk0p3", "start": 4000000, "size": 1, "type": "83"}),
            "different PARTUUID": lambda sd: setattr(sd, "partuuid", "different-02"),
            "not DOS": lambda sd: sd.table.update(label="gpt"),
            "zero disk identity": lambda sd: sd.table.update(id="0x00000000"),
            "too large for DOS": lambda sd: setattr(sd, "sectors", 2 ** 32 + 1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.sd = SDCommands()
                mutate(self.sd)
                with self.assertRaises(RuntimeError):
                    grow_module.grow(self.state)
                self.assertEqual(self.sd.partition_writes(), [])
                self.assertFalse((self.state / "complete").exists())

    def test_failed_kernel_update_retries_without_recreating_partition(self):
        self.sd.refuse_kernel_update = True
        with self.assertRaisesRegex(RuntimeError, "Kernel"):
            grow_module.grow(self.state)
        self.assertFalse((self.state / "complete").exists())
        self.assertFalse(any(args[0] == "resize2fs" for args, _ in self.sd.calls))
        backup = (self.state / "partition-table.before").read_bytes()
        self.sd.refuse_kernel_update = False
        grow_module.grow(self.state)
        self.assertEqual(len(self.sd.partition_writes()), 1)
        self.assertEqual((self.state / "partition-table.before").read_bytes(), backup)
        self.assertTrue((self.state / "complete").exists())

    def test_failed_resize_retries_next_boot_and_keeps_original_backup(self):
        self.sd.refuse_resize = True
        with self.assertRaises(subprocess.CalledProcessError):
            grow_module.grow(self.state)
        self.assertFalse((self.state / "complete").exists())
        self.sd.refuse_resize = False
        grow_module.grow(self.state)
        self.assertEqual(len(self.sd.partition_writes()), 1)
        self.assertTrue((self.state / "complete").exists())

    def test_unexpected_identity_change_fails_before_resize(self):
        self.sd.corrupt_identity = True
        with self.assertRaisesRegex(RuntimeError, "identity"):
            grow_module.grow(self.state)
        self.assertFalse((self.state / "complete").exists())
        self.assertFalse(any(args[0] == "resize2fs" for args, _ in self.sd.calls))

    def test_resize_success_without_matching_ext4_size_does_not_mark_complete(self):
        self.sd.partial_resize = True
        with self.assertRaisesRegex(RuntimeError, "ext4"):
            grow_module.grow(self.state)
        self.assertFalse((self.state / "complete").exists())

    def test_non_root_fails_before_querying_or_writing_devices(self):
        self.uid.stop()
        with mock.patch.object(grow_module.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(RuntimeError, "Root privileges"):
                grow_module.grow(self.state)
        self.assertEqual(self.sd.calls, [])


class ApplianceServiceTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("TONO_BUILT_ROOTFS_TAR"),
                         "set TONO_BUILT_ROOTFS_TAR to validate a finalized Buildroot artifact")
    def test_finalized_tar_enables_native_dns_clock_and_ssh(self):
        # Buildroot applies systemd presets in ROOTFS_PRE_CMD_HOOKS under
        # fakeroot. output/target alone intentionally cannot prove boot defaults.
        import tarfile
        with tarfile.open(os.environ["TONO_BUILT_ROOTFS_TAR"]) as archive:
            members = {member.name.removeprefix("./"): member for member in archive}
            expected = {
                "etc/systemd/system/sysinit.target.wants/systemd-resolved.service":
                    "/usr/lib/systemd/system/systemd-resolved.service",
                "etc/systemd/system/sysinit.target.wants/systemd-timesyncd.service":
                    "/usr/lib/systemd/system/systemd-timesyncd.service",
                "etc/systemd/system/multi-user.target.wants/sshd.service":
                    "/usr/lib/systemd/system/sshd.service",
                "etc/systemd/system/multi-user.target.wants/NetworkManager.service":
                    "/usr/lib/systemd/system/NetworkManager.service",
                "etc/systemd/system/dbus-org.freedesktop.resolve1.service":
                    "/usr/lib/systemd/system/systemd-resolved.service",
                "etc/systemd/system/dbus-org.freedesktop.timesync1.service":
                    "/usr/lib/systemd/system/systemd-timesyncd.service",
                "etc/systemd/system/sockets.target.wants/systemd-resolved-varlink.socket":
                    "/usr/lib/systemd/system/systemd-resolved-varlink.socket",
                "etc/systemd/system/sockets.target.wants/systemd-resolved-monitor.socket":
                    "/usr/lib/systemd/system/systemd-resolved-monitor.socket",
            }
            for name, target in expected.items():
                with self.subTest(unit=name):
                    entry = members[name]
                    self.assertTrue(entry.issym())
                    self.assertEqual(entry.linkname, target)
                    self.assertGreater(members[target.lstrip("/")].size, 0)
            for name, target in (
                ("etc/resolv.conf", "/run/systemd/resolve/stub-resolv.conf"),
                ("opt/tono-runtime/etc/resolv.conf", "/run/NetworkManager/resolv.conf"),
            ):
                self.assertTrue(members[name].issym())
                self.assertEqual(members[name].linkname, target)
            for name in ("dbus.socket", "pipewire.socket", "pipewire.service",
                         "pipewire-pulse.socket", "pipewire-pulse.service", "wireplumber.service"):
                self.assertGreater(members["usr/lib/systemd/user/" + name].size, 0)

    def test_runtime_arguments_report_missing_values_cleanly(self):
        for flag in ("--base-rootfs", "--app-deb", "--output"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    ["bash", str(REPO / "scripts/buildroot-prepare-runtime.sh"), flag],
                    text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage:", result.stdout + result.stderr)
                self.assertNotIn("unbound variable", result.stderr)

    def test_cached_runtime_and_bsp_are_excluded_from_native_mutation(self):
        config = (REPO / "buildroot/configs/avaota_a1_defconfig").read_text()
        self.assertIn('BR2_STRIP_EXCLUDE_DIRS="opt/tono-runtime usr/lib/modules usr/lib/firmware"', config)
        entry = (REPO / "build_buildroot.sh").read_text()
        self.assertLess(entry.index('bash "$repo/buildroot/apply-patches.sh"'),
                        entry.index('make -C "$source_root"'))
        with tempfile.TemporaryDirectory(prefix="tono-patch-retry-") as directory:
            script = Path(directory) / "support/scripts/fix-rpath"
            script.parent.mkdir(parents=True)
            script.write_text(
                "# ELF files should not be in these sub-directories\n"
                'HOST_EXCLUDEPATHS="/share/terminfo"\n'
                'STAGING_EXCLUDEPATHS="/usr/include /usr/share/terminfo"\n'
                'TARGET_EXCLUDEPATHS="/lib/firmware"\n'
                "\ndeclare -a sanitize_extra_args\nrootdir=\n"
            )
            apply = ["bash", str(REPO / "buildroot/apply-patches.sh"), directory]
            subprocess.run(apply, check=True, capture_output=True)
            first = script.read_bytes()
            subprocess.run(apply, check=True, capture_output=True)
            self.assertEqual(script.read_bytes(), first)
            self.assertIn(b"/opt/tono-runtime /usr/lib/modules", first)
            script.write_bytes(first.replace(b"TARGET_EXCLUDEPATHS=", b"UNKNOWN_EXCLUDEPATHS="))
            unchanged = script.read_bytes()
            rejected = subprocess.run(apply, capture_output=True)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(script.read_bytes(), unchanged)

    def test_missing_payload_inputs_fail_before_staging_services(self):
        with tempfile.TemporaryDirectory(prefix="tono-core-missing-") as directory:
            env = dict(os.environ)
            env.pop("TONO_RUNTIME_ARCHIVE", None)
            env.pop("TONO_BSP_DIR", None)
            result = subprocess.run(
                ["bash", str(BOARD / "post-build.sh"), directory], env=env,
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("requires TONO_RUNTIME_ARCHIVE and TONO_BSP_DIR", result.stderr)
            self.assertFalse((Path(directory) / "etc/systemd/system/tono-kiosk.service").exists())

    def test_parallel_polkit_configure_waits_for_native_libsystemd(self):
        with tempfile.TemporaryDirectory(prefix="tono-polkit-order-") as directory:
            root = Path(directory)
            makefile = root / "Makefile"
            makefile.write_text(
                "BR2_INIT_SYSTEMD=y\nBR2_PACKAGE_POLKIT=y\n"
                f"BR2_EXTERNAL_TONO_A527_PATH={REPO / 'buildroot'}\n"
                "POLKIT_TARGET_CONFIGURE=polkit.stamp\n"
                "POLKIT_CONF_OPTS=-Dman=false -Dsession_tracking=ConsoleKit\n"
                ".PHONY: all systemd\nall: polkit.stamp\n"
                "polkit.stamp:\n"
                "\t@test -f systemd.ready\n"
                "\t@printf '%s\\n' '$(POLKIT_CONF_OPTS)' > selected-options\n"
                "\t@touch $@\n"
                "systemd:\n\t@touch systemd.ready\n"
                f"include {REPO / 'buildroot/external.mk'}\n"
            )
            subprocess.run(["make", "-s", "-j4", "-C", directory, "all"], check=True)
            options = (root / "selected-options").read_text()
            self.assertIn("-Dsession_tracking=logind", options)
            self.assertNotIn("-Dsession_tracking=ConsoleKit", options)

    def test_cpu_auto_uses_supported_dynamic_governor_and_preserves_limits(self):
        with tempfile.TemporaryDirectory(prefix="tono-cpu-policy-") as directory:
            root = Path(directory)
            cases = [
                ("performance", "performance schedutil ondemand", "schedutil"),
                ("performance", "performance ondemand", "ondemand"),
                ("conservative", "performance schedutil conservative", "conservative"),
                ("performance", "performance", "performance"),
            ]
            for index, (current, available, _) in enumerate(cases):
                policy = root / f"policy{index}"
                policy.mkdir()
                (policy / "scaling_governor").write_text(current + "\n")
                (policy / "scaling_available_governors").write_text(available + "\n")
                (policy / "scaling_min_freq").write_text("408000\n")
                (policy / "scaling_max_freq").write_text("1800000\n")
            env = {**os.environ, "TONO_CPUFREQ_ROOT": str(root), "TONO_CPU_GOVERNOR": "auto"}
            subprocess.run(["bash", str(OVERLAY / "usr/local/libexec/tono-kiosk-power")],
                           check=True, env=env, capture_output=True)
            for index, (_, _, expected) in enumerate(cases):
                policy = root / f"policy{index}"
                self.assertEqual((policy / "scaling_governor").read_text().strip(), expected)
                self.assertEqual((policy / "scaling_min_freq").read_text(), "408000\n")
                self.assertEqual((policy / "scaling_max_freq").read_text(), "1800000\n")

    def test_postbuild_preserves_save_before_xorg_stop_and_native_pam(self):
        with tempfile.TemporaryDirectory(prefix="tono-core-test-") as directory:
            root = Path(directory)
            shutil.copytree(OVERLAY, root, dirs_exist_ok=True)
            # The payload assembler is tested separately; intercept only its
            # Python invocation so this test exercises the real service staging.
            tools = root / "test-tools"
            tools.mkdir()
            wrapper = tools / "python3"
            wrapper.write_text("#!" + sys.executable + "\n"
                "import pathlib,sys\n"
                "assert pathlib.Path(sys.argv[1]).name == 'buildroot-install-payload.py'\n"
                "assert sys.argv[2] == '--target'\n"
                "pathlib.Path(sys.argv[3], 'payload-called').touch()\n")
            wrapper.chmod(0o755)
            env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
                   "TONO_RUNTIME_ARCHIVE": "/test/runtime.tar.gz", "TONO_BSP_DIR": "/test/bsp"}
            subprocess.run(["bash", str(BOARD / "post-build.sh"), str(root)], check=True, env=env)
            self.assertTrue((root / "payload-called").exists())
            service = (root / "etc/systemd/system/tono-kiosk.service").read_text()
            self.assertIn("\nUser=avaota\n", service)
            self.assertIn("\nPAMName=tono-kiosk\n", service)
            self.assertNotIn("\nExecStop=", service)
            self.assertIn("\nKillMode=mixed\n", service)
            self.assertIn("\nTimeoutStopSec=100\n", service)
            scope = (root / "etc/systemd/system/session-.scope.d/10-tono-kiosk.conf").read_text()
            self.assertIn("\nBefore=tono-kiosk.service\n", scope)
            self.assertEqual((root / "etc/machine-id").read_bytes(), b"")
            self.assertEqual((root / "etc/systemd/system/getty@tty1.service").readlink(), Path("/dev/null"))

    def test_runtime_preserves_userns_sandbox_and_native_identity_bus(self):
        unit = (OVERLAY / "etc/systemd/system/tono-kiosk.service.d/buildroot-runtime.conf").read_text()
        for option in (
            "RootDirectory=/opt/tono-runtime", "MountAPIVFS=yes", "PrivateMounts=yes",
            "BindPaths=/home /run /tmp /dev", "/etc/passwd /etc/group /etc/machine-id",
            "ExecStartPre=/usr/local/libexec/tono-sandbox-preflight",
        ):
            self.assertIn(option, unit)
        self.assertNotIn("\nPrivateUsers=", unit)
        self.assertNotIn("\nRestrictNamespaces=", unit)
        self.assertNotIn("QTWEBENGINE_DISABLE_SANDBOX", unit)
        self.assertNotIn("--no-sandbox", unit)
        pam = (OVERLAY / "etc/pam.d/tono-kiosk").read_text()
        self.assertIn("session required pam_systemd.so", pam)

    def test_network_uses_logind_authorization_and_does_not_wait_online(self):
        config = (REPO / "buildroot/configs/avaota_a1_defconfig").read_text()
        self.assertIn("# BR2_PACKAGE_SYSTEMD_POLKIT is not set", config)
        self.assertIn("# BR2_PACKAGE_SYSTEMD_NETWORKD is not set", config)
        self.assertIn("BR2_PACKAGE_POLKIT=y", config)
        self.assertIn("BR2_PACKAGE_WPA_SUPPLICANT_DBUS=y", config)
        external = (REPO / "buildroot/external.mk").read_text()
        self.assertIn("-Dsession_tracking=logind", external)
        self.assertIn("POLKIT_DEPENDENCIES += systemd", external)
        nm = (OVERLAY / "etc/NetworkManager/conf.d/10-tono.conf").read_text()
        self.assertIn("dhcp=internal", nm)
        self.assertIn("dns=systemd-resolved", nm)
        unit = (OVERLAY / "etc/systemd/system/tono-kiosk.service.d/buildroot-runtime.conf").read_text()
        self.assertNotIn("network-online.target", unit)

    def test_native_core_keeps_audio_camera_and_sd_growth_tools(self):
        config = (REPO / "buildroot/configs/avaota_a1_defconfig").read_text()
        for option in ("PIPEWIRE", "PIPEWIRE_V4L2", "WIREPLUMBER", "ALSA_UTILS_APLAY",
                       "UTIL_LINUX_BINARIES", "UTIL_LINUX_PARTX", "E2FSPROGS_RESIZE2FS"):
            self.assertIn(f"BR2_PACKAGE_{option}=y", config)
        audio = (OVERLAY / "etc/wireplumber/wireplumber.conf.d/60-tono-headset-mic.conf").read_text()
        self.assertIn('node.name = "alsa_input.platform-soc_3000000_codec_mach.stereo-fallback"', audio)
        self.assertIn("audio.channels = 1", audio)
        self.assertIn("audio.position = [ MONO ]", audio)
        grow_unit = (OVERLAY / "etc/systemd/system/tono-grow-rootfs.service").read_text()
        self.assertIn("Before=tono-kiosk.service", grow_unit)
        self.assertIn("ConditionPathExists=!/var/lib/tono-grow-rootfs/complete", grow_unit)


if __name__ == "__main__":
    unittest.main()
