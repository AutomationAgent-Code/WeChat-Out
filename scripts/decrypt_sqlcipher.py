"""Authenticated offline SQLCipher-4-layout decryptor, not a key extractor."""
import argparse
import ctypes
import getpass
import hashlib
import hmac
import os
from pathlib import Path

from common import output_file, readonly, run, validate


def wipe(value):
    if value:
        value[:] = b'\0' * len(value)


def unprotect(path):
    if os.name != 'nt':
        raise ValueError('DPAPI_REQUIRES_WINDOWS')
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    data = Path(path).read_bytes()
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source = Blob(len(data), buf)
    plain = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(plain)):
        raise ValueError('DPAPI_UNPROTECT_FAILED')
    try:
        return bytearray(ctypes.string_at(plain.data, plain.size))
    finally:
        ctypes.memset(plain.data, 0, plain.size)
        kernel.LocalFree(plain.data)


def key_input(args):
    if args.protected_key:
        key = unprotect(args.protected_key)
    else:
        # Never accept a plaintext key on the command line or print it.
        key = bytearray.fromhex(getpass.getpass('本人数据库密钥（64位hex，不回显）: ').strip())
    if len(key) != 32:
        wipe(key)
        raise ValueError('KEY_LENGTH_INVALID')
    return key


def decrypt(source, output, key, page_size=4096, iterations=256000, raw_key=False):
    from Crypto.Cipher import AES
    source, output = Path(source).resolve(strict=True), Path(output).resolve()
    if source == output:
        raise ValueError('SOURCE_OUTPUT_CONFLICT')
    # Page-only decryption cannot replay encrypted WAL or hot rollback journals.
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('UNMERGED_JOURNAL: close WeChat cleanly or use a SQLCipher-aware backup')
    if len(key) != 32:
        raise ValueError('KEY_LENGTH_INVALID')
    if page_size not in (1024, 2048, 4096, 8192, 16384, 32768, 65536) or iterations < 1:
        raise ValueError('CIPHER_PARAMETERS_INVALID')
    size = source.stat().st_size
    if size < page_size or size % page_size:
        raise ValueError('CIPHER_SIZE_INVALID')
    with source.open('rb') as inp:
        salt = inp.read(16)
        if salt == b'SQLite format 3\0':
            raise ValueError('ALREADY_PLAINTEXT: use read_chats.py')
        aes = bytearray(key if raw_key else hashlib.pbkdf2_hmac('sha512', key, salt, iterations, 32))
        mac = bytearray(hashlib.pbkdf2_hmac('sha512', aes, bytes(x ^ 0x3A for x in salt), 2, 32))
        try:
            with output_file(output) as temp:
                inp.seek(0)
                with temp.open('wb') as out:
                    for number in range(1, size // page_size + 1):
                        page = inp.read(page_size)
                        start, end = (16 if number == 1 else 0), page_size - 80
                        if len(page) != page_size:
                            raise ValueError('SOURCE_CHANGED')
                        expected = hmac.new(mac, page[start:end+16] + number.to_bytes(4, 'little'), hashlib.sha512).digest()
                        if not hmac.compare_digest(expected, page[end+16:]):
                            raise ValueError('HMAC_FAILED: wrong key, parameters, corruption or incompatible format')
                        clear = AES.new(aes, AES.MODE_CBC, page[end:end+16]).decrypt(page[start:end])
                        out.write((b'SQLite format 3\0' if number == 1 else b'') + clear + bytes(80))
                    if inp.read(1):
                        raise ValueError('SOURCE_CHANGED')
                with readonly(temp) as db:
                    validate(db)
        finally:
            wipe(aes)
            wipe(mac)
    return {'pages': size // page_size, 'integrity': 'ok', 'profile': 'sqlcipher4-sha512'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source', type=Path, required=True, help='Offline, stable database copy')
    p.add_argument('--output', type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--protected-key', '--dpapi-key', type=Path, help='Current Windows user DPAPI blob')
    g.add_argument('--prompt-key', action='store_true', help='Local hidden prompt; never send keys in chat')
    p.add_argument('--page-size', type=int, default=4096)
    p.add_argument('--iterations', type=int, default=256000)
    p.add_argument('--raw-key', action='store_true', help='Explicit AES-key mode; not the default passphrase mode')
    args = p.parse_args()
    key = key_input(args)
    try:
        return decrypt(args.source, args.output, key, args.page_size, args.iterations, args.raw_key)
    finally:
        wipe(key)


if __name__ == '__main__':
    run(main)
