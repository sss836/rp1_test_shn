"""Bounded archive inspection in memory. Never extracts, imports or executes."""
import gzip
import hashlib
import io
from pathlib import PurePosixPath
import re
import stat
import struct
import tarfile
import zipfile

from app.core.errors import ApiError

MAX_MEMBERS = 2048
MAX_MEMBER_BYTES = 32 * 1024**2
MAX_EXPANDED_BYTES = 128 * 1024**2
MAX_RATIO = 1000


def fail(message):
    raise ApiError(422, 'attachment_archive_unsafe', message)


def member_name(name):
    if (not isinstance(name, str) or not name or len(name) > 1024 or
            '\\' in name or any(ord(c) < 32 for c in name) or
            name.startswith('/') or re.match(r'^[A-Za-z]:', name)):
        fail('压缩包成员路径不安全。')
    name = name.rstrip('/')
    parts = name.split('/')
    if not name or any(p in {'', '.', '..'} for p in parts) or len(parts) > 32:
        fail('压缩包成员路径不安全或嵌套过深。')
    return PurePosixPath(name).as_posix()


def text_encoding(raw):
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        try:
            raw.decode('utf-16')
            return 'utf-16'
        except UnicodeDecodeError:
            return None
    if b'\0' in raw[:65536]:
        return None
    try:
        raw.decode('utf-8-sig')
        return 'utf-8-sig'
    except UnicodeDecodeError:
        return None


def _consume(stream, size, total):
    if size < 0 or size > MAX_MEMBER_BYTES:
        fail('压缩包单成员展开后超过32MiB。')
    digest = hashlib.sha256()
    chunks, count = [], 0
    while True:
        data = stream.read(min(65536, size - count + 1))
        if not data:
            break
        count += len(data)
        total[0] += len(data)
        if count > size or total[0] > MAX_EXPANDED_BYTES:
            fail('压缩包展开大小超限或声明大小不符。')
        digest.update(data)
        chunks.append(data)
    if count != size:
        fail('压缩包成员不完整。')
    return b''.join(chunks), digest.hexdigest()


class _LimitedStream:
    def __init__(self, stream, maximum):
        self.stream, self.maximum, self.count = stream, maximum, 0

    def read(self, size=-1):
        data = self.stream.read(min(size if size >= 0 else self.maximum + 1,
                                    self.maximum - self.count + 1))
        self.count += len(data)
        if self.count > self.maximum:
            fail('压缩包展开流超限，拒绝压缩炸弹。')
        return data


