"""Read validated WeChat-4-shaped SQLite copies and export JSONL (not live memory)."""
import argparse
import base64
import hashlib
import json
import io
from datetime import datetime, timezone
from pathlib import Path

from common import columns, identifier, output_file, readonly, run, tables, validate

REQUIRED = {'local_id', 'server_id', 'local_type', 'create_time', 'real_sender_id', 'message_content'}


def content(value):
    if value is None:
        return '', 'empty', None
    if isinstance(value, str):
        return value, 'text', None
    raw = bytes(value)
    try:
        if raw.startswith(b'\x28\xb5\x2f\xfd'):
            import zstandard
            # Bound decompression, not export truncation; preserve original on failure.
            declared = zstandard.frame_content_size(raw)
            if declared != zstandard.CONTENTSIZE_UNKNOWN and declared > 16*1024*1024:
                raise ValueError('PAYLOAD_TOO_LARGE')
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw), read_across_frames=True) as reader:
                decoded = reader.read(16*1024*1024 + 1)
            if len(decoded) > 16*1024*1024:
                raise ValueError('PAYLOAD_TOO_LARGE')
            # stream_reader accepts EOF inside a frame. Validate frame boundaries
            # separately, only AFTER the bounded pass has capped total plaintext.
            remaining = raw
            while remaining:
                validator = zstandard.ZstdDecompressor().decompressobj()
                validator.decompress(remaining)
                if not validator.eof:
                    raise ValueError('TRUNCATED_ZSTD_FRAME')
                tail = validator.unused_data
                if len(tail) >= len(remaining):
                    raise ValueError('INVALID_ZSTD_FRAME')
                remaining = tail
            return decoded.decode('utf-8'), 'zstd_text', None
        if b'\0' in raw:
            raise ValueError('BINARY_PAYLOAD')
        return raw.decode('utf-8'), 'utf8_bytes', None
    except Exception:
        # Unsupported payloads remain recoverable; no made-up message text.
        return None, 'binary_or_decode_failed', base64.b64encode(raw).decode('ascii')


def contacts_from(path):
    result = {}
    with readonly(path) as db:
        validate(db)
        if 'contact' not in tables(db) or 'username' not in columns(db, 'contact'):
            raise ValueError('UNSUPPORTED_CONTACT_SCHEMA')
        fields = columns(db, 'contact')
        selected = ['username'] + [n for n in ('remark', 'nick_name') if n in fields]
        for row in db.execute('SELECT ' + ','.join(map(identifier, selected)) + ' FROM contact'):
            row = dict(row)
            username = row['username']
            if not isinstance(username, str):
                continue
            result[hashlib.md5(username.encode()).hexdigest()] = {
                'username': username, 'display_name': row.get('remark') or row.get('nick_name') or username}
    return result


