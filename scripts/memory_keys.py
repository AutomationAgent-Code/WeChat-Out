"""Bounded read-only WCDB key scanner; no injection, debugger or memory dumps.

Config.Cipher layout/mask adapted from TANGandXUE/wcdb-key-tool revision
79f1b5b92e12c66aa281b4a60a3c478b5f547dfa (MIT).
Copyright (c) 2025 CloudDreamAI / TANGandXUE.
See third_party/wcdb-key-tool.LICENSE and NOTICE.md.
"""
import ctypes
import hashlib
import hmac
import os
import re
import struct
import time

NAME = b'com.Tencent.WCDB.Config.Cipher'
MASK = bytes.fromhex('d2c7442458020000004889442450488b450048844c2448488944254048584c24')
LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
MAX_ADDRESS = 0x0000800000000000


def verify_raw_key(key, page):
    if len(key) != 32 or len(page) != 4096:
        return False
    mac = hashlib.pbkdf2_hmac('sha512', key, bytes(x ^ 0x3A for x in page[:16]), 2, 32)
    expected = hmac.new(mac, page[16:4032]+struct.pack('<I', 1), hashlib.sha512).digest()
    return hmac.compare_digest(expected, page[4032:])


def literal_candidates(data):
    for match in LITERAL.finditer(data):
        run = match.group(1)
        # Recognize bounded key/salt fields, not arbitrary sliding 32-byte memory.
        starts = [0] if len(run) <= 96 else sorted({*range(0, len(run)-63, 32), len(run)-64})
        for start in starts:
            key = bytes.fromhex(run[start:start+64].decode('ascii'))
            salt = bytes.fromhex(run[start+64:start+96].decode('ascii')) if start+96 <= len(run) else None
            yield key, salt


def config_candidates(blob):
    if 0 < len(blob) <= 1024:
        decoded = bytes(value ^ MASK[i % len(MASK)] for i, value in enumerate(blob))
        yield from literal_candidates(decoded)


class Budget:
    def __init__(self, seconds=60, max_bytes=4*1024**3, max_candidates=20000):
        if not 1 <= seconds <= 300 or not 1024 <= max_bytes <= 32*1024**3 or not 1 <= max_candidates <= 100000:
            raise ValueError('SCAN_BUDGET_INVALID')
        self.deadline = time.monotonic()+seconds
        self.max_bytes, self.max_candidates = max_bytes, max_candidates
        self.bytes = self.candidates = 0

    def check(self, size=0, candidate=False):
        if time.monotonic() >= self.deadline:
            raise ValueError('SCAN_TIMEOUT')
        self.bytes += size
        self.candidates += int(candidate)
        if self.bytes > self.max_bytes:
            raise ValueError('SCAN_BYTE_LIMIT')
        if self.candidates > self.max_candidates:
            raise ValueError('SCAN_CANDIDATE_LIMIT')


def chunks(regions, read, budget, chunk_size=1024*1024, overlap=256):
    tail, last_end = b'', None
    for base, size in regions:
        if last_end != base:
            tail = b''
        for offset in range(0, size, chunk_size):
            address = base+offset
            wanted = min(chunk_size, size-offset)
            budget.check(wanted)
            data = read(address, wanted)
            if len(data) != wanted:
                # Never glue bytes across an unreadable hole.
                tail, last_end = b'', None
                continue
            merged = tail+data
            yield address-len(tail), merged
            tail = merged[-overlap:] if overlap else b''
            last_end = address+wanted


def scan_memory(regions, read, pages, budget=None):
    """Pure scanner with injectable memory transport; returns ONLY HMAC-valid keys.

    pages maps salt -> list[first-page bytes] from the selected account only.
    Caller must wipe returned bytearrays and never log the result.
    """
    budget = budget or Budget()
    # Two passes must see the same regions, including when transport yields them.
    saved_regions = []
    for region in regions:
        budget.check()
        if len(saved_regions) >= 100000:
            raise ValueError('SCAN_REGION_LIMIT')
        saved_regions.append(region)
    regions = saved_regions
    found, needles, tested = {}, set(), set()

    def consider(key, salt):
        fingerprint = hashlib.sha256(key+(salt or b'')).digest()
        if fingerprint in tested:
            return
        tested.add(fingerprint)
        budget.check(candidate=True)
        targets = [salt] if salt in pages else pages
        for target in targets:
            if target not in found and all(verify_raw_key(key, p) for p in pages[target]):
                found[target] = bytearray(key)

    try:
        for base, data in chunks(regions, read, budget):
            for key, salt in literal_candidates(data):
                consider(key, salt)
            pos = data.find(NAME)
            while pos >= 0:
                needles.add(base+pos)
                if len(needles) > 512:
                    raise ValueError('SCAN_REFERENCE_LIMIT')
                pos = data.find(NAME, pos+1)
            if len(found) == len(pages):
                return found
        if not needles:
            return found
        patterns = [struct.pack('<QQ', ptr, len(NAME)) for ptr in needles]
        seen_configs = set()

        def exact(addr, size):
            if not 0x10000 <= addr < MAX_ADDRESS or addr+size >= MAX_ADDRESS:
                return b''
            budget.check(size)
            value = read(addr, size)
            return value if len(value) == size else b''

        for base, data in chunks(regions, read, budget):
            for pattern in patterns:
                pos = data.find(pattern)
                while pos >= 0:
                    budget.check()
                    node = exact(base+pos-0x10, 0x50)
                    if node and node[0x10:0x20] == pattern:
                        config = struct.unpack_from('<Q', node, 0x28)[0]
                        if config not in seen_configs:
                            seen_configs.add(config)
                            if len(seen_configs) > 10000:
                                raise ValueError('SCAN_REFERENCE_LIMIT')
                            obj = exact(config+0x88, 0x28)
                            if obj:
                                ptr, size = struct.unpack_from('<QQ', obj, 8)
                                if 0 < size <= 1024:
                                    blob = exact(ptr, size)
                                    for key, salt in config_candidates(blob):
                                        consider(key, salt)
                    if len(found) == len(pages):
                        return found
                    pos = data.find(pattern, pos+1)
        return found
    except BaseException:
        for key in found.values():
            key[:] = bytes(len(key))
        raise


