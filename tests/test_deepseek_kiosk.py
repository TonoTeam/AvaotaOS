"""Behavioral tests: private boot handoff, authentication rotation and cleanup."""
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / 'target/deepseek-harness'


def until(predicate, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.05)
    raise AssertionError('condition did not become true')


class BootHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.runtime = self.root / 'run'
        self.runtime.mkdir()
        self.env = dict(os.environ, XDG_RUNTIME_DIR=str(self.runtime), HOME=str(self.root))
        self.procs = []
        self.addCleanup(self.stop_all)

    def stop_all(self):
        for child in self.procs:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
            if child.stdout:
                child.stdout.close()

    def script(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)
        return path

    def run_helper(self, name):
        target = self.bin / name
        shutil.copyfile(TEMPLATES / name, target)
        target.chmod(0o755)
        child = subprocess.Popen(['python3', str(target)], env=self.env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.procs.append(child)
        return child

    def test_server_private_token_and_graceful_cleanup(self):
        self.script('tono-dsh', '#!/usr/bin/env python3\nimport time\n'
                    'print("dsh web: http://127.0.0.1:3080/?token=example-secret", flush=True)\n'
                    'time.sleep(60)\n')
        child = self.run_helper('tono-dsh-server')
        state = self.runtime / 'tono-deepseek-harness/launch.json'
        until(state.exists)
        data = json.loads(state.read_text())
        self.assertTrue(data['url'].endswith('example-secret'))
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(state.parent.stat().st_mode), 0o700)
        child.terminate()
        output = child.communicate(timeout=10)[0].decode()
        self.assertNotIn('example-secret', output)
        self.assertEqual(child.returncode, 0)
        self.assertFalse(state.exists())
        with self.assertRaises(ProcessLookupError):
            os.kill(data['pid'], 0)

    def test_browser_waits_then_reauthenticates_without_disabling_sandbox(self):
        log = self.root / 'browser.jsonl'
        self.script('fake-chrome', '#!/usr/bin/env python3\nimport json,sys,time\n'
                    f'with open({str(log)!r},"a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                    'time.sleep(60)\n')
        (self.root / 'browser.json').write_text(json.dumps({'executable':'bin/fake-chrome'}))
        state = self.runtime / 'tono-deepseek-harness/launch.json'
        state.parent.mkdir()
        child = self.run_helper('tono-dsh-browser')
        time.sleep(.6)
        self.assertFalse(log.exists())
        def publish(url, pid=None):
            temp = state.with_suffix('.tmp')
            temp.write_text(json.dumps({'url':url,'pid':pid or os.getpid()}))
            temp.replace(state)
        publish('https://example.com/?token=wrong')
        time.sleep(.6)
        self.assertFalse(log.exists())
        publish('http://127.0.0.1:3080/?token=one')
        until(log.exists)
        publish('http://127.0.0.1:3080/?token=two')
        until(lambda: len(log.read_text().splitlines()) == 2)
        commands = [json.loads(x) for x in log.read_text().splitlines()]
        self.assertEqual(commands[0][-1], 'http://127.0.0.1:3080/?token=one')
        self.assertEqual(commands[1][-1], 'http://127.0.0.1:3080/?token=two')
        self.assertIn('--start-maximized', commands[1])
        self.assertNotIn('--kiosk', commands[1])
        self.assertFalse(any('no-sandbox' in arg for cmd in commands for arg in cmd))
        child.terminate()
        child.wait(timeout=10)
        self.assertEqual(child.returncode, 0)


if __name__ == '__main__':
    unittest.main()
