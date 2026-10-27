"""Optional Windows adapter for a user-supplied, trusted wx_key-compatible DLL.

This changes the selected process (hook); it is never invoked by inventory or export.
No third-party binary or key extraction implementation is distributed here.
"""
import argparse
import ctypes
import hashlib
import os
import struct
import time
from pathlib import Path

from common import output_file, run
from decrypt_sqlcipher import decrypt, wipe


def protect(key, output):
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    keybuf = (ctypes.c_ubyte * len(key)).from_buffer(key)
    source, target = Blob(len(key), keybuf), Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not crypt.CryptProtectData(ctypes.byref(source), 'Local database credential', None, None, None, 1, ctypes.byref(target)):
        raise ValueError('DPAPI_PROTECT_FAILED')
    try:
        with output_file(output) as temp:
            temp.write_bytes(ctypes.string_at(target.data, target.size))
    finally:
        kernel.LocalFree(target.data)


def capture(dll_path, expected_hash, pid, seconds, output, database, page_size, iterations, raw_key):
    if os.name != 'nt':
        raise ValueError('CAPTURE_REQUIRES_WINDOWS')
    if seconds < 1 or seconds > 120 or pid <= 0:
        raise ValueError('CAPTURE_ARGUMENTS_INVALID')
    dll_path = Path(dll_path).resolve(strict=True)
    with dll_path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual.lower() != expected_hash.lower():
        raise ValueError('DLL_HASH_MISMATCH')
    if Path(output).exists():
        raise ValueError('OUTPUT_EXISTS')
    database = Path(database).resolve(strict=True)
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(database)+suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('UNMERGED_JOURNAL')
    if Path(output).resolve() == database:
        raise ValueError('SOURCE_OUTPUT_CONFLICT')
    with dll_path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise ValueError('DLL_PE_INVALID')
        stream.seek(60)
        offset_bytes = stream.read(4)
        if len(offset_bytes) != 4:
            raise ValueError('DLL_PE_INVALID')
        stream.seek(struct.unpack('<I', offset_bytes)[0])
        header = stream.read(6)
        if len(header) != 6 or header[:4] != b'PE\0\0':
            raise ValueError('DLL_PE_INVALID')
        machine = struct.unpack('<H', header[4:])[0]
        if machine not in (0x14C, 0x8664) or (machine == 0x14C) != (ctypes.sizeof(ctypes.c_void_p) == 4):
            raise ValueError('DLL_ARCHITECTURE_MISMATCH')
    # Explicit PID only: never choose the biggest/first process or guess an account.
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    process = kernel.OpenProcess(0x1000, False, pid)
    if not process:
        raise ValueError('PROCESS_NOT_ACCESSIBLE')
    try:
        buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
        if not kernel.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size)) or Path(buf.value).name.lower() != 'weixin.exe':
            raise ValueError('PROCESS_NOT_WEIXIN')
        if not hasattr(kernel, 'IsWow64Process2'):
            raise ValueError('PROCESS_ARCHITECTURE_CHECK_UNAVAILABLE')
        process_machine, native_machine = wintypes.WORD(), wintypes.WORD()
        kernel.IsWow64Process2.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.WORD)]
        kernel.IsWow64Process2.restype = wintypes.BOOL
        if not kernel.IsWow64Process2(process, ctypes.byref(process_machine), ctypes.byref(native_machine)):
            raise ValueError('PROCESS_ARCHITECTURE_CHECK_FAILED')
        if (process_machine.value or native_machine.value) != machine:
            raise ValueError('PROCESS_ARCHITECTURE_MISMATCH')
    finally:
        kernel.CloseHandle(process)
    with os.add_dll_directory(str(dll_path.parent)):
        dll = ctypes.WinDLL(str(dll_path))
    # ABI matches the inspected local tool; other DLL ABIs are not supported.
    dll.InitializeHook.argtypes, dll.InitializeHook.restype = [ctypes.c_uint32], ctypes.c_int
    dll.PollKeyData.argtypes, dll.PollKeyData.restype = [ctypes.c_void_p, ctypes.c_size_t], ctypes.c_int
    dll.CleanupHook.argtypes, dll.CleanupHook.restype = [], ctypes.c_int
    keybuf = ctypes.create_string_buffer(65)
    key = bytearray()
    try:
        if not dll.InitializeHook(pid):
            raise ValueError('HOOK_INITIALIZATION_FAILED')
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if dll.PollKeyData(keybuf, len(keybuf)):
                key = bytearray.fromhex(keybuf.value.decode('ascii'))
                if len(key) != 32:
                    raise ValueError('KEY_LENGTH_INVALID')
                # Verify ALL pages and SQLite integrity using an offline sample.
                import tempfile
                with tempfile.TemporaryDirectory(prefix='wechat-key-check-') as d:
                    decrypt(database, Path(d)/'verified.db', key, page_size, iterations, raw_key)
                protect(key, output)
                return {'key_verified': True, 'stored': 'current_user_dpapi', 'hook_cleanup_attempted': True}
            time.sleep(0.25)
        raise ValueError('KEY_NOT_RECEIVED')
    finally:
        wipe(key)
        ctypes.memset(keybuf, 0, len(keybuf))
        dll.CleanupHook()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--dll', type=Path, required=True)
    p.add_argument('--dll-sha256', required=True, help='Verified by user; a hash alone does not establish trust')
    p.add_argument('--pid', type=int, required=True)
    p.add_argument('--database', type=Path, required=True, help='Offline encrypted copy used to verify captured key')
    p.add_argument('--output', type=Path, required=True, help='New .dpapi path, never a plaintext key')
    p.add_argument('--wait-seconds', type=int, default=30)
    p.add_argument('--page-size', type=int, default=4096)
    p.add_argument('--iterations', type=int, default=256000)
    p.add_argument('--raw-key', action='store_true')
    p.add_argument('--acknowledge-process-hook', action='store_true', required=True,
                   help='User explicitly approves hooking selected own-account process with trusted DLL')
    a = p.parse_args()
    return capture(a.dll, a.dll_sha256, a.pid, a.wait_seconds, a.output, a.database, a.page_size, a.iterations, a.raw_key)


if __name__ == '__main__':
    run(main)
