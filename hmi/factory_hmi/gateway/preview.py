"""Loopback-only preview sessions, deliberately separate from control leases."""
from __future__ import annotations
import asyncio, json, secrets, threading, time
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from starlette.staticfiles import StaticFiles
from ..preview_assets import validate_preview_assets

PREVIEW_BATCH_FRAMES = 64
PREVIEW_BATCH_FRAME_BYTES = 256 * 1024

class PreviewSessions:
 def __init__(self,hub):self.hub=hub;self.tickets={};self.sessions={};self.lock=threading.Lock();self.sockets=0
 def cleanup(self):
  now=time.monotonic()
  self.tickets={k:v for k,v in self.tickets.items() if v[1]>now};self.sessions={k:v for k,v in self.sessions.items() if v[1]>now}
 def issue(self,key,run_id):
  with self.lock:
   self.cleanup()
   if len(self.tickets)+len(self.sessions)>=8:raise HTTPException(429,"预览会话已达8个，请关闭旧窗口后重试")
   ticket=secrets.token_urlsafe(32);self.tickets[ticket]=(key,time.monotonic()+30,run_id);return ticket
 def exchange(self,ticket):
  with self.lock:
   self.cleanup();value=self.tickets.pop(ticket,None)
   if value is None:raise HTTPException(401,"预览票据已过期，请从上位机重新打开")
   token=secrets.token_urlsafe(32);self.sessions[token]=(value[0],time.monotonic()+3600,value[2]);return token
 def runtime(self,token,*,include_run=False):
  with self.lock:
   self.cleanup();value=self.sessions.get(token or "")
  if value is None:raise HTTPException(401,"预览会话过期")
  with self.hub._lock:runtime=self.hub._sessions.get(value[0])
  if runtime is None:raise HTTPException(410,"原工位会话已关闭")
  service=runtime.controller;controller=getattr(service,"_controller",None)
  if controller is None and hasattr(service,"telemetry_hub"):controller=service
  if controller is None or controller.telemetry_hub.run_id!=value[2]:raise HTTPException(410,"执行已切换，请从上位机重新打开曲线")
  with self.lock:
   if token in self.sessions:self.sessions[token]=(value[0],time.monotonic()+3600,value[2])
  return (runtime,value[2]) if include_run else runtime
 def allowed_run(self,token):
  self.runtime(token)
  with self.lock:return self.sessions[token][2]

def local(request):
 host=urlsplit("http://"+request.headers.get("host","")).hostname
 if host not in {"127.0.0.1","localhost","::1"}:raise HTTPException(403,"仅允许本机预览")
 peer=request.client.host if request.client else ""
 if peer not in {"127.0.0.1","::1","testclient"}:raise HTTPException(403,"仅允许本机预览")
 origin=request.headers.get("origin")
 if origin and origin!=str(request.base_url).rstrip("/"):raise HTTPException(403,"预览来源不匹配")