def own_weixin_processes():
    import psutil
    owner = psutil.Process().username().casefold()
    result = []
    for process in psutil.process_iter(['pid', 'name']):
        if (process.info['name'] or '').casefold() != 'weixin.exe':
            continue
        try:
            if process.username().casefold() != owner:
                continue
            if any(arg.startswith('--type=') for arg in process.cmdline()):
                continue
            result.append(process.pid)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return sorted(result)


class WindowsMemory:
    """Windows AMD64 ReadProcessMemory transport, bound to one same-user Weixin PID."""
    def __init__(self, pid):
        if os.name != 'nt' or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise ValueError('WINDOWS_X64_PYTHON_REQUIRED')
        if pid not in own_weixin_processes():
            raise ValueError('PID_NOT_OWN_MAIN_WEIXIN')
        from ctypes import wintypes as w
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL
        self.api.ReadProcessMemory.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.api.ReadProcessMemory.restype = w.BOOL
        self.api.VirtualQueryEx.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
        self.api.VirtualQueryEx.restype = ctypes.c_size_t
        self.handle = self.api.OpenProcess(0x0010 | 0x0400, False, pid)
        if not self.handle:
            raise ValueError('PROCESS_READ_DENIED: no automatic elevation')
        try:
            # Recheck the actual opened handle to reduce PID reuse ambiguity.
            self.api.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
            self.api.QueryFullProcessImageNameW.restype = w.BOOL
            name, size = ctypes.create_unicode_buffer(32768), w.DWORD(32768)
            from pathlib import Path
            if not self.api.QueryFullProcessImageNameW(self.handle, 0, name, ctypes.byref(size)) or Path(name.value).name.casefold() != 'weixin.exe':
                raise ValueError('PID_NOT_OWN_MAIN_WEIXIN')
            if not hasattr(self.api, 'IsWow64Process2'):
                raise ValueError('PROCESS_ARCHITECTURE_CHECK_UNAVAILABLE')
            self.api.IsWow64Process2.argtypes = [w.HANDLE, ctypes.POINTER(w.WORD), ctypes.POINTER(w.WORD)]
            self.api.IsWow64Process2.restype = w.BOOL
            pm, nm = w.WORD(), w.WORD()
            if not self.api.IsWow64Process2(self.handle, ctypes.byref(pm), ctypes.byref(nm)) or (pm.value or nm.value) != 0x8664:
                raise ValueError('WEIXIN_AMD64_REQUIRED')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None

    def read(self, address, size):
        buf, n = ctypes.create_string_buffer(size), ctypes.c_size_t()
        try:
            if self.api.ReadProcessMemory(self.handle, address, buf, size, ctypes.byref(n)):
                return buf.raw[:n.value]
            return b''
        finally:
            ctypes.memset(buf, 0, size)

    def regions(self, budget):
        from ctypes import wintypes as w
        class MBI(ctypes.Structure):
            _fields_ = [('base', ctypes.c_uint64), ('allocation', ctypes.c_uint64),
                        ('allocation_protect', w.DWORD), ('pad', w.DWORD),
                        ('size', ctypes.c_uint64), ('state', w.DWORD), ('protect', w.DWORD),
                        ('kind', w.DWORD), ('pad2', w.DWORD)]
        address, result = 0, []
        info = MBI()
        while address < MAX_ADDRESS:
            budget.check()
            if not self.api.VirtualQueryEx(self.handle, address, ctypes.byref(info), ctypes.sizeof(info)):
                break
            if info.state == 0x1000 and not info.protect & 0x100 and (info.protect & 0xFF) in (2, 4, 8, 32, 64, 128):
                result.append((info.base, info.size))
            next_address = info.base+info.size
            if next_address <= address:
                raise ValueError('INVALID_MEMORY_REGION')
            address = next_address
        return result
