"""Explicit paired-window text repairs; no shell, controller or reload capability."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from datetime import datetime, timezone
from uuid import UUID
import fcntl
import yaml


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def safe_profile_change(original, replacement):
    """Only labels consumed by load_test_profiles(), or equivalent YAML formatting."""
    for raw in (original,replacement):
        if any(isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)) for token in yaml.scan(raw)):
            return False
    before, after = yaml.safe_load(original), yaml.safe_load(replacement)
    if not isinstance(before, dict) or not isinstance(after, dict):
        return False
    def normalized(value):
        value = json.loads(json.dumps(value))
        profiles = value.get('profiles')
        if not isinstance(profiles, list):
            raise ValueError('profiles must be a list')
        for profile in profiles:
            if not isinstance(profile, dict):
                raise ValueError('invalid profile')
            label = profile.pop('label', None)
            if not isinstance(label, str) or not 0 < len(label.strip()) <= 200:
                raise ValueError('display label must be nonempty and at most 200 characters')
        return value
    try:
        canonical = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',',':'), allow_nan=False)
        return canonical(normalized(before)) == canonical(normalized(after))
    except (ValueError, TypeError):
        return False


class DiagnosticRepairer:
    """Local role/OS/path checks remain mandatory after server-side confirmation.

    flock coordinates repair workers. Inode/size/mtime and byte-hash are checked
    again immediately before atomic replace. No automatic loading is performed.
    """
    def __init__(self, data_root, *, code_roots=None, profile_path=None):
        self.state = Path(data_root).resolve() / 'diagnostics' / 'repairs'
        project = Path(__file__).resolve().parents[2]
        self.roots = tuple(Path(p).resolve() for p in (code_roots or [project, Path('/opt/rp1-test-hmi')]))
        self.aliases = {'$PROJECT': self.roots[0].parent, '$INSTALL': Path('/opt/rp1-test-hmi').resolve()}
        self.profile = Path(profile_path or os.environ.get('RP1_RELIABILITY_PROFILES') or project / 'factory_hmi/config/reliability_platform.yaml').expanduser().absolute()
        self._lock = threading.RLock()

    def _path(self, value):
        for alias, root in self.aliases.items():
            if value.startswith(alias + '/'):
                value = str(root / value[len(alias) + 1:])
                break
        path = Path(value).expanduser()
        if not path.is_absolute(): path = self.roots[0] / path
        path = path.absolute()
        if path != path.resolve(strict=True):
            raise ValueError('符号链接或非规范路径不允许修改。')
        if any(part.startswith('.') or part.lower() in {'runtime', 'sessions', 'executions', 'logs', 'dist', 'backups', 'sync', 'resources', 'plc'} for part in path.parts):
            raise ValueError('运行记录、设备配置、凭据及构建目录不可修改。')
        return path

    def apply(self, proposal, *, role, installation_id, window_id, still_current=lambda: True):
        identifier = str(UUID(str(proposal['id'])))
        with self._lock:
            self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (self.state / 'repair.lock').open('a+b') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                receipt_path = self.state / (identifier + '.json')
                if receipt_path.exists():
                    previous = json.loads(receipt_path.read_text())
                    if previous.get('proposal_sha256') != proposal.get('proposal_sha256'):
                        raise ValueError('提议编号已存在且内容不同。')
                    return previous
                result = dict(status='rejected', source='paired_hmi_explicit_repair',
                              updated_at=datetime.now(timezone.utc).isoformat(), proposal_sha256=proposal.get('proposal_sha256'),
                              installation_id=installation_id, window_id=window_id, saved=False, loaded=False, current_process_updated=False, replacement_completed=False, durability_verified=False)
                try:
                    if proposal.get('state') != 'queued' or proposal.get('installation_id') != installation_id or proposal.get('window_id') != window_id:
                        raise ValueError('修复未确认或不属于当前配对窗口。')
                    expires = datetime.fromisoformat(str(proposal.get('expires_at','')).replace('Z','+00:00'))
                    if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
                        raise ValueError('修复提议已过期，请重新读取真实基线。')
                    if role not in {'SYSTEM_ADMIN', 'TEST_EXECUTOR'} or proposal.get('role') != role or not still_current():
                        raise ValueError('账号、角色或登录会话已变化。')
                    keys = ('id','job_id','actor_id','role','installation_id','window_id','kind','path','expected_sha256','original','replacement','reason')
                    digest = sha(json.dumps({key: proposal[key] for key in keys}, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8'))
                    if digest != proposal.get('proposal_sha256'):
                        raise ValueError('提议内容哈希不匹配，未执行。')
                    path = self._path(str(proposal['path']))
                    original, replacement = proposal['original'].encode('utf-8'), proposal['replacement'].encode('utf-8')
                    if max(len(original), len(replacement)) > 512 * 1024 or b'\0' in replacement:
                        raise ValueError('仅允许512KiB以内的UTF-8文本。')
                    if sha(original) != proposal['expected_sha256']:
                        raise ValueError('提议原始内容哈希不匹配。')
                    if proposal.get('kind') == 'config':
                        if path != self.profile.resolve(strict=True) or not safe_profile_change(proposal['original'], proposal['replacement']):
                            raise ValueError('仅允许现有平台快捷项显示标签或等价YAML格式修复；身份、控制和保护字段禁止修改。')
                    elif proposal.get('kind') == 'code':
                        if role != 'SYSTEM_ADMIN' or path.suffix != '.py' or not any(path.is_relative_to(root / 'factory_hmi') for root in self.roots):
                            raise ValueError('代码维护仅限管理员及当前上位机Python源码。')
                        compile(proposal['replacement'], str(path), 'exec')
                    else:
                        raise ValueError('未知修复类型。')
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'rb') as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ValueError('仅允许无硬链接普通文件。')
                        raw = source.read(512 * 1024 + 1)
                    result['before_sha256'] = sha(raw)
                    backup = self.state / (identifier + '.before')
                    recovered = raw == replacement and backup.is_file() and backup.read_bytes() == original
                    if recovered:
                        result.update(status='applied', saved=True, replacement_completed=True, after_sha256=sha(raw), after_hash_verified=True, backup_path=str(backup), before_sha256=sha(original), message='已从本地备份及实际目标哈希恢复保存回执；未加载或重启，未重新确认目录持久化。')
                    elif raw != original:
                        result.update(status='conflict', message='文件已被其他修改更新，未覆盖；请重新读取并生成提议。')
                    else:
                        if not os.access(path, os.W_OK) or not os.access(path.parent, os.W_OK):
                            raise PermissionError('操作系统拒绝写入；不会提升权限。')
                        if backup.exists():
                            if backup.read_bytes() != raw: raise ValueError('已有备份与原始基线不一致，未修改。')
                        else:
                            with backup.open('xb') as stream:
                                os.chmod(backup, 0o600); stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                        tmp_fd, temporary = tempfile.mkstemp(prefix='.rp1-repair-', dir=path.parent)
                        try:
                            with os.fdopen(tmp_fd, 'wb') as stream:
                                os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode)); stream.write(replacement); stream.flush(); os.fsync(stream.fileno())
                            current = path.stat(follow_symlinks=False)
                            identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                            check_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                            with os.fdopen(check_fd, 'rb') as check:
                                unchanged = identity(current) == identity(info) and check.read(512 * 1024 + 1) == original
                            if not unchanged or not still_current():
                                result.update(status='conflict', message='提交前文件或登录状态发生变化，未覆盖。')
                            else:
                                os.replace(temporary, path)
                                # Replacement is an irreversible fact even if a
                                # later directory fsync/close/check fails.
                                result.update(status='applied', saved=True, replacement_completed=True, backup_path=str(backup),
                                              after_hash_verified=False, message='文件已替换，正在核对保存后哈希与持久化。')
                                verify_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                                with os.fdopen(verify_fd, 'rb') as verify:
                                    actual = verify.read(512 * 1024 + 1)
                                result.update(after_sha256=sha(actual), after_hash_verified=actual == replacement)
                                if actual != replacement:
                                    result.update(status='conflict', message='文件已替换，但保存后又检测到不同内容；未重新写入，请核对当前文件与备份。')
                                else:
                                    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                                    try: os.fsync(directory)
                                    finally: os.close(directory)
                                    result.update(durability_verified=True,
                                                  message='已保存并备份，未加载到当前实例，未自动重启或操作设备；请按正常维护流程核对生效版本。')
                        finally:
                            if os.path.exists(temporary): os.unlink(temporary)
                except (OSError, ValueError, KeyError, SyntaxError, yaml.YAMLError) as exc:
                    if result.get('replacement_completed'):
                        result.update(status='applied' if result.get('after_hash_verified') else 'failed', saved=True, durability_verified=False,
                                      message='文件已替换；' + ('保存后哈希已核对，' if result.get('after_hash_verified') else '保存后哈希尚未核对，') + '目录持久化或后续检查未完成：' + str(exc)[:400] + '。未重新写入、加载或重启。')
                    else:
                        result.update(status='failed' if isinstance(exc, OSError) else 'rejected', message=str(exc)[:500])
                temporary_receipt = receipt_path.with_suffix('.tmp')
                durable_result = dict(result, receipt_durable=True)
                try:
                    with temporary_receipt.open('w', encoding='utf-8') as stream:
                        os.chmod(temporary_receipt, 0o600); json.dump(durable_result, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
                    os.replace(temporary_receipt, receipt_path)
                    return durable_result
                except OSError as exc:
                    # A receipt failure cannot erase an already observed save.
                    # The bridge's separate pre-written intent/outbox can retain
                    # this exact result or reconstruct it by read-only hashes.
                    result.update(receipt_durable=False, message=result.get('message','') + ' 本机审计回执未持久保存：' + str(exc)[:300] + '；请核对云端结算状态，勿重复写入。')
                    return result