def attach_preview(app,hub,require_token,owner_id):
 preview=PreviewSessions(hub);app.state.preview=preview
 root=Path(__file__).resolve().parents[1]/"resources/local-preview"
 def ensure_assets():
  try:validate_preview_assets(root)
  except RuntimeError as exc:raise HTTPException(503,str(exc)) from exc
 def runtime(request,*,include_run=False):
  local(request);return preview.runtime(request.cookies.get("rp1_preview"),include_run=include_run)
 def cache(rt):
  service=rt.controller;controller=getattr(service,"_controller",None)
  if controller is None and hasattr(service,"telemetry_hub"):controller=service
  if controller is None:raise HTTPException(409,"尚无测量缓存，请先在上位机准备任务")
  return controller.telemetry_hub
 @app.post("/api/v1/preview/launch")
 def launch(request:Request):
  local(request);require_token(request.headers.get("x-rp1-operator-token"));owner=owner_id(request,request.headers.get("x-rp1-client-id"));key=hub._key(owner)
  with hub._lock:rt=hub._sessions.get(key)
  if rt is None:raise HTTPException(409,"尚无当前执行，不会为预览创建设备会话")
  ensure_assets()
  c=cache(rt);ticket=preview.issue(key,c.run_id)
  return dict(url=str(request.base_url).rstrip("/")+"/preview/#ticket="+ticket,run_id=c.run_id)
 @app.post("/api/v1/preview/session")
 async def session(request:Request):
  local(request)
  if int(request.headers.get("content-length","0"))>512:raise HTTPException(413,"票据过大")
  body=bytearray()
  async for chunk in request.stream():
   if len(body)+len(chunk)>512:raise HTTPException(413,"票据过大")
   body.extend(chunk)
  try:ticket=json.loads(body).get("ticket","")
  except (ValueError,AttributeError):raise HTTPException(400,"无效票据")
  from fastapi.responses import JSONResponse
  response=JSONResponse({"read_only":True,"expires_in_s":3600});response.set_cookie("rp1_preview",preview.exchange(ticket),httponly=True,samesite="strict",max_age=3600,path="/")
  response.headers["Cache-Control"]="no-store";return response
 @app.get("/api/v1/preview/capabilities")
 def capabilities(request:Request):return cache(runtime(request)).capabilities()
 @app.get("/api/v1/preview/state")
 def state(request:Request,response:Response,after:int=0,limit:int=PREVIEW_BATCH_FRAMES):
  if after<0:raise HTTPException(422,"无效序号")
  try:
   rt,run_id=runtime(request,include_run=True)
   data=cache(rt).read(after=after,limit=limit,max_response_bytes=PREVIEW_BATCH_FRAME_BYTES)
   if data["run_id"]!=run_id:raise HTTPException(410,"执行已切换，请从上位机重新打开曲线")
   response.set_cookie('rp1_preview',request.cookies.get('rp1_preview'),httponly=True,samesite='strict',max_age=3600,path='/')
   response.headers['Cache-Control']='no-store';return data
  except ValueError as exc:raise HTTPException(422,str(exc))
 @app.get("/api/v1/preview/runs/{run_id}/curves")
 def historical(run_id:str,request:Request,metrics:str="",start:str="",end:str="",view:str="归档",window:str=""):
  from .local_curves import curves
  if len(metrics)>2000:raise HTTPException(422,"通道参数过长")
  rt=runtime(request)
  if run_id!=preview.allowed_run(request.cookies.get("rp1_preview")):raise HTTPException(403,"超出本次预览执行范围")
  return curves(rt.controller,run_id,metrics,start,end,view,window)
 @app.get("/api/v1/preview/runs/{run_id}/events")
 def events(run_id:str,request:Request,offset:int=0):
  from .local_curves import events
  rt=runtime(request)
  if run_id!=preview.allowed_run(request.cookies.get("rp1_preview")):raise HTTPException(403,"超出本次预览执行范围")
  return events(rt.controller,run_id,offset)
 @app.get("/api/v1/preview/runs/{run_id}/export.csv")
 def export(run_id:str,request:Request):
  rt=runtime(request)
  if run_id!=preview.allowed_run(request.cookies.get("rp1_preview")):raise HTTPException(403,"超出本次预览执行范围")
  manager=getattr(rt.controller,"report_manager",None)
  if manager is None:raise HTTPException(404,"尚无已封存记录")
  with manager.history_index.connect() as db:
   row=db.execute("SELECT payload FROM runs WHERE json_extract(payload,'$.execution_uuid')=? LIMIT 1",(run_id,)).fetchone()
  if row is None:raise HTTPException(409,"请等待记录封存；不会因下载触发测试或报告重算")
  path=manager.execution_dir(json.loads(row[0])["test_id"])/"telemetry/raw.csv"
  return FileResponse(path,filename="sealed-telemetry.csv",media_type="text/csv",headers={"Cache-Control":"no-store"})
 @app.post("/api/v1/preview/close")
 def close(request:Request):
  local(request)
  with preview.lock:preview.sessions.pop(request.cookies.get("rp1_preview"),None)
  return {"closed":True}
 @app.websocket("/ws/preview")
 async def websocket(ws:WebSocket):
  try:
   # WebSocket exposes URL instead of base_url; same-origin validation is explicit.
   host=ws.headers.get("host","");origin=ws.headers.get("origin","")
   if ws.client is None or ws.client.host not in {"127.0.0.1","::1","testclient"} or urlsplit("http://"+host).hostname not in {"localhost","127.0.0.1","::1"} or origin not in {"http://"+host,"https://"+host}:raise HTTPException(403,"仅允许同源本机预览")
   token=ws.cookies.get("rp1_preview")
   await asyncio.to_thread(preview.runtime,token)
   with preview.lock:
    if preview.sockets>=8:raise HTTPException(429,"预览连接已满")
    preview.sockets+=1
  except (HTTPException,RuntimeError):await ws.close(code=1008);return
  try:
   await ws.accept()
   after=0
   last_sent=None
   last_sent_at=0.
   def next_payload():
    rt,run_id=preview.runtime(token,include_run=True)
    hubcache=cache(rt)
    payload=hubcache.read(after=after,limit=PREVIEW_BATCH_FRAMES,max_response_bytes=PREVIEW_BATCH_FRAME_BYTES)
    if last_sent is not None and payload["stream_id"]!=last_sent[0]:
     payload=hubcache.read(limit=PREVIEW_BATCH_FRAMES,max_response_bytes=PREVIEW_BATCH_FRAME_BYTES)
    if payload["run_id"]!=run_id:raise HTTPException(410,"执行已切换，请从上位机重新打开曲线")
    return payload
   while True:
    payload=await asyncio.to_thread(next_payload)
    marker=(payload["stream_id"],payload["last_sequence"],payload["stale"])
    if marker!=last_sent or time.monotonic()-last_sent_at>=1.:
     # Retain no per-viewer queue. A slow viewer skips obsolete frames.
     await asyncio.wait_for(ws.send_json(payload),timeout=1.)
     last_sent=marker
     last_sent_at=time.monotonic()
    after=payload["last_sequence"]
    await asyncio.sleep(.1)
  except (WebSocketDisconnect,asyncio.TimeoutError,RuntimeError,HTTPException):pass
  finally:
   with preview.lock:preview.sockets-=1
 static=StaticFiles(directory=root/"assets",check_dir=False)
 @app.api_route("/preview/assets/{asset_path:path}",methods=["GET","HEAD"],include_in_schema=False)
 async def asset(request:Request,asset_path:str):
  local(request)
  # Look up files per request so a missing directory at startup cannot leave
  # a working index and ticket endpoint pointing to permanently absent routes.
  return await static.get_response(asset_path,request.scope)
 @app.get("/preview/")
 def page(request:Request):
  local(request);ensure_assets()
  return FileResponse(root/"index.html",headers={"Cache-Control":"no-store","Referrer-Policy":"no-referrer","Content-Security-Policy":"default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; img-src 'self' data:"})
