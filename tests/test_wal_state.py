"""Synthetic WAL generations, checksums and source-preservation regressions."""
import os
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from snapshot_database import snapshot
from wal_state import checksum, inspect_wal, sidecar_guard


def wal_header(salts=(10, 20), endian='<', page_size=4096):
    header = struct.pack('>6I', 0x377f0682 if endian == '<' else 0x377f0683,
                         3007000, page_size, 1, *salts)
    return header+struct.pack('>II', *checksum(header, endian))


def frame(header, page_number=1, commit=0, seed=None, endian='<'):
    page = bytes([page_number % 256])*4096
    prefix = struct.pack('>II', page_number, commit)
    rolling = checksum(page, endian, checksum(prefix, endian, seed or struct.unpack('>II', header[24:32])))
    return prefix+header[16:24]+struct.pack('>II', *rolling)+page, rolling


class WalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root/'encrypted.db'
        self.source.write_bytes(b'encrypted-fixture'.ljust(4096, b'\0'))
        self.wal = Path(str(self.source)+'-wal')

    def tearDown(self):
        self.tmp.cleanup()

    def test_absent_empty_and_header_only(self):
        self.assertEqual(inspect_wal(self.wal)['state'], 'absent')
        self.wal.write_bytes(b'')
        self.assertEqual(inspect_wal(self.wal)['state'], 'empty')
        self.wal.write_bytes(wal_header())
        self.assertEqual(inspect_wal(self.wal)['state'], 'header_only')

    def test_reset_with_old_commits_and_preallocation(self):
        old_header = wal_header((1, 2))
        old_frame, _ = frame(old_header, commit=1)
        self.wal.write_bytes(wal_header((2, 4))+old_frame+bytes(112))
        state = inspect_wal(self.wal)
        self.assertEqual(state['state'], 'reset')
        self.assertTrue(state['safe_without_replay'])
        self.assertEqual(state['valid_frames'], 0)
        original, original_wal = self.source.read_bytes(), self.wal.read_bytes()
        output = self.root/'copy.db'
        snapshot(self.source, output, source_closed=True)
        self.assertEqual(output.read_bytes(), original)
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.wal.read_bytes(), original_wal)

    def test_header_checksum_both_endiannesses(self):
        for endian in ('<', '>'):
            self.wal.write_bytes(wal_header(endian=endian))
            self.assertTrue(inspect_wal(self.wal)['header_checksum_valid'])

    def test_current_commits_rejected_both_endiannesses(self):
        for endian in ('<', '>'):
            header = wal_header(endian=endian)
            one, seed = frame(header, endian=endian)
            two, _ = frame(header, 2, 2, seed, endian)
            self.wal.write_bytes(header+one+two)
            state = inspect_wal(self.wal)
            self.assertEqual(state['valid_frames'], 2)
            self.assertEqual(state['commits'], 1)
            self.assertFalse(state['safe_without_replay'])
            with self.assertRaisesRegex(ValueError, 'UNMERGED_JOURNAL'):
                snapshot(self.source, self.root/'bad.db', source_closed=True)
            self.assertFalse((self.root/'bad.db').exists())

    def test_uncommitted_current_frames_also_block(self):
        header = wal_header()
        one, _ = frame(header)
        self.wal.write_bytes(header+one)
        self.assertEqual(inspect_wal(self.wal)['commits'], 0)
        with self.assertRaisesRegex(ValueError, 'UNMERGED_JOURNAL'):
            sidecar_guard(self.source)

    def test_old_tail_after_valid_frame_does_not_hide_current_commit(self):
        header = wal_header()
        current, _ = frame(header, commit=1)
        old, _ = frame(wal_header((999, 888)), commit=1)
        self.wal.write_bytes(header+current+old)
        with self.assertRaisesRegex(ValueError, 'UNMERGED_JOURNAL'):
            sidecar_guard(self.source)

    def test_header_and_frame_corruption_fail_closed(self):
        header = bytearray(wal_header())
        header[16] ^= 1
        self.wal.write_bytes(header)
        with self.assertRaisesRegex(ValueError, 'WAL_HEADER_CHECKSUM_INVALID'):
            inspect_wal(self.wal)
        header = wal_header()
        data, _ = frame(header, commit=1)
        damaged = bytearray(data)
        damaged[100] ^= 1
        self.wal.write_bytes(header+damaged)
        with self.assertRaisesRegex(ValueError, 'WAL_FRAME_CHECKSUM_INVALID'):
            inspect_wal(self.wal)

    def test_salt_only_corruption_never_looks_like_reset(self):
        header = wal_header()
        data, _ = frame(header, commit=1)
        for position in (8, 12):
            damaged = bytearray(data)
            damaged[position] ^= 1
            self.wal.write_bytes(header+damaged)
            with self.assertRaisesRegex(ValueError, 'WAL_FRAME_SALT_MISMATCH'):
                inspect_wal(self.wal)
            with self.assertRaisesRegex(ValueError, 'UNMERGED_JOURNAL'):
                snapshot(self.source, self.root/'copy.db', source_closed=True)
            self.assertFalse((self.root/'copy.db').exists())

    def test_corrupt_old_body_is_not_proof_of_reset(self):
        old_header = wal_header((1, 2))
        old, _ = frame(old_header, commit=1)
        damaged = bytearray(old)
        damaged[100] ^= 1
        self.wal.write_bytes(wal_header((2, 3))+damaged)
        with self.assertRaisesRegex(ValueError, 'WAL_RESET_UNPROVEN'):
            inspect_wal(self.wal)

    def test_reset_chain_both_endiannesses(self):
        for endian in ('<', '>'):
            old_header = wal_header((0xffffffff, 2), endian)
            old, _ = frame(old_header, commit=1, endian=endian)
            self.wal.write_bytes(wal_header((0, 3), endian)+old)
            self.assertEqual(inspect_wal(self.wal)['state'], 'reset')
    def test_truncation_is_not_reset(self):
        header = wal_header()
        for data in (header[:10], header+b'x', header+frame(header)[0][:-1]):
            self.wal.write_bytes(data)
            with self.assertRaises(ValueError):
                inspect_wal(self.wal)
            with self.assertRaisesRegex(ValueError, 'UNMERGED_JOURNAL'):
                sidecar_guard(self.source)

    def test_unknown_format_and_page_size_rejected(self):
        header = bytearray(wal_header())
        header[4:8] = struct.pack('>I', 1)
        self.wal.write_bytes(header)
        with self.assertRaisesRegex(ValueError, 'WAL_HEADER_INVALID'):
            inspect_wal(self.wal)
        self.wal.write_bytes(wal_header(page_size=1024))
        with self.assertRaisesRegex(ValueError, 'WAL_PAGE_SIZE_MISMATCH'):
            sidecar_guard(self.source, 4096)

    def test_journal_still_blocks(self):
        Path(str(self.source)+'-journal').write_bytes(b'hot')
        with self.assertRaisesRegex(ValueError, 'UNMERGED_JOURNAL'):
            sidecar_guard(self.source)

    def test_wal_change_during_copy_blocks_publication(self):
        self.wal.write_bytes(wal_header())
        import shutil
        original_copy = shutil.copyfile
        def changing(source, output):
            original_copy(source, output)
            self.wal.write_bytes(wal_header((50, 60)))
        with patch('snapshot_database.shutil.copyfile', side_effect=changing):
            with self.assertRaisesRegex(ValueError, 'SOURCE_CHANGED'):
                snapshot(self.source, self.root/'copy.db', source_closed=True)
        self.assertFalse((self.root/'copy.db').exists())

    def test_parser_matches_native_sqlite_wal(self):
        # Native SQLite, not our synthetic checksum writer, provides this fixture.
        source = self.root/'native.db'
        db = sqlite3.connect(source)
        try:
            db.execute('PRAGMA page_size=4096')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE synthetic(value TEXT)')
            db.execute("INSERT INTO synthetic VALUES('test')")
            db.commit()
            state = inspect_wal(Path(str(source)+'-wal'))
            self.assertGreater(state['valid_frames'], 0)
            self.assertGreater(state['commits'], 0)
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