def inspect_archive(raw, filename, selected=None):
    """Return a complete ordinary-member index and optionally one member bytes.

    Nested archives are ordinary bytes, never recursively extracted or executed.
    A complete traversal validates every regular member, not just the preview.
    """
    lower = filename.lower()
    is_zip = raw.startswith((b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08')) or lower.endswith('.zip')
    is_tar = lower.endswith(('.tar', '.tar.gz', '.tgz'))
    if not is_zip and not is_tar:
        return None, None
    rows, names, total, content = [], {}, [0], None

    def add(name, directory, size, stream=None):
        nonlocal content
        name = member_name(name)
        if name in names or len(rows) >= MAX_MEMBERS:
            fail('压缩包存在重复成员或成员数超过2048。')
        for parent in PurePosixPath(name).parents:
            if str(parent) != '.' and names.get(str(parent)) == 'file':
                fail('压缩包文件与目录路径冲突。')
        if not directory and any(n.startswith(name + '/') for n in names):
            fail('压缩包文件与目录路径冲突。')
        names[name] = 'directory' if directory else 'file'
        row = {'path': name, 'kind': names[name], 'size': size}
        if directory:
            if size:
                fail('压缩包目录包含异常数据。')
        else:
            data, digest = _consume(stream, size, total)
            if total[0] > max(1, len(raw)) * MAX_RATIO:
                fail('压缩包展开比例超过安全上限。')
            encoding = text_encoding(data)
            row.update(sha256=digest, encoding=encoding, format='text' if encoding else 'binary')
            if selected == name:
                content = data
        rows.append(row)

    try:
        if is_zip:
            # Check the directory count before ZipFile allocates entry objects.
            end = raw.rfind(b'PK\x05\x06', max(0, len(raw) - 65557))
            if end < 0 or len(raw) - end < 22:
                fail('ZIP目录不完整。')
            _, disk, directory_disk, on_disk, count, directory_size, directory_offset, comment = struct.unpack_from('<4s4H2IH', raw, end)
            if disk or directory_disk or on_disk != count or count > MAX_MEMBERS or end + 22 + comment != len(raw):
                fail('ZIP分卷、目录数量或尾部不符合安全要求。')
            # EOCD counts are attacker-controlled. Count the actual directory
            # records before ZipFile can allocate one Python object per entry.
            if directory_offset + directory_size != end:
                fail('ZIP目录偏移或大小不符。')
            position, actual_count = directory_offset, 0
            while position < end:
                if position + 46 > end or raw[position:position + 4] != b'PK\x01\x02':
                    fail('ZIP目录记录不完整。')
                fields = struct.unpack_from('<4s6H3I5H2I', raw, position)
                position += 46 + fields[10] + fields[11] + fields[12]
                actual_count += 1
                if position > end or actual_count > MAX_MEMBERS:
                    fail('ZIP真实成员数量或目录长度超限。')
            if actual_count != count:
                fail('ZIP声明与真实成员数量不符。')
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                if len(archive.infolist()) != count:
                    fail('ZIP成员数量不符。')
                for item in archive.infolist():
                    member_name(item.orig_filename)
                    mode = stat.S_IFMT(item.external_attr >> 16)
                    if item.flag_bits & 1 or mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        fail('不接受加密、链接、设备或其他特殊ZIP成员。')
                    directory = item.is_dir()
                    if mode == stat.S_IFDIR and not directory or mode == stat.S_IFREG and directory:
                        fail('ZIP成员类型与目录标记冲突。')
                    if item.file_size > max(1, item.compress_size) * MAX_RATIO:
                        fail('ZIP单成员压缩比例超限。')
                    if directory:
                        add(item.filename, True, item.file_size)
                    else:
                        with archive.open(item) as stream:
                            add(item.filename, False, item.file_size, stream)
            kind = 'zip'
        else:
            compressed = raw.startswith(b'\x1f\x8b')
            source = gzip.GzipFile(fileobj=io.BytesIO(raw)) if compressed else io.BytesIO(raw)
            maximum = min(MAX_EXPANDED_BYTES + MAX_MEMBERS * 2048,
                          max(10240, len(raw) * MAX_RATIO))
            with source:
                with tarfile.open(fileobj=_LimitedStream(source, maximum), mode='r|') as archive:
                    for item in archive:
                        if not (item.isfile() or item.isdir()) or item.sparse is not None:
                            fail('不接受TAR链接、设备、稀疏文件或其他特殊成员。')
                        if item.isdir():
                            add(item.name, True, item.size)
                        else:
                            with archive.extractfile(item) as stream:
                                add(item.name, False, item.size, stream)
                    # Consume buffered padding and the gzip trailer, so a bad
                    # CRC/truncation or a second hidden TAR cannot be silently
                    # labelled a complete index of the uploaded bytes.
                    while True:
                        padding = archive.fileobj.read(65536)
                        if not padding:
                            break
                        if any(padding):
                            fail('TAR结束标记后存在未索引数据，拒绝伪装完整目录。')
            kind = 'tar.gz' if compressed else 'tar'
    except ApiError:
        raise
    except (OSError, ValueError, RuntimeError, EOFError, tarfile.TarError, zipfile.BadZipFile, NotImplementedError):
        fail('压缩包损坏、格式不支持或无法安全读取。')
    return {'format': kind, 'members': rows, 'expanded_bytes': total[0]}, content
