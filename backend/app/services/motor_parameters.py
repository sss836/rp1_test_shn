"""Immutable admin-confirmed templates, kept outside source and CSV storage."""
from datetime import datetime,timezone
import fcntl
import json
import os
from pathlib import Path
import tempfile
from app.core.errors import ApiError
from app.schemas.motor_parameters import validate,sha,applicable,canonical

class ParameterTemplates:
    def __init__(self,settings):
        if not settings.motor_parameters_enabled or not settings.motor_parameters_root:
            raise ApiError(503,'parameter_templates_disabled','参数模板库尚未启用')
        raw_root=Path(settings.motor_parameters_root).expanduser()
        if not raw_root.is_absolute():
            raise ApiError(503,'parameter_templates_path','模板持久目录须为仓库外绝对路径')
        root=raw_root.resolve()
        application_root=Path(__file__).resolve().parents[2]
        # Checkout: <repository>/backend/app/services; image: /app/app/services.
        # parents[3] is '/' in the image, which incorrectly rejects all stores.
        source=application_root.parent if application_root.name=='backend' else application_root
        if root==source or root in source.parents or source in root.parents:
            raise ApiError(503,'parameter_templates_path','模板持久目录须位于仓库外')
        for name in ('hmi_updates_root','motor_firmware_root','archive_root','archive_storage_root'):
            other=getattr(settings,name,'')
            if other:
                p=Path(other).expanduser().resolve()
                if root==p or root in p.parents or p in root.parents:raise ApiError(503,'parameter_templates_path','模板与其他文件库不能重叠')
        self.root=root;root.mkdir(parents=True,exist_ok=True)

    def publish(self,template,actor,request_id):
        try:t=validate(template,published=True)
        except (ValueError,TypeError,KeyError) as exc:raise ApiError(422,'parameter_template_invalid',str(exc)) from exc
        record={'template':t,'sha256':sha(t),'published_by':actor,'published_at':datetime.now(timezone.utc).isoformat(),'request_id':request_id}
        path=self.root/(t['template_id']+'.json')
        with (self.root/'.publish.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if path.exists() or path.is_symlink():
                old=self.read(path)
                if old['sha256']!=record['sha256']:raise ApiError(409,'parameter_template_immutable','已发布模板不可覆盖；请创建新编号与版本')
                return {'created':False,'record':old}
            # A model/version identifies one immutable baseline across retries.
            for existing in self.records():
                if (existing['template']['model'],existing['template']['version'])==(t['model'],t['version']):
                    raise ApiError(409,'parameter_version_conflict','型号与模板版本已发布，请使用新版本')
            fd,name=tempfile.mkstemp(prefix='.incoming-',dir=self.root)
            try:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(canonical(record));stream.flush();os.fsync(stream.fileno())
                os.replace(name,path)
                directory=os.open(self.root,os.O_RDONLY)
                try:os.fsync(directory)
                finally:os.close(directory)
            finally:
                if os.path.exists(name):os.unlink(name)
        return {'created':True,'record':record}

    def read(self,path):
        if path.is_symlink() or path.stat().st_size>256*1024:raise ApiError(409,'parameter_template_corrupt','模板文件无效')
        try:
            row=json.loads(path.read_bytes());t=validate(row['template'],published=True)
            if sha(t)!=row['sha256'] or path.stem!=t['template_id']:raise ValueError('哈希或编号不符')
            if not row['published_by'] or not row['published_at']:raise ValueError('缺少发布回执')
            return row
        except (ValueError,KeyError,TypeError) as exc:raise ApiError(409,'parameter_template_corrupt',str(exc)) from exc

    def records(self):return [self.read(p) for p in sorted(self.root.glob('*.json'))]
    def query(self,observation):return [r for r in self.records() if applicable(r['template'],observation)]
