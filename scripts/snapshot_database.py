"""SQLite backup for plaintext, or fail-closed copy for closed encrypted sources."""
import argparse
import hashlib
import shutil
import time
from contextlib import closing
from pathlib import Path

from common import output_file, readonly, run, validate


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot(source, output, source_closed=False, timeout_seconds=30):
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if source == output:
        raise ValueError('SOURCE_OUTPUT_CONFLICT')
    if not 1 <= timeout_seconds <= 300:
        raise ValueError('BACKUP_TIMEOUT_INVALID')
    with source.open('rb') as stream:
        plain = stream.read(16) == b'SQLite format 3\0'
    with output_file(output) as temp:
        if plain:
            import sqlite3
            deadline = time.monotonic() + timeout_seconds
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise ValueError('BACKUP_TIMEOUT')
            with readonly(source) as src, closing(sqlite3.connect(temp)) as dst:
                src.backup(dst, pages=256, sleep=0.05, progress=progress)
                validate(dst)
        else:
            if not source_closed:
                raise ValueError('SOURCE_MUST_BE_CLOSED: copying live encrypted files is not a consistent backup')
            def check_sidecars():
                for suffix in ('-wal', '-journal'):
                    p = Path(str(source)+suffix)
                    if p.exists() and p.stat().st_size:
                        raise ValueError('UNMERGED_JOURNAL: use a SQLCipher-aware backup')
            check_sidecars()
            before = digest(source)
            shutil.copyfile(source, temp)
            check_sidecars()
            if before != digest(source) or before != digest(temp):
                raise ValueError('SOURCE_CHANGED')
    return {'kind': 'sqlite_backup' if plain else 'closed_encrypted_copy', 'bytes': output.stat().st_size}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--source-closed', action='store_true', help='User confirmed WeChat exited; never stops WeChat automatically')
    p.add_argument('--timeout-seconds', type=int, default=30, help='Plaintext backup deadline, 1-300 seconds')
    args = p.parse_args()
    return snapshot(args.source, args.output, args.source_closed, args.timeout_seconds)


if __name__ == '__main__':
    run(main)
