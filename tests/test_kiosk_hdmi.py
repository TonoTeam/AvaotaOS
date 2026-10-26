"""HDMI recovery regressions using the A527's observed driver state format."""
import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'target/services/tono-kiosk/tono-hdmi-watch'
loader = importlib.machinery.SourceFileLoader('hdmi_watch', str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
hdmi = importlib.util.module_from_spec(spec)
loader.exec_module(hdmi)

STUCK = '''[drv cfg]
| state | off | on | off | phypll | onboard | no | no | 0x0 | off |
[drv state]
| name | clock | output | thread | state | mask | encryption | clock |
| state | on | off | runing | in | 0x0 | disable | off |
[dw ctrl]
| state | 0 | 0 | 0 | 8 | off | off |
'''


class HdmiTests(unittest.TestCase):
    def test_connected_but_disabled_fault_including_missed_unplug(self):
        self.assertTrue(hdmi.output_stuck(STUCK, ['connected']))
        for statuses in ([], ['disconnected'], ['unknown'], ['connected', 'connected']):
            self.assertFalse(hdmi.output_stuck(STUCK, statuses))
        for source in ('', STUCK.replace('| off | runing', '| on | runing'),
                       STUCK.replace('| in |', '| out |'), STUCK.split('[drv state]')[0]):
            self.assertFalse(hdmi.output_stuck(source, ['connected']))

    def test_debounce_retry_limit_and_next_hotplug(self):
        gate = hdmi.RecoveryGate()
        self.assertFalse(gate.ready(True, 0))
        self.assertFalse(gate.ready(False, 1))
        self.assertFalse(gate.ready(True, 2))
        self.assertTrue(gate.ready(True, 3))
        for second in range(4, 13):
            self.assertFalse(gate.ready(True, second))
        self.assertTrue(gate.ready(True, 13))
        self.assertFalse(gate.ready(False, 14))
        self.assertFalse(gate.ready(True, 15))
        self.assertTrue(gate.ready(True, 16))

    def test_uses_xorg_name_and_enables_after_off_failure(self):
        for failure in (None, subprocess.CalledProcessError(1, 'xrandr'),
                        subprocess.TimeoutExpired('xrandr', 8)):
            with self.subTest(failure=failure), patch.object(hdmi, 'needs_recovery', return_value=True), \
                    patch.object(hdmi, 'xrandr') as run:
                run.side_effect = ['HDMI-1 connected primary 2560x1600+0+0\n', failure, '']
                if failure is None:
                    hdmi.recover()
                else:
                    with self.assertRaises(type(failure)):
                        hdmi.recover()
                self.assertEqual(run.call_args_list[-2].args, ('--output', 'HDMI-1', '--off'))
                self.assertEqual(run.call_args_list[-1].args, ('--output', 'HDMI-1', '--auto'))

    def test_no_modeset_if_unplugged_during_probe_or_multiple_outputs(self):
        for query, stuck in [('HDMI-1 disconnected\n', True),
                             ('HDMI-1 connected\nHDMI-2 connected\n', True),
                             ('HDMI-1 connected\n', False)]:
            with patch.object(hdmi, 'xrandr', return_value=query) as run, \
                    patch.object(hdmi, 'needs_recovery', return_value=stuck):
                hdmi.recover()
                run.assert_called_once_with('--query')


if __name__ == '__main__':
    unittest.main()