def export(contact_db, message_dir, output, self_id=None, conversation=None):
    contact_db = Path(contact_db).resolve(strict=True)
    message_dir = Path(message_dir).resolve(strict=True)
    shards = [message_dir] if message_dir.is_file() else sorted(message_dir.rglob('message_*.db'))
    if not shards:
        raise ValueError('NO_MESSAGE_SHARDS')
    if Path(output).resolve() in [contact_db, *shards]:
        raise ValueError('SOURCE_OUTPUT_CONFLICT')
    contacts = contacts_from(contact_db)
    seen, count, duplicates, binary, unknown_senders = {}, 0, 0, 0, 0
    first, last = None, None
    table_count = 0
    selected_tables = 0
    with output_file(output) as temp, temp.open('w', encoding='utf-8', newline='\n') as stream:
        for index, path in enumerate(shards):
            with readonly(path) as db:
                db.execute('BEGIN')
                validate(db)
                all_tables = tables(db)
                msg_tables = sorted(t for t in all_tables if t.startswith('Msg_'))
                if not msg_tables:
                    raise ValueError('UNSUPPORTED_MESSAGE_SCHEMA')
                if 'Name2Id' not in all_tables or 'user_name' not in columns(db, 'Name2Id'):
                    raise ValueError('UNSUPPORTED_SENDER_SCHEMA')
                # Deleted/missing contact records do not mean their messages are absent.
                sender_peers = {hashlib.md5(r[0].encode()).hexdigest(): r[0]
                                for r in db.execute('SELECT user_name FROM Name2Id') if isinstance(r[0], str)}
                for table in msg_tables:
                    if not REQUIRED <= columns(db, table):
                        raise ValueError('UNSUPPORTED_MESSAGE_SCHEMA')
                    table_count += 1
                    contact = contacts.get(table[4:], {})
                    peer = contact.get('username') or sender_peers.get(table[4:])
                    if not peer and conversation and table[4:] == hashlib.md5(conversation.encode()).hexdigest():
                        peer = conversation
                    if conversation and peer != conversation:
                        continue
                    selected_tables += 1
                    # Keep local IDs numeric; do not sort them lexicographically.
                    sql = ('SELECT m.*, n.user_name AS resolved_sender FROM ' + identifier(table) +
                           ' AS m LEFT JOIN Name2Id AS n ON m.real_sender_id=n.rowid ORDER BY m.create_time,m.local_id')
                    for row in db.execute(sql):
                        row = dict(row)
                        sender = row['resolved_sender']
                        text, encoding, blob = content(row['message_content'])
                        server = row['server_id']
                        stable = server not in (None, '', 0, '0')
                        key = (table, str(server)) if stable else (index, table, row['local_id'])
                        signature = hashlib.sha256(json.dumps(
                            [row['local_type'], row['create_time'], sender, text, blob], ensure_ascii=False).encode()).hexdigest()
                        if key in seen:
                            if seen[key] != signature:
                                raise ValueError('DUPLICATE_MESSAGE_CONFLICT')
                            duplicates += 1
                            continue
                        seen[key] = signature
                        timestamp = row['create_time']
                        # Retain the raw timestamp as evidence, even if it cannot be normalized.
                        try:
                            seconds = float(timestamp)
                            if seconds > 100_000_000_000:
                                seconds /= 1000
                            utc = datetime.fromtimestamp(seconds, timezone.utc).isoformat()
                            first = min(first, seconds) if first is not None else seconds
                            last = max(last, seconds) if last is not None else seconds
                        except (ValueError, TypeError, OSError, OverflowError):
                            utc = None
                        direction = ('sent' if sender == self_id else 'received') if sender and self_id else 'unknown'
                        record = {'conversation': peer, 'display_name': contact.get('display_name'),
                            'sender': sender, 'direction': direction, 'time_utc': utc, 'timestamp_raw': timestamp,
                            'message_type': row['local_type'], 'content': text, 'content_encoding': encoding,
                            'binary_base64': blob, 'server_id': server, 'local_id': row['local_id'],
                            'source_shard': path.name, 'source_shard_index': index, 'source_table': table}
                        stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                        count += 1
                        binary += blob is not None
                        unknown_senders += not bool(sender)
    return {'messages': count, 'shards': len(shards), 'tables': table_count, 'contacts': len(contacts),
            'selected_tables': selected_tables,
            'duplicates_removed': duplicates, 'binary_payloads_preserved': binary,
            'unknown_senders': unknown_senders, 'min_timestamp_seconds': first, 'max_timestamp_seconds': last,
            'ordering': 'shard_then_conversation_then_time', 'attachments_exported': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--contact-db', type=Path, required=True)
    p.add_argument('--message-dir', type=Path, required=True, help='Directory of message_*.db or one database')
    p.add_argument('--output', type=Path, required=True, help='New JSONL path; contains private data')
    p.add_argument('--self-id', help='Optional; unknown direction if omitted or sender unresolved')
    p.add_argument('--conversation', help='Optional exact contact username, not a label')
    a = p.parse_args()
    return export(a.contact_db, a.message_dir, a.output, a.self_id, a.conversation)


if __name__ == '__main__':
    run(main)
