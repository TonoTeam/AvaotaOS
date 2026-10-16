"""Exercise session selection and fallback without a display or GPU."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
START = REPO / 'target/services/tono-kiosk/tono-kiosk-start'


class GpuSessionTests(unittest.TestCase):
    def launch(self, requested, probe=0, hardware=0):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Keep the production flow; replace external programs with fixtures.
            for name, body in {
                'timeout': 'shift; exec \"$@\"', 'xauth': 'exit 0', 'mcookie': 'echo test-cookie',
                'tono-gpu-probe': f'exit {probe}',
                'xinit': 'echo "$TONO_RENDERER $*" >> "$RUN_LOG"\n'
                         f'if [ "$TONO_RENDERER" = hardware ]; then exit {hardware}; fi\nexit 0',
            }.items():
                path = root / name
                path.write_text('#!/bin/sh\n' + body + '\n')
                path.chmod(0o755)
            script = START.read_text().replace('/usr/bin/xinit', str(root / 'xinit')).replace(
                '/usr/local/libexec/tono-gpu-probe', str(root / 'tono-gpu-probe'))
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'],
                       XDG_RUNTIME_DIR=directory, TONO_RENDERER=requested,
                       RUN_LOG=str(root / 'runs'))
            result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
            log = (root / 'runs').read_text() if (root / 'runs').exists() else ''
            return result, log

    def test_hardware_success_and_clean_exit_do_not_fallback(self):
        result, log = self.launch('auto')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(log.splitlines()), 1)
        self.assertIn('-configdir tono-kiosk-hardware', log)

    def test_failed_preflight_never_starts_accelerated_xorg(self):
        result, log = self.launch('auto', probe=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('-configdir tono-kiosk-software', log)
        self.assertNotIn('hardware', log)

    def test_xorg_or_app_failure_retries_software_once(self):
        result, log = self.launch('auto', hardware=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line.split()[0] for line in log.splitlines()], ['hardware', 'software'])

    def test_manual_modes_and_invalid_config(self):
        result, log = self.launch('software', probe=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('software', log)
        result, log = self.launch('hardware', hardware=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(log.splitlines()), 1)
        result, log = self.launch('invalid')
        self.assertEqual(result.returncode, 2)
        self.assertEqual(log, '')
