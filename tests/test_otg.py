"""Exercise OTG installation into fresh and cached images without root or USB."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
SERVICES = REPO / 'target/services/avaota-otg'
FILES = {
    'avaota-otg-serial': 'usr/local/sbin/avaota-otg-serial',
    'avaota-otg-serial.service': 'etc/systemd/system/avaota-otg-serial.service',
    'serial-getty.conf': 'etc/systemd/system/serial-getty@ttyGS0.service.d/otg.conf',
}


class OtgInstallTests(unittest.TestCase):
    def install(self, root, stage='pack', board='avaota-a1', kind='kiosk', fail_apt=False):
        # Load the actual builder's hook defaults, without running image creation.
        source = (REPO / f'scripts/{stage}.sh').read_text()
        prefix = source.split('\nHOST_ARCH=' if stage == 'mkrootfs' else '\nworkspace=', 1)[0]
        command = prefix + '''
            chroot() {
                local root=$1
                shift
                printf '%s\\n' "$*" >> "$root/chroot-calls"
                case "$*" in
                    'apt-get update') [[ $FAIL_APT != yes ]] ;;
                    'apt-get install -y --no-install-recommends python3-minimal')
                        mkdir -p "$root/usr/bin"
                        touch "$root/usr/bin/python3"
                        chmod 755 "$root/usr/bin/python3" ;;
                    'systemctl enable avaota-otg-serial.service serial-getty@ttyGS0.service') ;;
                    *) echo "Unexpected chroot operation: $*" >&2; return 42 ;;
                esac
            }
            source "$TEST_REPO/boards/$BOARD.conf"
            setup_otg
        '''
        return subprocess.run(['bash', '-ec', command], text=True, capture_output=True,
                              env=dict(os.environ, TEST_REPO=str(REPO), ROOTFS=str(root),
                                       TYPE=kind, BOARD=board, VERSION='noble',
                                       KERNEL_TARGET='bsp', FAIL_APT='yes' if fail_apt else 'no'))

    def test_fresh_and_cached_rootfs_install_current_files_and_enable_offline(self):
        for stage, kind in [('mkrootfs', 'cli'), ('pack', 'kiosk')]:
            with self.subTest(stage=stage, kind=kind), tempfile.TemporaryDirectory(prefix='otg root ') as directory:
                root = Path(directory)
                result = self.install(root, stage=stage, kind=kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (root / 'chroot-calls').read_text().splitlines()
                self.assertEqual(len(calls), 3)  # Python installation, then offline enable.
                for name, destination in FILES.items():
                    self.assertEqual((root / destination).read_bytes(), (SERVICES / name).read_bytes())
                    expected = 0o755 if name == 'avaota-otg-serial' else 0o644
                    self.assertEqual(stat.S_IMODE((root / destination).stat().st_mode), expected)
                    (root / destination).write_text('outdated cached configuration\n')
                (root / 'chroot-calls').unlink()
                result = self.install(root, stage=stage, kind=kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(len((root / 'chroot-calls').read_text().splitlines()), 1)
                for name, destination in FILES.items():
                    self.assertEqual((root / destination).read_bytes(), (SERVICES / name).read_bytes())

    def test_missing_dependency_aborts_before_enabling_console(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.install(root, fail_apt=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / FILES['avaota-otg-serial.service']).exists())
            self.assertNotIn('systemctl', (root / 'chroot-calls').read_text())

    def test_other_boards_keep_the_default_noop_in_both_build_stages(self):
        for stage in ['mkrootfs', 'pack']:
            for board in ['avaota-c1', 'dshanpi-a1']:
                with self.subTest(stage=stage, board=board), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    result = self.install(root, stage=stage, board=board, kind='cli')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(list(root.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
