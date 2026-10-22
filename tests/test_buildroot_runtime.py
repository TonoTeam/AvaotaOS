"""Check runtime dependency closure, guest symlinks and private-data boundaries."""
import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("buildroot_runtime", REPO / "scripts/buildroot-runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
preflight_spec = importlib.util.spec_from_file_location("sandbox_preflight", REPO / "buildroot/board/avaota-a1/runtime-check.py")
preflight = importlib.util.module_from_spec(preflight_spec)
preflight_spec.loader.exec_module(preflight)


def package(name, version="1", architecture="arm64", **fields):
    result = "Package: " + name + "\nVersion: " + version + "\nArchitecture: " + architecture + "\nStatus: install ok installed\n"
    return result + "".join(key.replace("_", "-") + ": " + value + "\n" for key, value in fields.items()) + "\n"


class DependencyTests(unittest.TestCase):
    def test_debian_versions_epochs_revisions_tilde_and_numbers(self):
        ordered = ["1.0~~", "1.0~rc1", "1.0", "1.0-1", "1.0-2", "1.0-10", "1.0+git1", "1.1", "1:0.1", "2:0.1"]
        for left, right in zip(ordered, ordered[1:]):
            self.assertLess(runtime.version_compare(left, right), 0, (left, right))
            self.assertGreater(runtime.version_compare(right, left), 0, (left, right))
        self.assertEqual(runtime.version_compare("1.02", "1.2-0"), 0)

    def test_predepends_alternatives_virtual_versions_arch_and_cycles(self):
        packages = runtime.PackageSet(
            package("app", Depends="missing | library:any (>= 1:2.0), virtual-api (>= 3)", Pre_Depends="shell")
            + package("library", "1:2.1", Depends="app")
            + package("provider", "9", Provides="virtual-api (= 3)")
            + package("shell", architecture="all")
            + package("unrelated")
        )
        self.assertEqual(packages.closure(["app"]), ["app:arm64", "library:arm64", "provider:arm64", "shell:all"])

    def test_unversioned_provides_cannot_satisfy_versioned_requirement(self):
        packages = runtime.PackageSet(package("app", Depends="virtual-api (>= 1)") + package("provider", Provides="virtual-api"))
        with self.assertRaisesRegex(ValueError, "no installed"):
            packages.closure(["app"])

    def test_inadequate_installed_version_and_foreign_arch_fail(self):
        packages = runtime.PackageSet(package("app", Depends="library (>= 2)") + package("library", "1"))
        with self.assertRaisesRegex(ValueError, "no installed"):
            packages.closure(["app"])
        packages = runtime.PackageSet(package("app", architecture="amd64"))
        with self.assertRaisesRegex(ValueError, "no installed"):
            packages.closure(["app"])

    def test_uninstalled_and_build_time_dependency_syntax_rejected(self):
        packages = runtime.PackageSet(package("app").replace("install ok installed", "deinstall ok config-files"))
        with self.assertRaisesRegex(ValueError, "no installed"):
            packages.closure(["app"])
        with self.assertRaisesRegex(ValueError, "unsupported"):
            runtime.dependency("library [amd64]")


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.source, self.destination = self.base / "source", self.base / "runtime"
        self.source.mkdir()
        self.app_deb = self.base / "tono-canvas_1_arm64.deb"
        self.app_deb.write_bytes(b"fixture-debian-package")
        self.file("/etc/os-release", 'ID=ubuntu\nVERSION_ID="24.04"\n')
        self.file("/var/lib/dpkg/status", package("tono-canvas", Depends="libc6", Pre_Depends="dash") + package("libc6") + package("dash"))
        self.file("/usr/bin/tono-canvas", "app")
        self.file("/usr/bin/dash", "shell")
        self.file("/usr/lib/aarch64-linux-gnu/libc.so.6", "library")
        self.file("/usr/share/doc/tono-canvas/copyright", "license")
        self.file("/usr/share/ca-certificates/test.crt", "certificate")
        self.file("/etc/shadow", "private-password")
        self.file("/home/avaota/private-board.bin", "private-board")
        self.file("/etc/NetworkManager/system-connections/private.nmconnection", "private-wifi")
        self.link("/bin", "usr/bin")
        self.link("/lib", "usr/lib")
        self.link("/usr/bin/sh", "dash")
        self.link("/etc/ssl/certs/test.pem", "/usr/share/ca-certificates/test.crt")
        self.file("/var/cache/fontconfig/test.cache", "generated-font-cache")
        self.file("/var/lib/dpkg/info/tono-canvas.list", "/.\n/usr/bin/tono-canvas\n/usr/share/doc/tono-canvas/copyright\n/usr/share/ca-certificates/test.crt\n/etc/shadow\n/home/avaota/private-board.bin\n/etc/NetworkManager/system-connections/private.nmconnection\n")
        self.file("/var/lib/dpkg/info/libc6:arm64.list", "/lib/aarch64-linux-gnu/libc.so.6\n")
        self.file("/var/lib/dpkg/info/dash.list", "/bin/dash\n/bin/sh\n")

    def tearDown(self):
        self.temporary.cleanup()

    def file(self, guest, content):
        path = self.source / guest.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def link(self, guest, target):
        path = self.source / guest.lstrip("/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)

    def export(self):
        with mock.patch.object(runtime.subprocess, "check_output", return_value="tono-canvas\t1\tarm64"):
            return runtime.export_runtime(self.source, self.destination, self.app_deb, ["tono-canvas"])

    def test_package_closure_merged_usr_generated_assets_and_licenses(self):
        (self.source / "usr/bin/tono-canvas").chmod(0o4755)
        result = self.export()
        self.assertEqual(result["packages"], 3)
        self.assertEqual(os.readlink(self.destination / "bin"), "usr/bin")
        self.assertEqual((self.destination / "bin/sh").read_text(), "shell")
        self.assertEqual((self.destination / "lib/aarch64-linux-gnu/libc.so.6").read_text(), "library")
        self.assertEqual(os.readlink(self.destination / "etc/ssl/certs/test.pem"), "/usr/share/ca-certificates/test.crt")
        self.assertEqual((self.destination / "usr/share/doc/tono-canvas/copyright").read_text(), "license")
        self.assertFalse(stat.S_IMODE((self.destination / "usr/bin/tono-canvas").stat().st_mode) & 0o6000)
        manifest = json.loads((self.destination / "runtime-manifest.json").read_text())
        self.assertEqual(manifest["files"]["/usr/bin/tono-canvas"]["sha256"], runtime.sha256(self.source / "usr/bin/tono-canvas"))
        self.assertTrue(manifest["files"]["/var/cache/fontconfig/test.cache"]["generated"])

    def test_private_identity_profiles_boards_and_package_database_excluded(self):
        self.export()
        for path in ("etc/shadow", "home/avaota/private-board.bin", "etc/NetworkManager/system-connections/private.nmconnection", "var/lib/dpkg/status"):
            self.assertFalse((self.destination / path).exists(), path)
        manifest = (self.destination / "runtime-manifest.json").read_text()
        for value in ("private-password", "private-board", "private-wifi"):
            self.assertNotIn(value, manifest)

    def test_relative_symlink_escape_and_excluded_target_rejected(self):
        self.link("/usr/bin/bad", "../../../../outside")
        with (self.source / "var/lib/dpkg/info/tono-canvas.list").open("a") as stream:
            stream.write("/usr/bin/bad\n")
        with self.assertRaisesRegex(ValueError, "escapes guest"):
            self.export()

    def test_absolute_guest_symlink_does_not_read_host_files(self):
        self.file("/usr/share/example", "guest-value")
        self.link("/usr/bin/example", "/usr/share/example")
        with (self.source / "var/lib/dpkg/info/tono-canvas.list").open("a") as stream:
            stream.write("/usr/share/example\n/usr/bin/example\n")
        self.export()
        self.assertEqual((self.destination / "usr/share/example").read_text(), "guest-value")
        self.assertEqual(os.readlink(self.destination / "usr/bin/example"), "/usr/share/example")

    def test_generated_public_alternative_links_retain_owned_target(self):
        self.file("/usr/bin/real-editor", "editor")
        self.link("/usr/bin/editor", "/etc/alternatives/editor")
        self.link("/etc/alternatives/editor", "/usr/bin/real-editor")
        self.link("/etc/editor", "/usr/bin/editor")
        with (self.source / "var/lib/dpkg/info/tono-canvas.list").open("a") as stream:
            stream.write("/etc/editor\n/usr/bin/real-editor\n")
        self.export()
        self.assertEqual((self.destination / "usr/bin/real-editor").read_text(), "editor")
        self.assertEqual(os.readlink(self.destination / "usr/bin/editor"), "/etc/alternatives/editor")

    def test_standard_volatile_alias_is_kept_without_copying_runtime_state(self):
        self.link("/var/lock", "/run/lock")
        self.file("/run/lock/private.lock", "volatile-state")
        with (self.source / "var/lib/dpkg/info/tono-canvas.list").open("a") as stream:
            stream.write("/var/lock\n")
        self.export()
        self.assertEqual(os.readlink(self.destination / "var/lock"), "/run/lock")
        self.assertFalse((self.destination / "run/lock/private.lock").exists())

    def test_package_owned_systemd_mask_is_kept_without_copying_device(self):
        self.link("/usr/lib/systemd/system/x11-common.service", "/dev/null")
        with (self.source / "var/lib/dpkg/info/tono-canvas.list").open("a") as stream:
            stream.write("/usr/lib/systemd/system/x11-common.service\n")
        self.export()
        self.assertEqual(os.readlink(self.destination / "usr/lib/systemd/system/x11-common.service"), "/dev/null")
        self.assertFalse((self.destination / "dev/null").exists())

    def test_optional_ubuntu_locale_alias_can_remain_dangling(self):
        self.link("/usr/share/locale/zh_CN/LC_TIME/coreutils.mo", "../LC_MESSAGES/coreutils.mo")
        with (self.source / "var/lib/dpkg/info/tono-canvas.list").open("a") as stream:
            stream.write("/usr/share/locale/zh_CN/LC_TIME/coreutils.mo\n")
        self.export()
        manifest = json.loads((self.destination / "runtime-manifest.json").read_text())
        self.assertEqual(manifest["uninstalled_optional_locale_alias_targets"], ["/usr/share/locale/zh_CN/LC_MESSAGES/coreutils.mo"])

    def test_missing_package_asset_fails_and_existing_destination_untouched(self):
        (self.source / "usr/bin/tono-canvas").unlink()
        with self.assertRaisesRegex(ValueError, "file is missing"):
            self.export()
        shutil_destination = self.base / "existing"
        shutil_destination.mkdir()
        marker = shutil_destination / "keep"
        marker.write_text("unchanged")
        self.destination = shutil_destination
        with self.assertRaisesRegex(ValueError, "absent or empty"):
            self.export()
        self.assertEqual(marker.read_text(), "unchanged")

    def test_only_known_trimmed_docs_and_non_chinese_english_locales_may_be_missing(self):
        self.link("/usr/share/doc/tono-canvas/changelog.Debian.gz", "changelog.gz")
        with (self.source / "var/lib/dpkg/info/tono-canvas.list").open("a") as stream:
            stream.write("/usr/share/doc/tono-canvas/README\n/usr/share/man/man1/tono.1.gz\n/usr/share/locale/fr/LC_MESSAGES/tono.mo\n/usr/share/doc/tono-canvas/changelog.Debian.gz\n")
        self.export()
        manifest = json.loads((self.destination / "runtime-manifest.json").read_text())
        self.assertEqual(len(manifest["omitted_previously_trimmed_documentation"]), 4)
        self.assertEqual(os.readlink(self.destination / "usr/share/doc/tono-canvas/changelog.Debian.gz"), "changelog.gz")
        for path in ("/usr/share/doc/app/copyright", "/usr/share/doc/app/vendor-LiCeNsE.txt", "/usr/share/locale/zh_CN/LC_MESSAGES/tono.mo", "/usr/share/locale/en/LC_MESSAGES/tono.mo", "/usr/share/qt6/translations/qtwebengine_locales/fr.pak"):
            self.assertFalse(runtime.trimmed_documentation(path), path)

    def test_changed_app_version_and_nested_trees_rejected(self):
        with mock.patch.object(runtime.subprocess, "check_output", return_value="tono-canvas\t2\tarm64"):
            with self.assertRaisesRegex(ValueError, "version differs"):
                runtime.export_runtime(self.source, self.destination, self.app_deb, ["tono-canvas"])
        self.destination = self.source / "nested"
        with self.assertRaisesRegex(ValueError, "separate trees"):
            self.export()


class SandboxPreflightTests(unittest.TestCase):
    def test_rejects_root_before_fork(self):
        with mock.patch.object(preflight.os, "getuid", return_value=0), mock.patch.object(preflight.os, "fork") as fork, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(preflight.main(), 1)
            fork.assert_not_called()

    def test_rejects_sandbox_disabling_environment_before_fork(self):
        for values in ({"QTWEBENGINE_DISABLE_SANDBOX": "1"}, {"QTWEBENGINE_DISABLE_SANDBOX": "0"}, {"QTWEBENGINE_CHROMIUM_FLAGS": "--no-sandbox"}, {"QTWEBENGINE_CHROMIUM_FLAGS": "--disable-seccomp-filter-sandbox"}):
            with mock.patch.object(preflight.os, "getuid", return_value=1000), mock.patch.dict(preflight.os.environ, values, clear=True), mock.patch.object(preflight.os, "fork") as fork, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(preflight.main(), 1)
                fork.assert_not_called()

    def test_parent_fails_closed_when_namespace_child_fails_or_dies(self):
        for child_status, expected in ((0, 0), (256, 1), (9, 1)):
            with mock.patch.object(preflight.os, "getuid", return_value=1000), mock.patch.dict(preflight.os.environ, {}, clear=True), mock.patch.object(preflight.os, "fork", return_value=42), mock.patch.object(preflight.os, "waitpid", return_value=(42, child_status)), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(preflight.main(), expected)


if __name__ == "__main__":
    unittest.main()
