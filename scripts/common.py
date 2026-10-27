"""Shared read-only I/O and no-clobber output helpers."""
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def readonly(path):
    path = Path(path).resolve(strict=True)
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        yield db
    finally:
        db.close()


def validate(db):
    if [row[0] for row in db.execute('PRAGMA quick_check')] != ['ok']:
        raise ValueError('SQLITE_INTEGRITY_FAILED')


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


def tables(db):
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]


def columns(db, table):
    return {r[1] for r in db.execute('PRAGMA table_info(' + identifier(table) + ')')}


@contextmanager
def output_file(output):
    """Publish only complete outputs; never replace an existing file, even on races."""
    output = Path(output).resolve()
    def check_target():
        if os.path.lexists(output):
            raise FileExistsError('OUTPUT_EXISTS: choose a new output path')
        for suffix in ('-wal', '-shm', '-journal'):
            if os.path.lexists(str(output) + suffix):
                raise ValueError('OUTPUT_SIDECAR_EXISTS: choose a clean output path')
    check_target()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.wechat-', suffix='.partial', dir=output.parent)
    os.close(fd)
    temp = Path(name)
    try:
        yield temp
        check_target()
        os.link(temp, output)
    finally:
        temp.unlink(missing_ok=True)


def run(main):
    try:
        result = main()
    except (ValueError, OSError, sqlite3.Error, ImportError) as exc:
        # Do not echo exception text: it may contain keys, SQL or private paths.
        code = str(exc).split(':', 1)[0]
        if not code.isupper() or not code.replace('_', '').isalnum():
            code = type(exc).__name__
        print(json.dumps({'ok': False, 'error': code}))
        raise SystemExit(1)
    print(json.dumps({'ok': True, **(result or {})}, ensure_ascii=False))
