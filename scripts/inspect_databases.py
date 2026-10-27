"""Inventory only the user-selected data root; do not choose an account implicitly."""
import argparse
from pathlib import Path

from common import columns, readonly, run, tables, validate


def inspect(root):
    root = Path(root).resolve(strict=True)
    paths = [root] if root.is_file() else sorted(root.rglob('*.db'))
    result = []
    for path in paths:
        with path.open('rb') as stream:
            plain = stream.read(16) == b'SQLite format 3\0'
        item = {'relative_path': path.name if root.is_file() else path.relative_to(root).as_posix(),
                'bytes': path.stat().st_size, 'plaintext': plain,
                'wal_bytes': Path(str(path)+'-wal').stat().st_size if Path(str(path)+'-wal').exists() else 0}
        if plain:
            try:
                with readonly(path) as db:
                    validate(db)
                    item['schema'] = {t: sorted(columns(db, t)) for t in tables(db)}
                    item['integrity'] = 'ok'
            except Exception:
                item['integrity'] = 'failed'
        else:
            item['status'] = 'not_plaintext_not_proof_of_encryption_or_version_incompatibility'
        result.append(item)
    return {'databases': result, 'note': 'Paths may identify accounts; keep this report private.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', required=True, type=Path)
    return inspect(p.parse_args().root)


if __name__ == '__main__':
    run(main)
