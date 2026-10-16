#!/usr/bin/env python3
"""Recover only all-0xff Linux board metadata, using checksummed archive banks.

Dry-run by default. Stop the app before --apply. Board archives are never written.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import zlib

BANK_BYTES = 4 * 1024 * 1024
SLOTS = 12
PREVIEW_BYTES = 144 * 180 * 3
HEADER = struct.Struct('<9I')
META = struct.Struct('<qIIB7x')


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def valid_banks(archive):
    result = {}
    with archive.open('rb') as stream:
        for slot in range(SLOTS):
            banks = []
            for bank in range(2):
                offset = (slot * 2 + bank) * BANK_BYTES
                stream.seek(offset)
                raw = stream.read(HEADER.size)
                if len(raw) != HEADER.size:
                    continue
                magic, version, generation, length, width, height, pc, ac, hc = HEADER.unpack(raw)
                if (magic != 0x31445242 or version != 1 or not 0 < generation < 0xffffffff
                        or (width, height) != (2048, 2560)
                        or not 20 <= length <= BANK_BYTES - 64 - PREVIEW_BYTES
                        or zlib.crc32(raw[:32]) != hc):
                    continue
                stream.seek(offset + 64)
                preview = stream.read(PREVIEW_BYTES)
                payload = stream.read(length)
                if (len(preview) == PREVIEW_BYTES and len(payload) == length
                        and zlib.crc32(preview) == pc and zlib.crc32(payload) == ac):
                    banks.append({'bank': bank, 'generation': generation, 'archive_crc': ac})
            if banks:
                result[slot] = max(banks, key=lambda value: value['generation'])
    return result


def inspect(data_dir):
    data_dir = data_dir.resolve(strict=True)
    archive, nvs = data_dir / 'boards-v2.bin', data_dir / 'nvs'
    if not archive.is_file() or archive.is_symlink() or not nvs.is_dir() or nvs.is_symlink():
        raise ValueError('Expected a regular boards-v2.bin and nvs directory')
    banks = valid_banks(archive)
    repairs, blocked = [], []
    for slot in range(SLOTS):
        index = nvs / f'bb_boards.board{slot}'
        if index.is_symlink():
            raise ValueError('Refusing a symlinked board index')
        if not index.is_file() or index.read_bytes() != b'\xff' * META.size:
            continue
        if slot not in banks:
            blocked.append(slot)
        else:
            repairs.append({'slot': slot, **banks[slot]})
    return {'archive_sha256': digest(archive), 'recoverable': repairs,
            'blocked_without_valid_bank': blocked}


def sync_directory(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomic(path, data, original_stat):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.tono-index-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            if os.geteuid() == 0:
                os.fchown(stream.fileno(), original_stat.st_uid, original_stat.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(original_stat.st_mode))
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
        if path.read_bytes() != data:
            raise OSError('Index read-back verification failed')
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply(data_dir, plan):
    data_dir = data_dir.resolve(strict=True)
    if plan['blocked_without_valid_bank']:
        raise ValueError('A damaged index has no valid bank; manual recovery is required')
    if inspect(data_dir) != plan:
        raise ValueError('Storage changed after inspection; refusing to apply')
    if not plan['recoverable']:
        return None
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path(tempfile.mkdtemp(prefix=f'.index-recovery-{stamp}-', dir=data_dir))
    originals = {}
    for item in plan['recoverable']:
        path = data_dir / 'nvs' / f"bb_boards.board{item['slot']}"
        originals[item['slot']] = path.stat()
        target = backup / path.name
        shutil.copy2(path, target)
        with target.open('rb') as stream:
            os.fsync(stream.fileno())
        if target.read_bytes() != b'\xff' * META.size:
            raise ValueError('Index changed before backup completed')
    report = backup / 'report.json'
    report.write_text(json.dumps(plan, indent=2) + '\n')
    os.chmod(report, 0o600)
    with report.open('rb') as stream:
        os.fsync(stream.fileno())
    sync_directory(backup)
    sync_directory(data_dir)
    for item in plan['recoverable']:
        path = data_dir / 'nvs' / f"bb_boards.board{item['slot']}"
        # The old edit time and deletion marker are unreadable. Preserve the
        # newest CRC-checked bank, and leave the unknown edit time at zero.
        metadata = META.pack(0, item['archive_crc'], 0, 0)
        write_atomic(path, metadata, originals[item['slot']])
    if digest(data_dir / 'boards-v2.bin') != plan['archive_sha256']:
        raise OSError('Board archive changed during index repair; inspect the backup report')
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='back up and repair all-0xff indices')
    args = parser.parse_args()
    try:
        plan = inspect(args.data_dir)
        if args.apply:
            owner = args.data_dir.stat().st_uid
            probe = subprocess.run(['pgrep', '-u', str(owner), '-x', 'tono-canvas'],
                                   capture_output=True, text=True)
            if probe.returncode != 1:
                raise ValueError('Stop tono-kiosk.service and all app instances before --apply')
            backup = apply(args.data_dir, plan)
            plan['backup'] = str(backup) if backup else None
            plan['applied'] = True
        else:
            plan['applied'] = False
        print(json.dumps(plan, indent=2))
    except (OSError, ValueError) as error:
        parser.exit(1, f'Index recovery failed: {error}\n')


if __name__ == '__main__':
    main()
