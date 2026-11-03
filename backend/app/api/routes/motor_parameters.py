"""Publication is an administrator action; use is open to human operators."""
from fastapi import APIRouter,Response,Query
from typing import Annotated
from app.api.routes.hmi_updates import Human,Operator,Admin
from app.core.config import get_settings
from app.schemas.common import DataEnvelope
from app.services.motor_parameters import ParameterTemplates
Target=Annotated[str,Query(min_length=1,max_length=64,pattern=r'^[0-9A-Za-z][0-9A-Za-z._+\-]*$')]
router=APIRouter(prefix='/api/v1/hmi/motor-parameters',tags=['motor-parameters'])

@router.post('/templates',status_code=201)
def publish(payload:dict,actor:Human,_:Admin,response:Response):
    result=ParameterTemplates(get_settings()).publish(payload,str(actor.public_id),actor.request_id)
    if not result['created']:response.status_code=200
    response.headers['Cache-Control']='no-store'
    return DataEnvelope(data=result)

@router.get('/templates')
def query(_:Human,actor:Operator,response:Response,model:Target,hardware:Target,protocol:Target,software:Target,raw_firmware:Target):
    result=ParameterTemplates(get_settings()).query(dict(status='known',model=model,hardware=hardware,protocol=protocol,software=software,raw_firmware=raw_firmware))
    response.headers['Cache-Control']='no-store'
    return DataEnvelope(data={'templates':result})
