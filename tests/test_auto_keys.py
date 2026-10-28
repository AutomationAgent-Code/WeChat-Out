"""DLL-free runtime scanner tests with synthetic memory and synthetic databases."""
import ctypes
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from common import readonly, validate
from memory_keys import NAME, MASK, Budget, WindowsMemory, chunks, scan_memory, verify_raw_key
from wechat import (account_root, capture_account, choose_account, choose_pid,
                    discover_accounts, export_account, load_keyring)


def encrypt_sqlite(source, target, key, salt):
    """Convert tiny synthetic leaf-only SQLite fixtures to reserved80 encrypted pages."""
    from Crypto.Cipher import AES
    data = bytearray(Path(source).read_bytes())
    assert int.from_bytes(data[16:18], 'big') == 4096
    for offset in range(0, len(data), 4096):
        page = bytearray(data[offset:offset+4096])
        header = 100 if offset == 0 else 0
        assert page[header] == 13, 'Only fixture leaf pages supported'
        assert page[header+1:header+3] == b'\0\0'
        cells = int.from_bytes(page[header+3:header+5], 'big')
        start = int.from_bytes(page[header+5:header+7], 'big')
        assert start >= header+8+cells*2+80
        page[start-80:4016] = page[start:4096]
        page[4016:] = bytes(80)
        page[header+5:header+7] = (start-80).to_bytes(2, 'big')
        for cell in range(cells):
            pointer = header+8+2*cell
            old = int.from_bytes(page[pointer:pointer+2], 'big')
            page[pointer:pointer+2] = (old-80).to_bytes(2, 'big')
        if offset == 0:
            page[20] = 80
        data[offset:offset+4096] = page
    # Validate the synthetic source transformation independently with SQLite.
    reserved = Path(source).with_suffix('.reserved')
    reserved.write_bytes(data)
    with readonly(reserved) as db:
        validate(db)
    mac = hashlib.pbkdf2_hmac('sha512', key, bytes(x^0x3A for x in salt), 2, 32)
    encrypted = bytearray()
    for number, offset in enumerate(range(0, len(data), 4096), 1):
        page = data[offset:offset+4096]
        iv = bytes([number % 256])*16
        cipher = AES.new(key, AES.MODE_CBC, iv).encrypt(bytes(page[16 if number == 1 else 0:4016]))
        tag = hmac.new(mac, cipher+iv+struct.pack('<I', number), hashlib.sha512).digest()
        encrypted.extend((salt if number == 1 else b'')+cipher+iv+tag)
    target.write_bytes(encrypted)
    return bytes(encrypted[:4096])


class FakeMemory:
    def __init__(self):
        self.data = bytearray(16384)
        self.base = 0x10000
        self.closed = False

    def read(self, address, size):
        start = address-self.base
        if start < 0 or start+size > len(self.data):
            return b''
        return bytes(self.data[start:start+size])

    def regions(self, budget):
        return [(self.base, len(self.data))]

    def close(self):
        self.closed = True

    def put_config(self, key, salt, index=0):
        name_at, node, config, blob_at = [n+index*1024 for n in (128, 256, 512, 768)]
        self.data[name_at:name_at+len(NAME)] = NAME
        struct.pack_into('<QQ', self.data, node+0x10, self.base+name_at, len(NAME))
        struct.pack_into('<Q', self.data, node+0x28, self.base+config)
        literal = b"x'"+key.hex().encode()+salt.hex().encode()+b"'"
        blob = bytes(x^MASK[i % len(MASK)] for i, x in enumerate(literal))
        struct.pack_into('<QQ', self.data, config+0x90, self.base+blob_at, len(blob))
        self.data[blob_at:blob_at+len(blob)] = blob


class AutoKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.account = self.root/'xwechat_files'/'synthetic-account'/'db_storage'
        (self.account/'contact').mkdir(parents=True)
        (self.account/'message').mkdir()
        self.contact = self.account/'contact'/'contact.db'
        self.message = self.account/'message'/'message_0.db'
        for path, statements in (
            (self.contact, ["CREATE TABLE contact(username TEXT, remark TEXT)", "INSERT INTO contact VALUES('synthetic-peer','测试')"]),
            (self.message, ["CREATE TABLE Name2Id(user_name TEXT)", "INSERT INTO Name2Id VALUES('synthetic-self')",
                'CREATE TABLE "Msg_'+hashlib.md5(b'synthetic-peer').hexdigest()+'"(local_id INTEGER, server_id INTEGER, local_type INTEGER, create_time INTEGER, real_sender_id INTEGER, message_content TEXT)',
                'INSERT INTO "Msg_'+hashlib.md5(b'synthetic-peer').hexdigest()+'" VALUES(1,101,1,1700000000,1,\'PRIVATE_TEST_MESSAGE\')'])):
            db = sqlite3.connect(path)
            try:
                for sql in statements:
                    db.execute(sql)
                db.commit()
            finally:
                db.close()
        self.keys = [hashlib.sha256(b'synthetic-key-'+bytes([i])).digest() for i in range(2)]
        self.salts = [bytes([i+1])*16 for i in range(2)]

    def tearDown(self):
        self.temp.cleanup()

    def encrypt_account(self):
        pages = {}
        for path, key, salt in zip([self.contact, self.message], self.keys, self.salts):
            encrypted = path.with_suffix('.cipher')
            page = encrypt_sqlite(path, encrypted, key, salt)
            os.replace(encrypted, path)  # Only synthetic test fixtures.
            pages[salt] = [page]
        return pages

    def test_config_scans_all_account_salts(self):
        pages = self.encrypt_account()
        mem = FakeMemory()
        for i in range(2):
            mem.put_config(self.keys[i], self.salts[i], i)
        keys = scan_memory(mem.regions(None), mem.read, pages)
        self.assertEqual(keys, dict(zip(self.salts, self.keys)))

    def test_config_with_region_iterator(self):
        pages = self.encrypt_account()
        mem = FakeMemory()
        for i in range(2):
            mem.put_config(self.keys[i], self.salts[i], i)
        keys = scan_memory(iter(mem.regions(None)), mem.read, pages)
        self.assertEqual(keys, dict(zip(self.salts, self.keys)))

    def test_nested_uppercase_shards_all_exported(self):
        for relative in ('MESSAGE_1.DB', 'archive/message_2.db'):
            target = self.message.parent/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.message.read_bytes())
            db = sqlite3.connect(target)
            try:
                table = 'Msg_'+hashlib.md5(b'synthetic-peer').hexdigest()
                db.execute('UPDATE "'+table+'" SET server_id=?', (102 if relative.startswith('MESSAGE') else 103,))
                db.commit()
            finally:
                db.close()
        with patch('wechat.own_weixin_processes', return_value=[]):
            result = export_account(self.account, self.root/'output', source_closed=True)
        self.assertEqual(result['shards'], 3)
        self.assertEqual(result['messages'], 3)

    def test_legacy_literal_and_wrong_candidates(self):
        pages = self.encrypt_account()
        mem = FakeMemory()
        content = b"x'"+(b'0'*64)+self.salts[0].hex().encode()+b"'"
        content += b"X'"+self.keys[0].hex().encode()+self.salts[0].hex().encode()+b"'"
        mem.data[100:100+len(content)] = content
        self.assertEqual(scan_memory(mem.regions(None), mem.read, pages), {self.salts[0]: self.keys[0]})

    def test_unknown_mask_does_not_publish_false_key(self):
        pages = self.encrypt_account()
        mem = FakeMemory()
        mem.put_config(self.keys[0], self.salts[0])
        mem.data[768] ^= 0xFF
        self.assertEqual(scan_memory(mem.regions(None), mem.read, pages), {})

    def test_chunk_boundaries_and_unreadable_gap(self):
        mem = FakeMemory()
        needle = b'boundary'
        mem.data[1020:1020+len(needle)] = needle
        records = list(chunks(mem.regions(None), mem.read, Budget(), chunk_size=1024, overlap=16))
        self.assertTrue(any(needle in data for _, data in records))
        def read(address, size):
            return b'' if address == mem.base+1024 else mem.read(address, size)
        records = list(chunks(mem.regions(None), read, Budget(), chunk_size=1024, overlap=16))
        after = next(data for base, data in records if base == mem.base+2048)
        self.assertEqual(len(after), 1024)

    def test_budgets(self):
        mem = FakeMemory()
        with self.assertRaisesRegex(ValueError, 'SCAN_BYTE_LIMIT'):
            list(chunks(mem.regions(None), mem.read, Budget(max_bytes=1024)))
        budget = Budget()
        budget.deadline = 0
        with self.assertRaisesRegex(ValueError, 'SCAN_TIMEOUT'):
            budget.check()

    @unittest.skipUnless(os.name == 'nt' and ctypes.sizeof(ctypes.c_void_p) == 8, 'Windows x64 transport')
    def test_native_transport_requests_only_read_query_rights(self):
        from ctypes import wintypes as w
        class Function:
            def __init__(self, f):
                self.f = f
            def __call__(self, *args):
                return self.f(*args)
        calls = []
        class API:
            pass
        api = API()
        def opened(rights, inherit, pid):
            calls.append(('open', rights, pid))
            return 123
        def image(handle, flags, buf, length):
            buf.value = 'C:/synthetic/Weixin.exe'
            return 1
        def architecture(handle, process, native):
            ctypes.cast(process, ctypes.POINTER(w.WORD)).contents.value = 0
            ctypes.cast(native, ctypes.POINTER(w.WORD)).contents.value = 0x8664
            return 1
        def read(handle, address, buf, size, count):
            ctypes.memmove(buf, b'synthetic', size)
            ctypes.cast(count, ctypes.POINTER(ctypes.c_size_t)).contents.value = size
            return 1
        api.OpenProcess = Function(opened)
        api.CloseHandle = Function(lambda handle: calls.append(('close', handle)) or 1)
        api.QueryFullProcessImageNameW = Function(image)
        api.IsWow64Process2 = Function(architecture)
        api.ReadProcessMemory = Function(read)
        api.VirtualQueryEx = Function(lambda *args: 0)
        with patch('memory_keys.own_weixin_processes', return_value=[123]), patch('ctypes.WinDLL', return_value=api):
            memory = WindowsMemory(123)
            self.assertEqual(memory.read(0x10000, 9), b'synthetic')
            memory.close()
        self.assertEqual(calls, [('open', 0x0410, 123), ('close', 123)])

    @unittest.skipUnless(os.name == 'nt', 'Windows transport')
    def test_foreign_pid_rejected_before_open(self):
        with patch('memory_keys.own_weixin_processes', return_value=[]), patch('ctypes.WinDLL') as api:
            with self.assertRaisesRegex(ValueError, 'PID_NOT_OWN_MAIN_WEIXIN'):
                WindowsMemory(123)
            api.assert_not_called()

    def test_multiple_accounts_never_pick_first(self):
        with patch('wechat.discover_accounts', return_value=[self.account, self.root/'other']):
            with self.assertRaisesRegex(ValueError, 'ACCOUNT_SELECTION_REQUIRED'):
                choose_account()
        with patch('wechat.own_weixin_processes', return_value=[10, 20]):
            with self.assertRaisesRegex(ValueError, 'PID_SELECTION_REQUIRED'):
                choose_pid()
            self.assertEqual(choose_pid(20), 20)

    def test_discovery_uses_selected_roots_only(self):
        self.assertEqual(discover_accounts([self.root]), [self.account.resolve()])

    def test_bad_hmac_not_accepted(self):
        pages = self.encrypt_account()
        page = next(iter(pages.values()))[0]
        self.assertFalse(verify_raw_key(b'wrong'*6+b'xx', page))
        self.assertFalse(verify_raw_key(self.keys[0], page[:-1]))

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_capture_and_export_end_to_end_without_dll(self):
        self.encrypt_account()
        mem = FakeMemory()
        for i in range(2):
            mem.put_config(self.keys[i], self.salts[i], i)
        credential = self.root/'private'/'keyring.dpapi'
        stats = capture_account(self.account, 123, credential, memory_factory=lambda pid: mem)
        self.assertTrue(stats['keyring_saved'])
        self.assertTrue(mem.closed)
        self.assertNotIn(self.keys[0], credential.read_bytes())
        with patch('wechat.own_weixin_processes', return_value=[]):
            result = export_account(self.account, self.root/'output', credential, True, 'synthetic-self')
        self.assertEqual(result['messages'], 1)
        record = json.loads((self.root/'output'/'chats.jsonl').read_text(encoding='utf-8'))
        self.assertEqual(record['content'], 'PRIVATE_TEST_MESSAGE')
        self.assertEqual(record['direction'], 'sent')

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_partial_capture_no_credential(self):
        self.encrypt_account()
        mem = FakeMemory()
        mem.put_config(self.keys[0], self.salts[0])
        output = self.root/'keyring.dpapi'
        with self.assertRaisesRegex(ValueError, 'KEYS_INCOMPLETE'):
            capture_account(self.account, 123, output, memory_factory=lambda pid: mem)
        self.assertFalse(output.exists())
        self.assertTrue(mem.closed)

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_keyring_binding(self):
        self.encrypt_account()
        mem = FakeMemory()
        for i in range(2):
            mem.put_config(self.keys[i], self.salts[i], i)
        output = self.root/'keyring.dpapi'
        capture_account(self.account, 123, output, memory_factory=lambda pid: mem)
        with patch('wechat.account_binding', return_value='different'):
            with self.assertRaisesRegex(ValueError, 'KEYRING_ACCOUNT_MISMATCH'):
                load_keyring(output, self.account)

    def test_export_must_be_closed(self):
        with self.assertRaisesRegex(ValueError, 'SOURCE_MUST_BE_CLOSED'):
            export_account(self.account, self.root/'out')

    def test_still_running_never_terminated(self):
        if os.name != 'nt':
            self.skipTest('Windows process check')
        with patch('wechat.own_weixin_processes', return_value=[123]):
            with self.assertRaisesRegex(ValueError, 'WEIXIN_STILL_RUNNING'):
                export_account(self.account, self.root/'out', source_closed=True)
        self.assertFalse((self.root/'out').exists())


if __name__ == '__main__':
    unittest.main()
