"""Append-only, checksummed statement frames with independently readable rows.

File: MAGIC, [TXN header | (row header + JSON)* | commit trailer]*.
Only complete committed frames are recovered. A torn trailing frame is removed.
"""

import json
import os
import struct
import zlib

from .btree import BPlusTree
from .errors import StorageError
from .locking import RWLock
from .schema import validate_value

MAGIC = b"MINIDB01"
TRANSACTION_PREFIX = struct.Struct(">4sQI")
TRANSACTION = struct.Struct(">4sQII")  # magic, payload bytes, row count, header CRC32
RECORD = struct.Struct(">II")  # JSON length, CRC32
TRAILER = struct.Struct(">4sI")  # commit marker, CRC32(header + payload)
MAX_FRAME = 64 * 1024 * 1024


def encode_row(row):
    data = json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return RECORD.pack(len(data), zlib.crc32(data)) + data


class Table:
    def __init__(self, path, columns, durable=True, order=64):
        self.path, self.columns, self.durable, self.order = path, columns, durable, order
        self.lock = RWLock()
        self.recovered_bytes = 0
        self.poisoned = False
        if not path.exists():
            with path.open("xb") as file:
                file.write(MAGIC)
                file.flush()
                if durable:
                    os.fsync(file.fileno())
        self.index = BPlusTree(order)
        self.next_id = 1
        self.rebuild(recover=True)

    def _check_row(self, row, expected_id):
        if not isinstance(row, dict) or set(row) != {"id", *self.columns}:
            raise StorageError(f"Invalid stored columns in {self.path.name}")
        if type(row["id"]) is not int or row["id"] != expected_id:
            raise StorageError(f"Invalid stored ID in {self.path.name}")
        try:
            for name, spec in self.columns.items():
                validate_value(name, row[name], spec)
        except Exception as error:
            raise StorageError(f"Invalid stored value in {self.path.name}: {error}") from error

    def records(self, recover=False):
        with self.path.open("r+b" if recover else "rb") as file:
            if file.read(len(MAGIC)) != MAGIC:
                raise StorageError(f"Invalid file header: {self.path.name}")
            file_size = os.fstat(file.fileno()).st_size
            while file.tell() < file_size:
                start = file.tell()
                header = file.read(TRANSACTION.size)
                incomplete = len(header) != TRANSACTION.size
                if not incomplete:
                    marker, size, count, header_crc = TRANSACTION.unpack(header)
                    if (marker != b"TXN1" or header_crc != zlib.crc32(header[:-4])
                            or size > MAX_FRAME or count == 0 or count > 10_000):
                        raise StorageError(f"Corrupt transaction header at offset {start}")
                    incomplete = start + TRANSACTION.size + size + TRAILER.size > file_size
                if incomplete:
                    if not recover:
                        raise StorageError(f"Incomplete transaction at offset {start}")
                    file.truncate(start)
                    file.flush()
                    if self.durable:
                        os.fsync(file.fileno())
                    self.recovered_bytes += file_size - start
                    return
                payload = file.read(size)
                marker, checksum = TRAILER.unpack(file.read(TRAILER.size))
                if marker != b"CMIT" or checksum != zlib.crc32(payload, zlib.crc32(header)):
                    raise StorageError(f"Transaction checksum mismatch at offset {start}")
                position = 0
                for _ in range(count):
                    if position + RECORD.size > size:
                        raise StorageError("Truncated record header")
                    length, checksum = RECORD.unpack_from(payload, position)
                    offset = start + TRANSACTION.size + position
                    position += RECORD.size
                    if position + length > size:
                        raise StorageError("Truncated record")
                    data = payload[position:position + length]
                    position += length
                    yield offset, self._decode(data, checksum)
                if position != size:
                    raise StorageError("Transaction row count does not match payload")

    @staticmethod
    def _decode(data, checksum):
        if zlib.crc32(data) != checksum:
            raise StorageError("Record checksum mismatch")
        try:
            return json.loads(data)
        except (ValueError, UnicodeError) as error:
            raise StorageError("Invalid record JSON") from error

    def read_at(self, file, offset):
        file.seek(offset)
        header = file.read(RECORD.size)
        if len(header) != RECORD.size:
            raise StorageError("Missing indexed record")
        length, checksum = RECORD.unpack(header)
        if length > MAX_FRAME:
            raise StorageError("Invalid record length")
        data = file.read(length)
        if len(data) != length:
            raise StorageError("Truncated indexed record")
        return self._decode(data, checksum)

    def rebuild(self, recover=False):
        index = BPlusTree(self.order)
        expected_id = 1
        for offset, row in self.records(recover):
            self._check_row(row, expected_id)
            index.insert(row["id"], offset)
            expected_id += 1
        self.index, self.next_id = index, expected_id

    def append(self, rows):
        """Caller holds the write lock and has validated the entire statement."""
        if self.poisoned:
            raise StorageError("Table unavailable after an I/O failure; restart and inspect storage")
        chunks = [encode_row(row) for row in rows]
        size = sum(map(len, chunks))
        if size > MAX_FRAME:
            raise StorageError("INSERT exceeds 64 MiB storage frame limit")
        prefix = TRANSACTION_PREFIX.pack(b"TXN1", size, len(rows))
        header = prefix + struct.pack(">I", zlib.crc32(prefix))
        payload = b"".join(chunks)
        trailer = TRAILER.pack(b"CMIT", zlib.crc32(payload, zlib.crc32(header)))
        start = self.path.stat().st_size
        try:
            with self.path.open("ab") as file:
                file.write(header)
                file.write(payload)
                file.write(trailer)
                file.flush()
                if self.durable:
                    os.fsync(file.fileno())
            offset = start + TRANSACTION.size
            for row, chunk in zip(rows, chunks):
                self.index.insert(row["id"], offset)
                offset += len(chunk)
            self.next_id += len(rows)
        except Exception as error:
            # Roll back both the append and any partial in-memory index update.
            try:
                with self.path.open("r+b") as file:
                    file.truncate(start)
                    file.flush()
                    if self.durable:
                        os.fsync(file.fileno())
                self.rebuild()
            except Exception:
                self.poisoned = True
            raise StorageError("INSERT failed; inspect storage and retry only after checking the result") from error
