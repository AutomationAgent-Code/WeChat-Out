"""Built-in DLL-free account discovery, key capture and offline chat export."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path

from capture_key import protect
from common import readonly, run, validate
from decrypt_sqlcipher import decrypt, unprotect, wipe
from memory_keys import Budget, WindowsMemory, own_weixin_processes, scan_memory, verify_raw_key
from read_chats import export
from snapshot_database import snapshot
from wal_state import sidecar_guard


def account_root(path):
    root = Path(path).expanduser().resolve(strict=True)
    if (root/'db_storage').is_dir():
        root = (root/'db_storage').resolve()
    if not (root/'contact'/'contact.db').is_file() or not (root/'message').is_dir():
        raise ValueError('ACCOUNT_LAYOUT_NOT_FOUND')
    return root


def chat_databases(root):
    root = account_root(root)
    paths = [root/'contact'/'contact.db']
    paths += sorted(p for p in (root/'message').rglob('*')
                    if p.is_file() and re.fullmatch(r'message_\d+\.db', p.name, re.IGNORECASE))
    if len(paths) < 2:
        raise ValueError('NO_MESSAGE_SHARDS')
    if len(paths) > 256:
        raise ValueError('TOO_MANY_DATABASES')
    for path in paths:
        # Symlinks/junctions cannot expand the selected account's scope.
        if not path.resolve(strict=True).is_relative_to(root):
            raise ValueError('DATABASE_OUTSIDE_ACCOUNT')
    return paths


def account_binding(root):
    return hashlib.sha256(os.path.normcase(str(account_root(root))).encode('utf-8')).hexdigest()


def collect_pages(root):
    pages = {}
    for path in chat_databases(root):
        with path.open('rb') as stream:
            page = stream.read(4096)
        if page[:16] == b'SQLite format 3\0':
            continue
        if len(page) != 4096:
            raise ValueError('CIPHER_SIZE_INVALID')
        pages.setdefault(page[:16], []).append(page)
    return pages


def discover_accounts(roots=None):
    if roots is None:
        roots = [Path.home()/'Documents']
        # Respect Windows redirected Documents without searching other profiles.
        if os.name == 'nt':
            import ctypes
            buf = ctypes.create_unicode_buffer(32768)
            api = ctypes.WinDLL('shell32')
            api.SHGetFolderPathW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p]
            api.SHGetFolderPathW.restype = ctypes.c_long
            if api.SHGetFolderPathW(None, 5, None, 0, buf) == 0:
                roots.append(Path(buf.value))
        config = Path(os.environ.get('APPDATA', str(Path.home()))) / 'Tencent'/'xwechat'/'config'
        if config.is_dir():
            for ini in sorted(config.glob('*.ini'))[:128]:
                with ini.open('rb') as stream:
                    raw = stream.read(4096)
                for encoding in ('utf-8-sig', 'utf-16', 'gbk'):
                    try:
                        value = raw.decode(encoding).strip().strip('\0').strip('"')
                        if value and Path(value).is_absolute() and Path(value).is_dir():
                            roots.append(Path(value))
                            break
                    except (UnicodeError, ValueError, OSError):
                        continue
    accounts = set()
    for base in roots:
        base = Path(base).expanduser()
        containers = [base] if base.name == 'xwechat_files' else [base/'xwechat_files']
        for container in containers:
            if container.is_dir():
                for candidate in container.glob('*/db_storage'):
                    try:
                        accounts.add(account_root(candidate))
                    except (ValueError, OSError):
                        continue
    return sorted(accounts, key=str)


def choose_account(explicit=None):
    if explicit:
        return account_root(explicit)
    accounts = discover_accounts()
    if len(accounts) != 1:
        raise ValueError('ACCOUNT_SELECTION_REQUIRED' if accounts else 'ACCOUNT_NOT_FOUND')
    return accounts[0]


def choose_pid(explicit=None):
    pids = own_weixin_processes()
    if explicit is not None:
        if explicit not in pids:
            raise ValueError('PID_NOT_OWN_MAIN_WEIXIN')
        return explicit
    if len(pids) != 1:
        raise ValueError('PID_SELECTION_REQUIRED' if pids else 'WEIXIN_NOT_RUNNING')
    return pids[0]


def capture_account(root, pid, output, seconds=60, max_bytes=4*1024**3, memory_factory=WindowsMemory):
    root = account_root(root)
    pages = collect_pages(root)
    if not pages:
        raise ValueError('NO_ENCRYPTED_CHAT_DATABASES')
    if Path(output).exists():
        raise ValueError('OUTPUT_EXISTS')
    budget = Budget(seconds, max_bytes)
    memory = memory_factory(pid)
    keys, payload = {}, bytearray()
    try:
        keys = scan_memory(memory.regions(budget), memory.read, pages, budget)
        if len(keys) != len(pages):
            raise ValueError('KEYS_INCOMPLETE: memory layout, account/process selection or key not loaded')
        # Only the selected account's HMAC-validated per-salt keys enter storage.
        payload = bytearray(json.dumps({'format': 'wechat-keyring-v1', 'profile': 'sqlcipher4-raw',
            'account': account_binding(root), 'keys': {salt.hex(): key.hex() for salt, key in keys.items()}}).encode())
        protect(payload, output)
        return {'keyring_saved': True, 'databases': sum(map(len, pages.values())), 'verified_salts': len(keys),
                'memory_bytes_read': budget.bytes, 'memory_modified': False, 'credential_storage': 'current_user_dpapi'}
    finally:
        for key in keys.values():
            wipe(key)
        wipe(payload)
        memory.close()


def load_keyring(path, root):
    payload = unprotect(path)
    result = {}
    success = False
    try:
        ring = json.loads(payload)
        if ring.get('format') != 'wechat-keyring-v1' or ring.get('profile') != 'sqlcipher4-raw':
            raise ValueError('KEYRING_FORMAT_INVALID')
        if ring.get('account') != account_binding(root):
            raise ValueError('KEYRING_ACCOUNT_MISMATCH')
        for salt, value in ring['keys'].items():
            salt_bytes, key = bytes.fromhex(salt), bytearray.fromhex(value)
            if len(salt_bytes) != 16 or len(key) != 32:
                wipe(key)
                raise ValueError('KEYRING_FORMAT_INVALID')
            result[salt_bytes] = key
        success = True
        return result
    except (TypeError, KeyError, AttributeError):
        raise ValueError('KEYRING_FORMAT_INVALID') from None
    finally:
        if not success:
            for key in result.values():
                wipe(key)
        wipe(payload)


def export_account(root, output_dir, keyring=None, source_closed=False, self_id=None):
    root = account_root(root)
    paths = chat_databases(root)
    if not source_closed:
        raise ValueError('SOURCE_MUST_BE_CLOSED')
    if os.name == 'nt' and own_weixin_processes():
        raise ValueError('WEIXIN_STILL_RUNNING: close normally after capturing keys; no automatic termination')
    output_dir = Path(output_dir).resolve()
    if output_dir.is_relative_to(root) or root.is_relative_to(output_dir):
        raise ValueError('OUTPUT_OVERLAPS_ACCOUNT')
    keys = load_keyring(keyring, root) if keyring else {}
    try:
        # Preflight every file before creating an output directory.
        for path in paths:
            sidecar_guard(path)
            with path.open('rb') as stream:
                page = stream.read(4096)
            if page[:16] != b'SQLite format 3\0':
                if not verify_raw_key(keys.get(page[:16], b''), page):
                    raise ValueError('KEYRING_MISSING_OR_STALE')
        output_dir.mkdir(parents=True, exist_ok=False)
        for path in paths:
            rel = path.relative_to(root)
            copy = output_dir/'working-copy'/rel
            snapshot(path, copy, source_closed=True)
            with copy.open('rb') as stream:
                page = stream.read(4096)
            target = output_dir/'decrypted'/rel
            if page[:16] == b'SQLite format 3\0':
                snapshot(copy, target)
            else:
                decrypt(copy, target, keys.get(page[:16], b''), raw_key=True)
        stats = export(output_dir/'decrypted'/'contact'/'contact.db', output_dir/'decrypted'/'message',
                       output_dir/'chats.jsonl', self_id)
        return {'exported': True, **stats}
    finally:
        for key in keys.values():
            wipe(key)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    commands.add_parser('discover', help='List only current-user account candidates and own Weixin main PIDs')
    capture = commands.add_parser('capture', help='Built-in read-only scan; no external DLL')
    capture.add_argument('--account', type=Path)
    capture.add_argument('--pid', type=int)
    capture.add_argument('--output', type=Path, default=Path('private/keyring.dpapi'))
    capture.add_argument('--timeout-seconds', type=int, default=60)
    capture.add_argument('--max-memory-mib', type=int, default=4096)
    capture.add_argument('--accept-own-account-memory-read', action='store_true', required=True)
    ex = commands.add_parser('export', help='Capture first, then close WeChat normally and export all chat shards')
    ex.add_argument('--account', type=Path)
    ex.add_argument('--keyring', type=Path)
    ex.add_argument('--output-dir', type=Path, required=True)
    ex.add_argument('--source-closed', action='store_true', required=True)
    ex.add_argument('--self-id')
    a = p.parse_args()
    if a.command == 'discover':
        return {'accounts': [str(root) for root in discover_accounts()], 'own_main_pids': own_weixin_processes(),
                'private_report': True}
    root = choose_account(a.account)
    if a.command == 'capture':
        return capture_account(root, choose_pid(a.pid), a.output, a.timeout_seconds, a.max_memory_mib*1024**2)
    return export_account(root, a.output_dir, a.keyring, a.source_closed, a.self_id)


if __name__ == '__main__':
    run(main)
