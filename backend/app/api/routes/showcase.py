from typing import Annotated
from fastapi import APIRouter,Depends,Request,Response
from pydantic import BaseModel,Field
from app.core.auth import RequestActor,require_roles
from app.core.db import DbSession,PublicDbSession
from app.core.config import get_settings
from app.core.errors import ApiError
from app.schemas.common import DataEnvelope
from app.services import showcase
from app.services.auth import AuthService
from app.repositories.security import SecurityRepository
router=APIRouter(prefix='/api/v1/showcase',tags=['showcase'])
Presenter=Annotated[RequestActor,Depends(require_roles('SYSTEM_ADMIN','TEST_EXECUTOR'))]
class Selection(BaseModel):
 topic_id:str=Field(min_length=1,max_length=100)
@router.get('/topics')
def topics(actor:Presenter):
 return DataEnvelope(data=[{k:v.get(k) for k in ('id','alias','domain')} for v in showcase.topics()])
@router.post('/session')
def create(payload:Selection,request:Request,response:Response,actor:Presenter,session:DbSession):
 topic=next((t for t in showcase.topics() if t.get('id')==payload.topic_id),None)
 if topic is None:raise ApiError(403,'showcase_topic','主题尚未获准展示')
 settings=get_settings();original=request.cookies.get(settings.auth_cookie_name,'')
 if not original:raise ApiError(403,'showcase_login','讲解员须使用真实登录会话')
 token=showcase.issue(showcase.project(session,topic),original,topic["id"])
 response.set_cookie('rp1_showcase',token,max_age=3600,httponly=True,samesite='strict',secure=settings.secure_auth_cookie,path='/')
 response.delete_cookie(settings.auth_cookie_name,path='/');response.delete_cookie(settings.auth_csrf_cookie_name,path='/')
 return DataEnvelope(data={'read_only':True,'url':'/showcase?domain='+topic['domain']})
@router.get('/view')
def view(request:Request,session:PublicDbSession):
 grant=showcase.read(request.cookies.get('rp1_showcase'))
 principal=AuthService(get_settings(),SecurityRepository(session)).resolve(grant['original_token'])
 if principal is None or principal.role not in {'SYSTEM_ADMIN','TEST_EXECUTOR'} or principal.must_change_password:raise ApiError(401,'showcase_expired','讲解员授权已失效')
 topic=next((t for t in showcase.topics() if t.get('id')==grant['topic_id']),None)
 if topic is None:raise ApiError(403,'showcase_revoked','展示主题授权已撤销')
 from sqlalchemy import text
 session.execute(text("SELECT iam.set_request_context(:id,'showcase-read','read approved projection')"),{'id':principal.id})
 return DataEnvelope(data=showcase.project(session,topic))
