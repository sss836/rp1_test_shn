"""Job-scoped read-only diagnostic evidence. Never executes or changes files."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time


MAX_READ = 65536


def redact_text(value):
    # Keep parity with the platform's assistant_privacy for model-visible text.
    # Cheap markers and bounded schemes/addresses avoid quadratic backtracking
    # on a long source/CSV line containing no URL or e-mail delimiter.
    if '://' in value and '@' in value:
        value = re.sub(r"(?i)([a-z][a-z0-9+.-]{0,31}://)[^/\s:@]+:[^/\s@]+@", r"\1[凭据]@", value)
    if 'sk-' in value.lower() or 'bearer' in value.lower():
        value = re.sub(r"(?i)\bsk-[a-z0-9_-]{16,}|Bearer\s+[a-z0-9._~-]+", "[凭据已隐藏]", value)
    value = re.sub(r"(?i)((?:password|passwd|api[_-]?key|secret|access[_-]?token|cookie)\s*[=:]\s*)[^\s,;]+", r"\1[已隐藏]", value)
    if '@' in value:
        value = re.sub(r"[\w.+-]{1,128}@[\w.-]{1,255}\.[a-zA-Z]{2,24}", "[邮箱已隐藏]", value)
    value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号已隐藏]", value)
    value = re.sub(r"(?i)(/home/|/Users/|[A-Z]:\\Users\\)[^/\\\s]+", r"\1[用户]", value)
    value = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[私钥已隐藏]", value, flags=re.S)
    return value


def receipt(status, **values):
    return dict(status=status, source='paired_hmi_live_read',
                updated_at=datetime.now(timezone.utc).isoformat(), **values)


class DiagnosticFileReader:
    def __init__(self, data_root, *, roots=None):
        self.data_root = Path(data_root).expanduser().resolve()
        code_root = Path(__file__).resolve().parents[3]
        candidates = roots if roots is not None else [code_root, Path('/opt/rp1-test-hmi'), self.data_root]
        self.roots = tuple(dict.fromkeys(Path(p).expanduser().resolve() for p in candidates if Path(p).exists()))
        self.aliases = {'$STATE': self.data_root, '$PROJECT': self.roots[0] if roots is not None and self.roots else code_root,
                        '$INSTALL': Path('/opt/rp1-test-hmi').resolve(), '$HOME': Path.home().resolve()}
        self._lock = threading.RLock()
        self._grants = {}
        self._analysis = None
        self._scope_guards, self._revoked_scopes, self._managed_scopes = {}, set(), set()
        self._private_root = self.data_root / 'diagnostics'

    def revoke(self, scope=None):
        with self._lock:
            if scope is None:
                self._grants.clear()
                self._revoked_scopes.update(self._scope_guards)
                self._scope_guards.clear()
            else:
                self._grants.pop(scope, None)
                self._scope_guards.pop(scope, None)
                self._revoked_scopes.add(scope)
            analysis = self._analysis
        if analysis is not None:
            analysis.revoke(scope)

    def _analysis_helper(self):
        with self._lock:
            if self._analysis is None:
                from factory_hmi.core.diagnostic_analysis import DiagnosticAnalysis
                self._analysis = DiagnosticAnalysis(self)
            return self._analysis

    def bind_scope(self, scope, current):
        with self._lock:
            if not current(): raise ValueError('诊断关联已经撤销，未继续读取。')
            self._managed_scopes.add(scope)
            self._revoked_scopes.discard(scope)
            self._scope_guards[scope] = current

    def grant(self, scope, path, kind):
        if kind not in {'file', 'directory', 'all'}:
            raise ValueError('unknown read-only grant')
        resolved = self._resolve(path)
        if kind == 'directory' and not resolved.is_dir(): resolved = resolved.parent
        with self._lock:
            if scope in self._managed_scopes and scope in self._revoked_scopes:
                raise ValueError('任务已经撤销，不能再允许读取。')
            self._revoked_scopes.discard(scope)
            self._grants.setdefault(scope, []).append((kind, resolved))

    def _allowed(self, scope, path):
        if path == self._private_root or path.is_relative_to(self._private_root): return False
        with self._lock:
            if scope in self._revoked_scopes: return False
            guard = self._scope_guards.get(scope)
        if guard is not None and not guard(): return False
        if any(path == root or path.is_relative_to(root) for root in self.roots): return True
        with self._lock:
            return any(kind == 'all' or path == root or (kind == 'directory' and path.is_relative_to(root))
                       for kind, root in self._grants.get(scope, []))

    def _resolve(self, value):
        for alias, root in self.aliases.items():
            if str(value) == alias or str(value).startswith(alias + '/'):
                return (root / str(value)[len(alias):].lstrip('/')).resolve()
        if '/[用户]' in str(value):
            raise ValueError('脱敏路径不能作为真实路径，请使用$STATE/$PROJECT/$INSTALL/$HOME别名')
        return Path(value).expanduser().resolve()

    def reference(self, path):
        path = Path(path).resolve()
        choices = [(alias, root) for alias, root in self.aliases.items() if path == root or path.is_relative_to(root)]
        if not choices: return str(path)
        alias, root = max(choices, key=lambda item: len(str(item[1])))
        relative = path.relative_to(root).as_posix()
        return alias if relative == '.' else alias + '/' + relative

    def run(self, scope, request):
        operation = request.get('operation')
        if operation not in {'capabilities', 'list', 'read', 'search', 'execution_summary', 'discover', 'csv_diagnosis', 'log_diagnosis'}:
            return receipt('invalid_request', error='未知只读操作', eof=False)
        offset, limit = request.get('offset', 0), request.get('limit', 16384)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= MAX_READ:
            return receipt('invalid_request', error='offset必须非负，limit须为1..65536', eof=False)
        if operation == 'capabilities':
            return receipt('read', roots=[self.reference(p) for p in self.roots],
                           path_aliases={alias: dict(path=str(root), default_authorized=self._allowed(scope, root)) for alias, root in self.aliases.items()},
                           operations=['capabilities','list','read','search','execution_summary','discover','csv_diagnosis','log_diagnosis'],
                           read_only=True, max_chunk_bytes=MAX_READ, grant_scope='current_job_and_account',
                           os_permissions_bypassed=False, eof=True, unread=0)
        if operation == 'discover':
            try: return self._analysis_helper().run(scope, request)
            except PermissionError as exc:
                return receipt('os_permission_denied', error=str(exc), eof=False, advice='当前账户不能读取这些目录，不会提升系统权限。')
            except (OSError,ValueError,RuntimeError) as exc:
                return receipt('error',error=f'{type(exc).__name__}: {exc}',eof=False)
        value = request.get('path') or (str(self.data_root) if operation == 'execution_summary' else str(self.roots[0]) if self.roots else '')
        if not isinstance(value, str) or not value or len(value) > 4096 or '\x00' in value:
            return receipt('invalid_request', error='路径无效', eof=False)
        try:
            path = self._resolve(value)
            if path == self._private_root or path.is_relative_to(self._private_root):
                return receipt('unsupported_file',path=self.reference(path),eof=False,error='助手内部会话与凭据缓存不作为诊断资料读取。')
            if any(path == root or path.is_relative_to(root) for root in map(Path, ('/dev', '/proc', '/sys'))):
                return receipt('unsupported_file', path=str(path), error='不读取设备及虚拟系统文件', eof=False)
            if not self._allowed(scope, path):
                return receipt('permission_required', path=str(path), resolved_path=str(path), path_ref=self.reference(path), operation=operation, eof=False,
                               error='此路径不在当前任务授权范围；内容会发送到统一平台模型，须由当前窗口用户授权。')
            if operation == 'execution_summary':
                # The summary source is always this installation's state root.
                # An arbitrary path cannot redirect it to another machine/store.
                if path != self.data_root:
                    return receipt('invalid_request', path=str(path), error='执行汇总仅读取本实例状态根', eof=False)
                return self._execution_summary(scope, offset, limit)
            info = path.stat()
            if operation in {'csv_diagnosis','log_diagnosis'}:
                if not stat.S_ISREG(info.st_mode): raise ValueError('分析只允许普通文件，不读取设备、目录、FIFO或socket')
                return self._analysis_helper().run(scope, request, path, info)
            if operation == 'read': return self._read(scope, path, info, offset, limit)
            if operation == 'list': return self._list(scope, path, offset, limit)
            query = request.get('query')
            if not isinstance(query, str) or not 1 <= len(query) <= 2000:
                return receipt('invalid_request', path=str(path), error='搜索须提供1..2000字的字面文本', eof=False)
            return self._search(scope, path, info, query, offset, limit)
        except PermissionError as exc:
            return receipt('os_permission_denied', path=value, error=str(exc), eof=False,
                           advice='当前操作系统账户无权读取。界面授权不能越过OS权限；请选择可读副本，或由用户处理该路径系统权限。')
        except (OSError, ValueError, RuntimeError) as exc:
            return receipt('error', path=value, error=f'{type(exc).__name__}: {exc}', eof=False)

    def _check_fd(self, scope, fd, expected):
        # /proc/self/fd is used only to attest this process's already-open inode;
        # it is never a model-readable diagnostic root.
        actual = os.readlink(f'/proc/self/fd/{fd}')
        if actual.endswith(' (deleted)') or actual != str(expected) or expected.resolve() != expected:
            raise ValueError('路径在授权后发生替换，未读取或返回内容')
        if not self._allowed(scope, Path(actual)):
            raise ValueError('实际文件描述符目标不在当前任务授权范围')
        return actual

    def _open_fd(self, scope, path, *, directory=False):
        # O_PATH cannot activate a substituted device/FIFO. Only after type and
        # actual inode path checks do we open this exact inode for reading.
        anchor = os.open(path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(anchor)
            valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
            if not valid: raise ValueError('只允许普通目录或文件，不读取设备、FIFO、socket或替换符号链接')
            self._check_fd(scope, anchor, path)
            flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC
            if directory: flags |= os.O_DIRECTORY
            fd = os.open(f'/proc/self/fd/{anchor}', flags)
            try:
                self._check_fd(scope, fd, path)
                if (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError('锚定文件身份不符')
            except Exception:
                os.close(fd); raise
            return fd
        finally:
            os.close(anchor)

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _bytes(self, scope, path, info, offset, limit):
        fd = self._open_fd(scope, path)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if self._identity(opened) != self._identity(info): raise ValueError('文件在打开前发生变化')
            stream.seek(offset); data = stream.read(limit)
            self._check_fd(scope, stream.fileno(), path)
            if self._identity(os.fstat(stream.fileno())) != self._identity(opened): raise ValueError('文件读取期间发生变化')
        return data

    def _redacted_range(self, stream, path, offset, raw, total):
        # A known private-key file never contributes body bytes, even when the
        # requested offset lies in the middle of its encoded key material.
        if path.name.lower() in {'id_rsa','id_dsa','id_ecdsa','id_ed25519'} or path.suffix.lower() == '.key':
            return 'withheld', None, 'private_key_body_withheld'
        end, scanned, overlap = offset + len(raw), 0, b''
        deadline = time.monotonic() + 3
        stream.seek(0)
        while scanned < end:
            if time.monotonic() >= deadline:
                return 'withheld', None, 'redaction_context_budget_exceeded'
            part = stream.read(min(65536, end - scanned))
            if not part: break
            if re.search(rb'-----BEGIN [A-Z0-9 ]{0,64}PRIVATE KEY-----', overlap + part):
                return 'withheld', None, 'private_key_body_withheld'
            overlap = (overlap + part)[-128:]; scanned += len(part)
        # Whole-line context means a label split across a chunk boundary, or a
        # value whose label lies in the preceding chunk, cannot leak. A line too
        # large to classify within this bounded context is withheld explicitly.
        start = max(0, offset - 262144)
        stream.seek(start); prefix = stream.read(offset - start)
        newline = prefix.rfind(b'\n')
        if start and newline < 0:
            return 'withheld', None, 'long_line_context_incomplete'
        start = start + newline + 1 if newline >= 0 else 0
        stream.seek(start); context = stream.read(min(total - start, end - start + 262144))
        relative_end = end - start
        next_newline = context.find(b'\n', relative_end)
        if next_newline >= 0: context = context[:next_newline + 1]
        elif start + len(context) < total:
            return 'withheld', None, 'long_line_context_incomplete'
        try:
            context.decode('utf-8')
        except UnicodeError:
            return 'binary', None, 'binary_body_not_sent; use explicit attachment analysis for this format'
        output, position, changed = [], start, False
        sensitive = re.compile(r'(?i)(password|passwd|api[_-]?key|secret|access[_-]?token|cookie|authorization)')
        for line in context.splitlines(keepends=True):
            line_end = position + len(line)
            left, right = max(position, offset), min(line_end, end)
            if left < right:
                text = line.decode('utf-8')
                if sensitive.search(text) or redact_text(text) != text:
                    output.append('[敏感行内容已隐藏]'); changed = True
                else:
                    output.append(line[left-position:right-position].decode('utf-8', errors='replace'))
            position = line_end
        return 'utf-8', ''.join(output), 'sensitive_spans_withheld' if changed else 'context_checked'

    def _read(self, scope, path, info, offset, limit):
        if not stat.S_ISREG(info.st_mode): raise ValueError('只允许普通文件，不读取设备、FIFO或socket')
        fd = self._open_fd(scope, path)
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if self._identity(opened) != self._identity(info): raise ValueError('文件在打开前发生变化')
            stream.seek(offset); raw = stream.read(limit)
            encoding, content, redaction = self._redacted_range(stream, path, offset, raw, info.st_size)
            # Keep the returned JSON comfortably below the broker's 128KiB cap.
            while content and len(json.dumps(content, ensure_ascii=False).encode()) >= 96000:
                raw = raw[:max(1, len(raw)//2)]
                encoding, content, redaction = self._redacted_range(stream, path, offset, raw, info.st_size)
            self._check_fd(scope, stream.fileno(), path)
            if self._identity(os.fstat(stream.fileno())) != self._identity(opened): raise ValueError('文件读取期间发生变化')
        eof = offset + len(raw) >= info.st_size
        return receipt('read', path=self.reference(path), encoding=encoding, content=content, offset=offset,
                       bytes_read=len(raw), next_offset=offset + len(raw), total_bytes=info.st_size,
                       sha256=hashlib.sha256(raw).hexdigest(), sha256_scope='file' if offset == 0 and eof else 'byte_range',
                       file_mtime_ns=info.st_mtime_ns, redacted=redaction != 'context_checked', redaction_status=redaction,
                       model_content_complete=encoding == 'utf-8' and redaction == 'context_checked',
                       eof=eof, unread_bytes=max(0, info.st_size - offset - len(raw)))

    def _directory_entries(self, scope, path, *, offset=0, deadline=None, max_entries=512):
        deadline = min(deadline or float('inf'), time.monotonic() + 3)
        fd = self._open_fd(scope, path, directory=True)
        try:
            entries, index, exhausted = [], 0, True
            with os.scandir(fd) as iterator:
                while True:
                    if time.monotonic() >= deadline or len(entries) >= max_entries:
                        exhausted = False; break
                    try: entry = next(iterator)
                    except StopIteration: break
                    index += 1
                    if index <= offset: continue
                    info = entry.stat(follow_symlinks=False)
                    entries.append((entry.name, info))
            self._check_fd(scope, fd, path)
            return dict(entries=entries, next_offset=offset+len(entries), eof=exhausted,
                        skipped_until=min(index, offset), scan_budget_seconds=3)
        finally:
            os.close(fd)

    def _list(self, scope, path, offset, limit):
        entries, used, next_offset = [], 0, offset
        source = self._directory_entries(scope, path, offset=offset)
        for index, (name, info) in enumerate(source['entries'], offset):
            candidate = path / name
            item = dict(name=name, path=self.reference(candidate), symlink=stat.S_ISLNK(info.st_mode),
                        authorized=self._allowed(scope, candidate.resolve()),
                        type='directory' if stat.S_ISDIR(info.st_mode) else 'file' if stat.S_ISREG(info.st_mode) else 'special', size=info.st_size)
            size = len(json.dumps(item, ensure_ascii=False).encode())
            if entries and used + size > limit: break
            entries.append(item); used += size; next_offset = index + 1
        exhausted = source['eof'] and len(entries) == len(source['entries'])
        return receipt('read' if exhausted else 'partial', path=self.reference(path), entries=entries, offset=offset, next_offset=next_offset,
                       pagination='directory_entry_index; directory changes can change ordering', eof=exhausted,
                       unread=0 if exhausted else 'more entries; continue next_offset or narrow directory if scan budget is exhausted')

    def _search(self, scope, path, info, query, offset, limit):
        if stat.S_ISREG(info.st_mode):
            result = self._read(scope, path, info, offset, limit)
            content = result.pop('content')
            result['query'] = redact_text(query)
            result['matches'] = ([dict(byte_range_offset=offset, text=content)] if result['encoding'] == 'utf-8' and query in content else [])
            result['search_scope'] = 'returned byte range only; query crossing chunk boundary may require overlap'
            return result
        if not stat.S_ISDIR(info.st_mode): raise ValueError('搜索只允许普通文件或目录')
        matches, unread, errors, index, used = [], [], [], 0, 0
        deadline, stack = time.monotonic() + 3, [(path, 0)]
        exhausted, next_offset = True, offset
        while stack:
            parent, directory_offset = stack.pop()
            if time.monotonic() >= deadline or len(errors) + len(unread) >= 64:
                exhausted = False; break
            try:
                page = self._directory_entries(scope, parent, offset=directory_offset, deadline=deadline, max_entries=128)
                entries = page['entries']
                if not page['eof']:
                    stack.append((parent, page['next_offset']))
            except (OSError, ValueError) as exc:
                errors.append(dict(path=self.reference(parent), error=str(exc))); continue
            for name, file_info in entries:
                candidate = parent / name
                if stat.S_ISDIR(file_info.st_mode):
                    stack.append((candidate, 0)); continue
                index += 1
                if index <= offset: continue
                if used >= limit or time.monotonic() >= deadline:
                    exhausted = False; break
                next_offset = index
                if stat.S_ISLNK(file_info.st_mode):
                    unread.append(dict(path=str(candidate), reason='symlink_directory_not_traversed')); continue
                if not self._allowed(scope, candidate):
                    unread.append(dict(path=self.reference(candidate), reason='permission_required')); continue
                try:
                    if not stat.S_ISREG(file_info.st_mode):
                        unread.append(dict(path=self.reference(candidate), reason='special_file_not_read')); continue
                    read = self._read(scope, candidate, file_info, 0, min(limit-used, MAX_READ))
                    used += read['bytes_read']
                    if read['encoding'] == 'utf-8' and query in read['content']:
                        matches.append(dict(path=read['path'], text=read['content'], sha256=read['sha256'], sha256_scope=read['sha256_scope'], offset=0))
                    if read['encoding'] != 'utf-8':
                        unread.append(dict(path=read['path'], reason='binary_not_text_searched' if read['encoding']=='binary' else read['redaction_status']))
                    if not read['eof']:
                        unread.append(dict(path=read['path'], offset=read['next_offset'], reason='remaining_file_bytes; search this path with byte offset'))
                except (OSError, ValueError) as exc:
                    errors.append(dict(path=self.reference(candidate), error=str(exc)))
            if not exhausted: break
        return receipt('read' if not errors else 'partial', path=self.reference(path), matches=matches, query=redact_text(query),
                       bytes_searched=used, files_considered=max(0,next_offset-offset), errors=errors, unread=unread,
                       next_offset=next_offset, pagination='directory file index; per-file remainder listed separately',
                       eof=exhausted and not unread and not errors, directory_exhausted=exhausted,
                       search_scope='literal text in listed byte ranges, no shell or regex execution')

    def _execution_summary(self, scope, offset, limit):
        from .report import SUMMARY_PATH
        root = self.data_root / 'executions'
        if not root.is_dir():
            return receipt('read', root=self.reference(root), total_records=0, verdict_counts={}, reasons={}, failed_reasons={},
                           records=[], errors=[], counts_complete=True, eof=True, unread=0)
        records, errors, counts, reasons, failed_reasons = [], [], Counter(), Counter(), Counter()
        deadline, used, processed, valid, omitted_reasons = time.monotonic()+3, 0, 0, 0, 0
        page = self._directory_entries(scope, root, offset=offset, deadline=deadline)
        directory_count = sum(stat.S_ISDIR(info.st_mode) for _, info in page['entries'])
        for name, info in page['entries']:
            if time.monotonic() >= deadline or len(errors) >= 64: break
            if not stat.S_ISDIR(info.st_mode):
                processed += 1; continue
            manifest = root/name/'manifest.json'
            try:
                manifest_info = manifest.stat()
                if manifest_info.st_size > 1024**2: raise ValueError('manifest过大')
                original = json.loads(self._bytes(scope, manifest, manifest_info, 0, 1024**2))
                if not isinstance(original, dict) or original.get('test_id') != manifest.parent.name: raise ValueError('manifest执行身份不符')
                summary_path = manifest.parent / SUMMARY_PATH
                summary_info = summary_path.stat()
                if summary_info.st_size > 4*1024**2: raise ValueError('汇总大于4MiB，须用read分块读取')
                raw = self._bytes(scope, summary_path, summary_info, 0, 4*1024**2)
                summary = json.loads(raw)
                if not isinstance(summary, dict): raise ValueError('汇总不是对象')
                verdict = summary.get('verdict') or {}
                if not isinstance(verdict, dict): raise ValueError('判定不是对象')
                outcome = verdict.get('status') or 'unknown'
                if verdict.get('criteria_version') == 'UNCONFIGURED' or outcome=='unconfigured': outcome='unconfigured'
                if outcome not in {'passed','failed','blocked','unconfigured'}: outcome='unknown'
                original_reasons = verdict.get('reasons') or []
                if not isinstance(original_reasons, list): raise ValueError('判定原因不是列表')
                safe_reasons = []
                for reason in original_reasons[:8]:
                    text = str(reason)
                    label = redact_text(text[:512])
                    if len(text)>512 or len(label.encode()) > 512:
                        label = label.encode()[:400].decode(errors='ignore') + ' …[原原因SHA256:' + hashlib.sha256(text.encode()).hexdigest() + ']'
                    safe_reasons.append(label)
                full_version = str(verdict.get('criteria_version') or 'unknown')
                version = redact_text(full_version[:256])
                record = dict(test_id=name, verdict=outcome, criteria_version=version, criteria_version_truncated=len(full_version)>256,
                    reasons=safe_reasons, omitted_reason_items=max(0,len(original_reasons)-8),
                    source_path=self.reference(summary_path), sha256=hashlib.sha256(raw).hexdigest(), file_mtime_ns=summary_info.st_mtime_ns)
                size = len(json.dumps(record, ensure_ascii=False).encode())
                if records and used + size > limit: break
                future_reasons = reasons + Counter(safe_reasons)
                future_failed = failed_reasons + (Counter(safe_reasons) if outcome=='failed' else Counter())
                if len(json.dumps(dict(records=records+[record],reasons=future_reasons,failed_reasons=future_failed,errors=errors),ensure_ascii=False).encode()) > 90000:
                    break
                records.append(record); used += size; valid += 1
                omitted_reasons += max(0,len(original_reasons)-8)
                counts[outcome] += 1
                for reason in safe_reasons: reasons[reason] += 1
                if outcome == 'failed':
                    for reason in safe_reasons: failed_reasons[reason] += 1
            except (OSError, ValueError, KeyError, TypeError) as exc:
                entry = dict(path=self.reference(manifest.parent), error=str(exc)[:512])
                if len(json.dumps(dict(records=records,reasons=reasons,failed_reasons=failed_reasons,errors=errors+[entry]),ensure_ascii=False).encode()) > 90000:
                    break
                counts['corrupt'] += 1; errors.append(entry)
            processed += 1
        eof = page['eof'] and processed == len(page['entries'])
        complete = eof and offset == 0
        return receipt('read' if eof and not errors else 'partial', root=self.reference(root),
                       total_records=directory_count if complete else None, history_visible_count=valid,
                       history_count_scope='safely parsed original manifest+summary in counter_range; no UI correction view',
                       verdict_counts=dict(counts), reasons=dict(reasons), failed_reasons=dict(failed_reasons), records=records, errors=errors,
                       reasons_complete=omitted_reasons==0, omitted_reason_items=omitted_reasons,
                       counter_range=dict(start_offset=offset,end_offset=offset+processed), counts_complete=complete,
                       next_offset=offset+processed, eof=eof, unread=0 if eof else 'more original summaries; continue next_offset',
                       statistics_scope='本机封存原始自动判定；仅本批计数，counts_complete才表示全量；人工核实和本地更正不改变计数，不作为平台正式统计')
