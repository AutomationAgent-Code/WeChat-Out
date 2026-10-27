"""Synthetic-only tests; never search for or open real WeChat data."""
import hashlib
import hmac
import json
import os
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from common import output_file, readonly
from decrypt_sqlcipher import decrypt
from inspect_databases import inspect
from read_chats import content, export
from snapshot_database import snapshot
from capture_key import capture, protect
from decrypt_sqlcipher import unprotect


@contextmanager
def writable(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.contact = self.root / 'contact.db'
        self.peer = 'synthetic-peer'
        self.table = 'Msg_' + hashlib.md5(self.peer.encode()).hexdigest()
        with writable(self.contact) as db:
            db.execute('CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT)')
            db.execute('INSERT INTO contact VALUES(?,?,?)', (self.peer, '测试联系人', 'Test'))
        self.messages = self.root / 'messages'
        self.messages.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def shard(self, index, rows):
        path = self.messages / ('message_' + str(index) + '.db')
        with writable(path) as db:
            db.execute('CREATE TABLE Name2Id(user_name TEXT)')
            db.executemany('INSERT INTO Name2Id VALUES(?)', [('synthetic-self',), (self.peer,)])
            db.execute('CREATE TABLE "' + self.table + '"(local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content)')
            db.executemany('INSERT INTO "' + self.table + '" VALUES(?,?,?,?,?,?)', rows)
        return path

    def test_multishard_dedup_and_sender(self):
        a = (2, 101, 1, 1700000000, 1, '你好')
        b = (10, 102, 1, 1700000000, 2, '回复')
        self.shard(0, [b, a])
        self.shard(1, [a, (3, 103, 49, 1700000001000, 9, b'\xff\xfe')])
        out = self.root/'out.jsonl'
        stats = export(self.contact, self.messages, out, 'synthetic-self')
        records = [json.loads(x) for x in out.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(stats['messages'], 3)
        self.assertEqual(stats['duplicates_removed'], 1)
        self.assertEqual([r['local_id'] for r in records[:2]], [2, 10])
        self.assertEqual(records[0]['direction'], 'sent')
        self.assertEqual(records[1]['direction'], 'received')
        self.assertEqual(records[2]['direction'], 'unknown')
        self.assertEqual(records[2]['content'], None)
        self.assertEqual(records[2]['binary_base64'], '//4=')
        self.assertEqual(stats['max_timestamp_seconds'], 1700000001)

    def test_conflicting_duplicate_does_not_publish(self):
        self.shard(0, [(1, 101, 1, 1700000000, 1, 'one')])
        self.shard(1, [(1, 101, 1, 1700000000, 1, 'different')])
        out = self.root/'out.jsonl'
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_MESSAGE_CONFLICT'):
            export(self.contact, self.messages, out)
        self.assertFalse(out.exists())
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_zero_server_ids_not_collapsed(self):
        self.shard(0, [(1, 0, 1, 1700000000, 1, 'a')])
        self.shard(1, [(1, 0, 1, 1700000000, 1, 'a')])
        self.assertEqual(export(self.contact, self.messages, self.root/'x.jsonl')['messages'], 2)

    def test_schema_failure_no_partial_result(self):
        path = self.shard(0, [(1, 0, 1, 1, 1, 'a')])
        with writable(path) as db:
            db.execute('CREATE TABLE Msg_unknown(incorrect TEXT)')
        out = self.root/'out.jsonl'
        with self.assertRaisesRegex(ValueError, 'UNSUPPORTED_MESSAGE_SCHEMA'):
            export(self.contact, self.messages, out)
        self.assertFalse(out.exists())

    def test_sources_unchanged_and_no_clobber(self):
        path = self.shard(0, [(1, 1, 1, 1, 1, 'text')])
        before = path.read_bytes()
        out = self.root/'out.jsonl'
        export(self.contact, self.messages, out)
        prior = out.read_bytes()
        with self.assertRaises(FileExistsError):
            export(self.contact, self.messages, out)
        self.assertEqual(prior, out.read_bytes())
        self.assertEqual(before, path.read_bytes())

    def test_missing_db_not_created(self):
        missing = self.root/'absent.db'
        with self.assertRaises(FileNotFoundError):
            with readonly(missing):
                pass
        self.assertFalse(missing.exists())

    def test_filter(self):
        self.shard(0, [(1, 1, 1, 1, 1, 'text')])
        self.assertEqual(export(self.contact, self.messages, self.root/'x.jsonl', conversation='absent')['messages'], 0)

    def test_missing_contact_still_matches_conversation(self):
        self.shard(0, [(1, 1, 1, 1, 2, 'text')])
        with writable(self.contact) as db:
            db.execute('DELETE FROM contact')
        stats = export(self.contact, self.messages, self.root/'x.jsonl', conversation=self.peer)
        self.assertEqual(stats['messages'], 1)
        self.assertEqual(stats['selected_tables'], 1)

    def test_unknown_peer_still_exported(self):
        path = self.shard(0, [(1, 1, 1, 1, 1, 'text')])
        with writable(self.contact) as db:
            db.execute('DELETE FROM contact')
        with writable(path) as db:
            db.execute('DELETE FROM Name2Id WHERE user_name=?', (self.peer,))
        out = self.root/'x.jsonl'
        self.assertEqual(export(self.contact, self.messages, out)['messages'], 1)
        self.assertIsNone(json.loads(out.read_text(encoding='utf-8'))['conversation'])

    def test_zstd_and_raw_binary(self):
        import zstandard
        compressed = zstandard.ZstdCompressor().compress('压缩消息'.encode())
        self.assertEqual(content(compressed)[:2], ('压缩消息', 'zstd_text'))
        self.assertEqual(content(b'plain')[:2], ('plain', 'utf8_bytes'))

    def test_zstd_multiframe_and_trailing_junk(self):
        import zstandard
        compress = zstandard.ZstdCompressor().compress
        self.assertEqual(content(compress(b'FIRST')+compress(b'SECOND'))[:2], ('FIRSTSECOND', 'zstd_text'))
        payload = compress(b'FIRST')+b'INVALID_TAIL'
        text, status, raw = content(payload)
        self.assertIsNone(text)
        self.assertEqual(status, 'binary_or_decode_failed')
        import base64
        self.assertEqual(base64.b64decode(raw), payload)

    def test_truncated_zstd_preserves_original(self):
        import zstandard
        import base64
        compress = zstandard.ZstdCompressor(write_checksum=True).compress
        for payload in (compress(b'FIRST')[:-1], compress(b'FIRST')+compress(b'SECOND')[:-6]):
            text, status, raw = content(payload)
            self.assertIsNone(text)
            self.assertEqual(status, 'binary_or_decode_failed')
            self.assertEqual(base64.b64decode(raw), payload)

    def test_oversized_zstd_preserves_original(self):
        import zstandard
        import base64
        payload = zstandard.ZstdCompressor().compress(b'x'*(16*1024*1024+1))
        text, status, raw = content(payload)
        self.assertIsNone(text)
        self.assertEqual(base64.b64decode(raw), payload)

    def test_plaintext_backup_includes_live_wal(self):
        source = self.root/'live.db'
        db = sqlite3.connect(source)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE sample(value TEXT)')
            db.execute("INSERT INTO sample VALUES('committed-in-wal')")
            db.commit()
            target = self.root/'copy.db'
            snapshot(source, target)
            with readonly(target) as copied:
                self.assertEqual(copied.execute('SELECT value FROM sample').fetchone()[0], 'committed-in-wal')
        finally:
            db.close()

    @unittest.skipUnless(os.name == 'nt', 'DPAPI is Windows-only')
    def test_dpapi_synthetic_credential_roundtrip(self):
        key = bytearray(b'not-real-key-material-synthetic!!')
        output = self.root/'synthetic.dpapi'
        protect(key, output)
        self.assertNotIn(bytes(key), output.read_bytes())
        self.assertEqual(unprotect(output), key)

    @unittest.skipUnless(os.name == 'nt', 'Windows adapter')
    def test_capture_hash_rejected_before_process_access(self):
        fake = self.root/'untrusted.dll'
        fake.write_bytes(b'not executable')
        with self.assertRaisesRegex(ValueError, 'DLL_HASH_MISMATCH'):
            capture(fake, '0'*64, 999999, 1, self.root/'x.dpapi', self.contact, 4096, 256000, False)
        self.assertFalse((self.root/'x.dpapi').exists())

    def test_backup_timeout_parameters(self):
        with self.assertRaisesRegex(ValueError, 'BACKUP_TIMEOUT_INVALID'):
            snapshot(self.contact, self.root/'copy.db', timeout_seconds=0)

    def test_encrypted_copy_requires_closed_and_no_wal(self):
        source = self.root/'encrypted.db'
        source.write_bytes(b'x'*4096)
        with self.assertRaisesRegex(ValueError, 'SOURCE_MUST_BE_CLOSED'):
            snapshot(source, self.root/'copy.db')
        Path(str(source)+'-wal').write_bytes(b'active')
        with self.assertRaisesRegex(ValueError, 'UNMERGED_JOURNAL'):
            snapshot(source, self.root/'copy.db', True)
        Path(str(source)+'-wal').unlink()
        snapshot(source, self.root/'copy.db', True)
        self.assertEqual(source.read_bytes(), (self.root/'copy.db').read_bytes())

    def test_inventory_does_not_dump_messages(self):
        self.shard(0, [(1, 1, 1, 1, 1, 'PRIVATE_SENTINEL')])
        text = json.dumps(inspect(self.root))
        self.assertNotIn('PRIVATE_SENTINEL', text)
        self.assertIn('message_0.db', text)

    def test_atomic_publish_rejects_race(self):
        output = self.root/'race.txt'
        with self.assertRaises(FileExistsError):
            with output_file(output) as temp:
                temp.write_text('mine')
                output.write_text('other')
        self.assertEqual(output.read_text(), 'other')

    def test_output_sidecars_rejected(self):
        for suffix in ('-wal', '-shm', '-journal'):
            output = self.root/('copy'+suffix+'.db')
            sidecar = Path(str(output)+suffix)
            sidecar.write_bytes(b'foreign')
            with self.assertRaisesRegex(ValueError, 'OUTPUT_SIDECAR_EXISTS'):
                snapshot(self.contact, output)
            self.assertEqual(sidecar.read_bytes(), b'foreign')
            self.assertFalse(output.exists())

    def test_sidecar_created_during_export_rejected(self):
        output = self.root/'copy.db'
        with self.assertRaisesRegex(ValueError, 'OUTPUT_SIDECAR_EXISTS'):
            with output_file(output) as temp:
                temp.write_text('mine')
                Path(str(output)+'-wal').write_bytes(b'other')
        self.assertFalse(output.exists())

    def encrypted_fixture(self, raw=False):
        from Crypto.Cipher import AES
        # An empty but valid SQLite database with 80 reserved bytes per page.
        page = bytearray(4096)
        page[:16] = b'SQLite format 3\0'
        page[16:18] = (4096).to_bytes(2, 'big')
        page[18:24] = bytes([1, 1, 80, 64, 32, 32])
        for offset, value in [(24, 1), (28, 1), (44, 4), (56, 1), (92, 1), (96, 3040000)]:
            page[offset:offset+4] = struct.pack('>I', value)
        page[100] = 13
        page[105:107] = (4016).to_bytes(2, 'big')
        key = bytearray(b'k'*32)
        salt, iv = bytes(range(16)), bytes(reversed(range(16)))
        aes = bytes(key) if raw else hashlib.pbkdf2_hmac('sha512', key, salt, 256000, 32)
        mac = hashlib.pbkdf2_hmac('sha512', aes, bytes(x^0x3A for x in salt), 2, 32)
        cipher = AES.new(aes, AES.MODE_CBC, iv).encrypt(bytes(page[16:4016]))
        tag = hmac.new(mac, cipher+iv+struct.pack('<I', 1), hashlib.sha512).digest()
        source = self.root/('raw.db' if raw else 'cipher.db')
        source.write_bytes(salt+cipher+iv+tag)
        return source, key, bytes(page)

    def test_authenticated_decryption(self):
        source, key, page = self.encrypted_fixture()
        output = self.root/'clear.db'
        stats = decrypt(source, output, key)
        self.assertEqual(stats['integrity'], 'ok')
        self.assertEqual(output.read_bytes(), page)

    def test_raw_key_mode(self):
        source, key, page = self.encrypted_fixture(True)
        decrypt(source, self.root/'clear.db', key, raw_key=True)
        self.assertEqual((self.root/'clear.db').read_bytes(), page)

    def test_multiple_encrypted_pages_and_second_page_tamper(self):
        from Crypto.Cipher import AES
        source, key, original = self.encrypted_fixture()
        first = bytearray(original)
        first[28:32] = struct.pack('>I', 2)
        first[32:36] = struct.pack('>I', 2)  # freelist trunk
        first[36:40] = struct.pack('>I', 1)  # one free page
        pages = [bytes(first), bytes(4096)]
        salt = bytes(range(16))
        aes = hashlib.pbkdf2_hmac('sha512', key, salt, 256000, 32)
        mac = hashlib.pbkdf2_hmac('sha512', aes, bytes(x^0x3A for x in salt), 2, 32)
        encrypted = bytearray()
        for number, page in enumerate(pages, 1):
            iv = bytes([number])*16
            start = 16 if number == 1 else 0
            cipher = AES.new(aes, AES.MODE_CBC, iv).encrypt(page[start:4016])
            tag = hmac.new(mac, cipher+iv+struct.pack('<I', number), hashlib.sha512).digest()
            encrypted.extend((salt if number == 1 else b'')+cipher+iv+tag)
        source.write_bytes(encrypted)
        out = self.root/'two-page.db'
        self.assertEqual(decrypt(source, out, key)['pages'], 2)
        self.assertEqual(out.read_bytes(), b''.join(pages))
        encrypted[4200] ^= 1
        source.write_bytes(encrypted)
        with self.assertRaisesRegex(ValueError, 'HMAC_FAILED'):
            decrypt(source, self.root/'tamper.db', key)
        self.assertFalse((self.root/'tamper.db').exists())

    def test_wrong_key_tamper_and_journal_rejected(self):
        source, key, _ = self.encrypted_fixture()
        with self.assertRaisesRegex(ValueError, 'HMAC_FAILED'):
            decrypt(source, self.root/'bad.db', bytearray(b'z'*32))
        self.assertFalse((self.root/'bad.db').exists())
        original = source.read_bytes()
        source.write_bytes(original[:100]+bytes([original[100]^1])+original[101:])
        with self.assertRaisesRegex(ValueError, 'HMAC_FAILED'):
            decrypt(source, self.root/'bad.db', key)
        Path(str(source)+'-wal').write_bytes(b'wal')
        with self.assertRaisesRegex(ValueError, 'UNMERGED_JOURNAL'):
            decrypt(source, self.root/'bad.db', key)

    def test_cli_read_end_to_end(self):
        self.shard(0, [(1, 101, 1, 1, 1, 'PRIVATE_SENTINEL')])
        script = Path(__file__).resolve().parents[1]/'scripts'/'read_chats.py'
        result = subprocess.run([sys.executable, str(script), '--contact-db', str(self.contact),
            '--message-dir', str(self.messages), '--output', str(self.root/'result.jsonl')], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('PRIVATE_SENTINEL', result.stdout)
        self.assertEqual(json.loads(result.stdout)['messages'], 1)


if __name__ == '__main__':
    unittest.main()
