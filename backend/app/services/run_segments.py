"""Resumable segment bytes; existing raw-CSV worker and approval gates stay authoritative."""
import hashlib,json,os,shutil
from pathlib import Path
from sqlalchemy import text
from app.core.errors import ApiError
from app.services.archive_storage import ArchiveStorage,sha256_file,fsync_directory

class RunSegments:
 def __init__(self,session,storage):self.session=session;self.storage=storage
 def metadata(self,archive):
  row=self.session.execute(text("SELECT m.payload,m.sha256 FROM integration.archive_run_manifest m JOIN integration.execution_archive a ON a.id=m.archive_id WHERE a.public_id=:id"),{"id":archive["id"]}).mappings().one_or_none()
  if row is None:raise ApiError(404,"segment_manifest_missing","尚未登记分段清单")
  return dict(row)
 def register(self,archive,payload):
  serialized=json.dumps(payload,ensure_ascii=False)
  digest=self.session.execute(text("SELECT encode(digest(convert_to(CAST(:p AS jsonb)::text,'UTF8'),'sha256'),'hex')"),{"p":serialized}).scalar_one()
  self.session.execute(text("SELECT integration.register_run_manifest(:id,CAST(:p AS jsonb),:hash)"),{"id":archive["id"],"p":serialized,"hash":digest})
  return self.status(archive)
 def status(self,archive):
  metadata=self.metadata(archive)
  rows=self.session.execute(text("SELECT s.sequence,s.received_bytes FROM integration.archive_run_segment s JOIN integration.execution_archive a ON a.id=s.archive_id WHERE a.public_id=:id ORDER BY s.sequence"),{"id":archive["id"]}).mappings()
  return {"manifest_sha256":metadata["sha256"],"segments":[dict(x) for x in rows]}
 def path(self,archive,n):
  if not 0<=n<4096:raise ApiError(422,"segment_index","分段序号无效")
  root=self.storage.directory(archive["execution_uuid"])/"segments"
  if root.is_symlink():raise ApiError(409,"segment_storage_conflict","分段目录无效")
  root.mkdir(exist_ok=True,mode=0o750);p=root/f"{n:06d}.csv"
  if p.is_symlink():raise ApiError(409,"segment_storage_conflict","分段文件无效")
  return p
 def append(self,archive,n,offset,body):
  metadata=self.metadata(archive)["payload"];parts=metadata["segments"]
  if n>=len(parts):raise ApiError(422,"segment_index","分段不存在")
  size=parts[n]["size_bytes"];states=self.status(archive)["segments"];received=states[n]["received_bytes"]
  if not body or offset<0 or offset+len(body)>size:raise ApiError(422,"segment_range","分段内容越界")
  path=self.path(archive,n)
  with path.open("r+b" if path.exists() else "w+b") as handle:
   physical=os.fstat(handle.fileno()).st_size
   if physical<received:raise ApiError(409,"segment_lost","已确认分段字节缺失")
   if physical>received:handle.truncate(received)
   if offset<received:
    handle.seek(offset)
    if offset+len(body)<=received and handle.read(len(body))==body:return self.status(archive)
    raise ApiError(409,"segment_conflict","重试块与已保存内容冲突")
   if offset!=received:raise ApiError(409,"segment_offset",f"应从 {received} 继续")
   handle.seek(offset);handle.write(body);handle.flush();os.fsync(handle.fileno())
  received+=len(body)
  if received==size and sha256_file(path)!=parts[n]["sha256"]:
   # Preserve corrupt bytes for investigation, and leave the last confirmed offset unchanged.
   raise ApiError(409,"segment_hash","分段哈希不一致，未发布")
  self.session.execute(text("SELECT integration.update_run_segment(:id,:n,:received)"),{"id":archive["id"],"n":n,"received":received})
  return self.status(archive)
 def materialize(self,archive):
  meta=self.metadata(archive);parts=meta["payload"]["segments"];states=self.status(archive)["segments"]
  if len(states)!=len(parts) or any(s["received_bytes"]!=p["size_bytes"] for s,p in zip(states,parts)):raise ApiError(409,"segments_incomplete","尚有缺失分段，不能封存整个执行")
  destination=self.storage.path(archive["execution_uuid"],"upload.part");temporary=destination.with_name("segments-merge.tmp")
  if archive["upload_state"]=="COMPLETE":
   existing=self.storage.path(archive["execution_uuid"],"raw.csv")
   if not existing.exists():existing=destination
   if existing.exists() and existing.stat().st_size==archive["size_bytes"] and sha256_file(existing)==archive["sha256"]:return
  header=None
  try:
   with temporary.open("wb") as out:
    for part in parts:
     source=self.path(archive,part["sequence"])
     if source.stat().st_size!=part["size_bytes"] or sha256_file(source)!=part["sha256"]:raise ApiError(409,"segment_hash","分段已损坏，禁止发布")
     with source.open("rb") as handle:
      line=handle.readline(16384)
      if not line.endswith(b'\n'):raise ApiError(422,"segment_header","分段表头过长或不完整")
      if header is None:header=line;out.write(header)
      elif line!=header:raise ApiError(422,"segment_schema","分段测量通道改变")
      shutil.copyfileobj(handle,out,1024**2)
    out.flush();os.fsync(out.fileno())
   if temporary.stat().st_size!=archive["size_bytes"] or sha256_file(temporary)!=archive["sha256"]:raise ApiError(409,"logical_hash","整次执行的字节数或哈希不一致")
   if destination.exists() and archive["received_bytes"]>0:raise ApiError(409,"raw_upload_exists","已有原始上传，不覆盖原件")
   os.replace(temporary,destination);fsync_directory(destination.parent)
  finally:temporary.unlink(missing_ok=True)
