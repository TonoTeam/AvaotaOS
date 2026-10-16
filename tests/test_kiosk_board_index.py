"""Recovery fixtures contain no user data and require no root or mounted image."""
import importlib.util
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

SPEC = importlib.util.spec_from_file_location(
    'repair_board_index', Path(__file__).resolve().parents[1] / 'scripts/repair-kiosk-board-index.py')
RECOVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECOVERY)


class BoardIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        (self.data / 'nvs').mkdir()
        self.archive = self.data / 'boards-v2.bin'
        self.archive.touch()

    def bank(self, slot, bank, generation, corrupt=False):
        preview = b'\xff' * RECOVERY.PREVIEW_BYTES
        payload = b'synthetic archive fixture' * 2
        header = struct.pack('<8I', 0x31445242, 1, generation, len(payload),
                             2048, 2560, zlib.crc32(preview), zlib.crc32(payload))
        header += struct.pack('<I', zlib.crc32(header))
        if corrupt:
            payload = b'!' + payload[1:]
        with self.archive.open('r+b') as stream:
            stream.seek((slot * 2 + bank) * RECOVERY.BANK_BYTES)
            stream.write(header)
            stream.seek((slot * 2 + bank) * RECOVERY.BANK_BYTES + 64)
            stream.write(preview + payload)

    def index(self, slot, data=b'\xff' * 24):
        path = self.data / 'nvs' / f'bb_boards.board{slot}'
        path.write_bytes(data)
        path.chmod(0o600)
        return path

    def test_recovers_newest_valid_bank_with_backup_without_changing_archive(self):
        self.bank(0, 0, 1)
        self.bank(0, 1, 2)
        index = self.index(0)
        plan = RECOVERY.inspect(self.data)
        self.assertEqual(plan['recoverable'][0]['generation'], 2)
        self.assertEqual(index.read_bytes(), b'\xff' * 24)  # dry run
        backup = RECOVERY.apply(self.data, plan)
        self.assertEqual((backup / index.name).read_bytes(), b'\xff' * 24)
        self.assertEqual(RECOVERY.digest(self.archive), plan['archive_sha256'])
        edited, crc, deleted_through, deleted = RECOVERY.META.unpack(index.read_bytes())
        self.assertEqual((edited, deleted_through, deleted), (0, 0, 0))
        self.assertEqual(crc, plan['recoverable'][0]['archive_crc'])
        self.assertEqual(index.stat().st_mode & 0o777, 0o600)
        self.assertEqual(RECOVERY.inspect(self.data)['recoverable'], [])

    def test_rejects_invalid_payload_and_uses_older_valid_bank(self):
        self.bank(0, 0, 1)
        self.bank(0, 1, 2, corrupt=True)
        self.index(0)
        self.assertEqual(RECOVERY.inspect(self.data)['recoverable'][0]['generation'], 1)

    def test_preserves_valid_deletion_marker(self):
        self.bank(0, 0, 1)
        index = self.index(0, RECOVERY.META.pack(0, 0, 1, 1))
        before = index.read_bytes()
        plan = RECOVERY.inspect(self.data)
        self.assertEqual(plan['recoverable'], [])
        self.assertIsNone(RECOVERY.apply(self.data, plan))
        self.assertEqual(index.read_bytes(), before)

    def test_refuses_no_valid_bank_and_changed_archive(self):
        index = self.index(0)
        plan = RECOVERY.inspect(self.data)
        self.assertEqual(plan['blocked_without_valid_bank'], [0])
        with self.assertRaisesRegex(ValueError, 'no valid bank'):
            RECOVERY.apply(self.data, plan)
        self.assertEqual(index.read_bytes(), b'\xff' * 24)
        self.bank(0, 0, 1)
        plan = RECOVERY.inspect(self.data)
        self.bank(0, 1, 2)
        with self.assertRaisesRegex(ValueError, 'changed after inspection'):
            RECOVERY.apply(self.data, plan)

    def test_refuses_symlinked_storage(self):
        self.archive.unlink()
        external = self.data / 'untouched'
        external.write_bytes(b'keep')
        self.archive.symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'regular'):
            RECOVERY.inspect(self.data)
        self.assertEqual(external.read_bytes(), b'keep')

    def test_cli_refuses_to_apply_while_app_is_running(self):
        self.bank(0, 0, 1)
        index = self.index(0)
        commands = self.data / 'bin'
        commands.mkdir()
        pgrep = commands / 'pgrep'
        pgrep.write_text('#!/bin/sh\nexit 0\n')
        pgrep.chmod(0o755)
        environment = dict(os.environ, PATH=str(commands) + os.pathsep + os.environ['PATH'])
        result = subprocess.run(
            [sys.executable, str(SPEC.origin), '--data-dir', str(self.data), '--apply'],
            env=environment, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Stop tono-kiosk.service', result.stderr)
        self.assertEqual(index.read_bytes(), b'\xff' * 24)
        self.assertEqual(list(self.data.glob('.index-recovery-*')), [])


if __name__ == '__main__':
    unittest.main()
