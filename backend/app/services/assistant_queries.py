from datetime import datetime,timezone
from uuid import UUID
from sqlalchemy import text
from app.core.config import get_settings
from app.core.errors import ApiError
from app.repositories.archives import ArchiveRepository
from app.repositories.hmi_presence import HmiPresenceRepository
from app.repositories.setup import SetupRepository
from app.schemas.assistant_chat import CurveArguments,ProgressArguments
from app.services.archive_storage import ArchiveStorage
from app.services.curve_analysis import CurveAnalysisService


def query(session,tool,arguments,context):
    if tool not in {"get_curve_statistics","get_test_progress","list_hmi_stations"}:
        from app.services.assistant_business import query as business_query
        return business_query(session,tool,arguments,context)
    at=datetime.now(timezone.utc).isoformat()
    if tool=="get_curve_statistics":
        args=CurveArguments.model_validate(arguments)
        if context.get('selection_empty') and (not args.reference or args.reference==context.get('archive_id')):
            raise ApiError(422,"archive_selection_empty","当前视窗与所选归档没有重叠，请重新选择归档或区间，不能改用全程统计。")
        reference=args.reference or context.get("archive_id") or (context.get("object_id") if context.get("view") in {"archive","curves"} else None)
        if not reference: raise ApiError(422,"archive_required","请先选择归档及曲线区间，或提供明确的归档编号。")
        archive=ArchiveRepository(session).get(reference)
        settings=get_settings();service=CurveAnalysisService(ArchiveStorage(settings.archive_root),settings.archive_query_memory_mb)
        fields=args.model_dump(exclude_unset=True,exclude={"reference"})
        if not args.reference or args.reference==context.get('archive_id'):
            for key in ("metrics","subjects","start","end","stage","cycle"):
                if key not in fields and key in context: fields[key]=context[key]
        selection=service.select(archive,fields.get("metrics") or [],fields.get("subjects") or [],fields.get("start"),fields.get("end"),fields.get("stage"),fields.get("cycle"))
        data=service.statistics(selection)
        return {"type":"curve_statistics","title":"归档原始区间精确统计","source":f"/api/v1/archives/{archive['id']}/statistics","updated_at":at,
            "archive_updated_at":str(archive["updated_at"]),"original_sha256":archive.get("sha256"),"range":data["range"],"unit":"各序列unit字段",
            "scope":"归档原始数据检查；不表示审批通过或正式统计","data":data,"target":{"view":"archive","id":str(archive["id"])}}
    if tool=="get_test_progress":
        args=ProgressArguments.model_validate(arguments)
        identifier=args.campaign_id
        if identifier is None and context.get("view")=="planning" and context.get("object_id"): identifier=UUID(context["object_id"])
        campaigns=SetupRepository(session).campaigns()
        if identifier:
            campaigns=[c for c in campaigns if str(c["id"])==str(identifier)]
            if not campaigns: raise ApiError(404,"campaign_not_found","计划不存在或不可访问。")
        rows=[]
        for campaign in campaigns[:30]:
            counts=[dict(x) for x in session.execute(text("""SELECT e.status,count(*) AS count,max(e.received_at) AS updated_at
            FROM test.test_execution e JOIN test.test_campaign c ON c.id=e.campaign_id WHERE c.public_id=:id GROUP BY e.status ORDER BY e.status"""),{"id":campaign["id"]}).mappings()]
            rows.append({"id":str(campaign["id"]),"code":campaign["code"],"name":campaign["name"],"status":campaign["status"],"configured_items":campaign["case_count"],"executions":counts})
        return {"type":"test_progress","title":"测试计划与执行进展","source":"/api/v1/setup/campaigns + 当前权限下test_execution","updated_at":at,"range":"当前快照","unit":"项/次","data":{"items":rows,"truncated":len(campaigns)>30},"scope":"运行进展；未经审批的测试结果不据此进入正式统计","target":{"view":"planning","id":str(identifier) if identifier else None}}
    if arguments: raise ApiError(422,"invalid_station_query","工位状态查询不接受额外参数。")
    result=HmiPresenceRepository(session).list_stations(cursor=None,limit=50).model_dump(mode="json")
    return {"type":"station_status","title":"当前可见工位状态","source":"/api/v1/hmi/stations","updated_at":result["as_of_at"],"range":"最新心跳快照","unit":"工位；每项含last_seen_at","data":result,"scope":"在线快照，不是运行中完整曲线诊断","target":{"view":"stations","id":None}}
