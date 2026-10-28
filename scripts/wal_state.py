"""Read-only WAL classification for closed database copies; never replay/delete logs.

SQLite fileformat2 sections 4.1-4.5: salts define a generation, checksums
validate the current contiguous frame sequence, and reset need not truncate.
Unknown/corrupt logs fail closed. Any valid current-generation frame is rejected
by the page-only exporter, even if there is not yet a commit marker.
"""
import hashlib
import struct
import time
from pathlib import Path


def file_hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checksum(data, endian, seed=(0, 0)):
    if len(data) % 8:
        raise ValueError('WAL_CHECKSUM_INPUT_INVALID')
    s0, s1 = seed
    for x0, x1 in struct.iter_unpack(endian+'II', data):
        s0 = (s0+x0+s1) & 0xffffffff
        s1 = (s1+x1+s0) & 0xffffffff
    return s0, s1


def checksum_seed(data, endian, result):
    """Invert SQLite's additive checksum to recover the seed of a known frame."""
    s0, s1 = result
    words = list(struct.iter_unpack(endian+'II', data))
    for x0, x1 in reversed(words):
        previous_s1 = (s1-x1-s0) & 0xffffffff
        previous_s0 = (s0-x0-previous_s1) & 0xffffffff
        s0, s1 = previous_s0, previous_s1
    return s0, s1


def verify_reset_frame(header, frame, page, endian, current_seed):
    """Require positive previous-generation evidence, not merely a salt mismatch.

    Salts are not covered by the frame checksum. A salt-only damaged current
    frame still verifies using the current header seed and must be rejected.
    For an old first frame, recover its checksum seed, reconstruct its prior
    header checkpoint counter algebraically, and verify the complete header/
    frame chain plus the documented salt-1 increment on reset.
    """
    stored = struct.unpack('>II', frame[16:24])
    body = frame[:8]+page
    if checksum(body, endian, current_seed) == stored:
        raise ValueError('WAL_FRAME_SALT_MISMATCH')
    old_salt1, old_salt2 = struct.unpack('>II', frame[8:16])
    salt1, salt2 = struct.unpack('>II', header[16:24])
    if ((old_salt1+1) & 0xffffffff) != salt1 or old_salt2 == salt2:
        raise ValueError('WAL_RESET_UNPROVEN')
    prior_seed = checksum_seed(body, endian, stored)
    # The checkpoint-counter word contributes (x, 2*x) to the 24-byte
    # header checksum. Solve it directly; no brute-force/parameter guessing.
    prior_base = header[:12]+bytes(4)+frame[8:16]
    base_seed = checksum(prior_base, endian)
    counter_word = (prior_seed[0]-base_seed[0]) & 0xffffffff
    if ((prior_seed[1]-base_seed[1]) & 0xffffffff) != ((2*counter_word) & 0xffffffff):
        raise ValueError('WAL_RESET_UNPROVEN')
    prior_header = header[:12]+struct.pack(endian+'I', counter_word)+frame[8:16]
    if checksum(prior_header, endian) != prior_seed:
        raise ValueError('WAL_RESET_UNPROVEN')


def inspect_wal(path, expected_page_size=None):
    path = Path(path)
    if not path.exists():
        return {'state': 'absent', 'safe_without_replay': True, 'valid_frames': 0, 'commits': 0}
    size = path.stat().st_size
    if size == 0:
        return {'state': 'empty', 'safe_without_replay': True, 'valid_frames': 0, 'commits': 0}
    deadline = time.monotonic()+30
    with path.open('rb') as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise ValueError('WAL_HEADER_INVALID')
        magic, version, page_size = struct.unpack('>III', header[:12])
        if magic not in (0x377f0682, 0x377f0683) or version != 3007000:
            raise ValueError('WAL_HEADER_INVALID')
        if not (512 <= page_size <= 65536 and page_size & (page_size-1) == 0):
            raise ValueError('WAL_PAGE_SIZE_INVALID')
        if expected_page_size is not None and page_size != expected_page_size:
            raise ValueError('WAL_PAGE_SIZE_MISMATCH')
        endian = '<' if magic == 0x377f0682 else '>'
        rolling = checksum(header[:24], endian)
        if rolling != struct.unpack('>II', header[24:]):
            raise ValueError('WAL_HEADER_CHECKSUM_INVALID')
        frames = commits = 0
        stop = 'eof'
        while stream.tell() < size:
            if time.monotonic() >= deadline or frames >= 1000000:
                raise ValueError('WAL_SCAN_LIMIT')
            frame = stream.read(24)
            if len(frame) != 24:
                raise ValueError('WAL_TRUNCATED')
            if frame[8:16] != header[16:24]:
                if frames:
                    # Any current frames already forbid main-file-only export.
                    stop = 'stale_generation'
                    break
                page = stream.read(page_size)
                if len(page) != page_size or struct.unpack('>I', frame[:4])[0] == 0:
                    raise ValueError('WAL_RESET_UNPROVEN')
                verify_reset_frame(header, frame, page, endian, rolling)
                stop = 'stale_generation'
                break
            page_number, commit_size = struct.unpack('>II', frame[:8])
            if page_number == 0:
                raise ValueError('WAL_FRAME_INVALID')
            page = stream.read(page_size)
            if len(page) != page_size:
                raise ValueError('WAL_TRUNCATED')
            rolling = checksum(page, endian, checksum(frame[:8], endian, rolling))
            if rolling != struct.unpack('>II', frame[16:24]):
                raise ValueError('WAL_FRAME_CHECKSUM_INVALID')
            frames += 1
            commits += commit_size != 0
    return {'state': 'requires_replay' if frames else ('reset' if stop == 'stale_generation' else 'header_only'),
            'safe_without_replay': frames == 0, 'valid_frames': frames, 'commits': commits,
            'page_size': page_size, 'header_checksum_valid': True}


def sidecar_guard(source, expected_page_size=4096):
    """Return a stable fingerprint plus WAL classification, not secret data.

    The caller must compare guards before/after copying or decrypting the main
    file. This never makes a live source safe; source-closed remains a prerequisite.
    """
    source = Path(source)
    journal = Path(str(source)+'-journal')
    journal_before = file_hash(journal) if journal.exists() else None
    if journal.exists() and journal.stat().st_size:
        raise ValueError('UNMERGED_JOURNAL: rollback journal requires separate recovery')
    wal = Path(str(source)+'-wal')
    before = file_hash(wal) if wal.exists() else None
    try:
        state = inspect_wal(wal, expected_page_size)
    except ValueError as exc:
        raise ValueError('UNMERGED_JOURNAL: '+str(exc)) from None
    after = file_hash(wal) if wal.exists() else None
    journal_after = file_hash(journal) if journal.exists() else None
    if before != after or journal_before != journal_after:
        raise ValueError('SOURCE_CHANGED')
    if not state['safe_without_replay']:
        raise ValueError('UNMERGED_JOURNAL: current-generation WAL frames require replay')
    return {'wal_fingerprint': before, 'journal_fingerprint': journal_before, 'wal_state': state['state']}
