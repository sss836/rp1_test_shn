"""Typed read adapters over the platform's real account/RLS repositories."""
from datetime import datetime,timezone
import time
from uuid import UUID
from pathlib import PurePosixPath
from sqlalchemy import text
from app.core.config import get_settings
from app.core.errors import ApiError
from app.repositories.read_models import ReadModelRepository
from app.repositories.setup import SetupRepository
from app.repositories.archives import ArchiveRepository
from app.services.archive_storage import ArchiveStorage
from app.services.curve_analysis import CurveAnalysisService
from app.services.assistant_privacy import public_evidence
from app.services.assistant_parts import known_part, canonical_part

TOOLS=("search_business","read_business_object","get_runtime_summary","compare_joint_torque","search_project_source","read_project_source","search_web","read_web_page","search_my_history")
RESOURCES=("parts","assets","archives","cases","programs","campaigns","stations","live_executions","test_case_durations","data_sources")

def wire(value):
    return value.model_dump(mode="json") if hasattr(value,"model_dump") else value

def card(kind,title,data,source,unit="资料",scope="当前登录账号可读取的业务资料"):
    return {"type":kind,"title":title,"data":public_evidence(wire(data)),"source":source,"unit":unit,
            "updated_at":datetime.now(timezone.utc).isoformat(),"range":"工具回执所示范围","scope":scope}

def part_codes(session,part):
    if not part:return []
    parts=[dict(r) for r in session.execute(text("SELECT part_code AS code,name,asset_kind FROM catalog.test_target_part WHERE enabled ORDER BY part_code")).mappings()]
    needle=(known_part(part) or part).casefold()
    return [r["code"] for r in parts if needle==r["code"].casefold() or needle in r["name"].casefold()]

def search(session,args):
    resource=args.get("resource","assets");q=str(args.get("query","")).strip()[:200]
    repo=ReadModelRepository(session);setup=SetupRepository(session)
    limit=min(50,max(1,int(args.get("limit",20))));cursor=args.get("cursor")
    kind=args.get("asset_kind")
    if kind not in (None,"MODULE","WHOLE_MACHINE"):raise ApiError(422,"query_kind","对象类型无效。")
    supported={
        "assets":{"asset_kind","target_part_code"},"archives":{"execution_id"},
        "cases":{"asset_kind","target_part_code","published_only","enabled","include_contexts"},
        "test_case_durations":{"asset_kind","asset"},"parts":{"asset_kind","target_part_code"},
        "campaigns":{"asset_kind"},"programs":set(),"stations":set(),
        "live_executions":set(),"data_sources":set(),
    }
    unused={key for key,value in args.items() if key not in {"resource","query","cursor","limit"} and value is not None and key not in supported.get(resource,set())}
    if unused:raise ApiError(422,"unsupported_query_filter","此类资料不支持这些过滤字段："+"、".join(sorted(unused)))
    if any(args.get(key) is not None and type(args[key]) is not bool for key in ("published_only","enabled","include_contexts")):
        raise ApiError(422,"query_boolean","启用及发布过滤必须为布尔值。")
    args=dict(args)
    if args.get("target_part_code"):args["target_part_code"]=canonical_part(args["target_part_code"])
    alias=known_part(q) if resource in {"assets","cases","parts"} else None
    if alias:
        if args.get("target_part_code") and args['target_part_code']!=alias:raise ApiError(422,"conflicting_part_filters","检索词与指定部位不一致，请明确要查询的部位。")
        args["target_part_code"]=alias;q=""
    if resource=="assets":
        result=wire(repo.list_assets(asset_kind=kind,target_part_code=args.get("target_part_code"),status=None,search=q or None,limit=limit,cursor=cursor))
        return {**result,"applied_filters":{k:v for k,v in {"target_part_code":args.get("target_part_code"),"asset_kind":kind,"query":q}.items() if v},"returned_count":len(result["items"])}
    if resource=="archives":
        result=ArchiveRepository(session).list(query=q or None,execution_id=UUID(args["execution_id"]) if args.get("execution_id") else None,before=int(cursor) if cursor else None,limit=limit)
        # Explicit projection: never send raw verification/configuration payloads.
        fields=("id","archive_code","execution_id","execution_code","test_name","actual_object","station_label","processing_state","origin","submission_status","updated_at","sha256","classification_state")
        return {"items":[{k:r.get(k) for k in fields} for r in result["items"]],"next_cursor":result["next_cursor"]}
    if resource=="live_executions":return wire(repo.live_executions())
    if resource=="test_case_durations":
        return wire(repo.test_case_durations(asset_kind=kind,asset=args.get("asset"),test_case=None,status=None,search=q or None,limit=limit,cursor=cursor))
    if resource=="data_sources":return wire(repo.data_sources_health())
    if resource=="cases":rows=setup.catalog()
    elif resource=="campaigns":rows=setup.campaigns()
    elif resource=="parts":rows=[dict(r) for r in session.execute(text("SELECT part_code AS code,name,asset_kind FROM catalog.test_target_part WHERE enabled ORDER BY part_code")).mappings()]
    elif resource=="programs":rows=[dict(r) for r in session.execute(text("SELECT public_id AS id,program_code AS code,name,status FROM test.test_program ORDER BY id DESC")).mappings()]
    elif resource=="stations":
        result=setup.stations(cursor=UUID(cursor) if cursor else None,limit=limit)
        if q:result["items"]=[r for r in result["items"] if q.casefold() in (str(r.get("name",""))+" "+str(r.get("code",""))).casefold()]
        return result
    else:raise ApiError(422,"unknown_business_resource","未知业务资源。")
    filters={}
    target=args.get("target_part_code")
    alias=known_part(q) if resource in {"cases","parts"} else None
    if alias:
        if target and canonical_part(target)!=alias:raise ApiError(422,"conflicting_part_filters","检索词与指定部位不一致，请明确要查询的部位。")
        target=alias;q=""
    if target:
        target=canonical_part(target);filters["target_part_code"]=target
        rows=[r for r in rows if (r.get("code") if resource=="parts" else r.get("target_part_code"))==target]
    if kind:
        filters["asset_kind"]=kind;rows=[r for r in rows if r.get("asset_kind")==kind]
    if resource=="cases":
        if args.get("published_only"):
            filters["published_only"]=True;rows=[r for r in rows if r.get("version_id")]
        if args.get("enabled") is not None:
            filters["enabled"]=bool(args["enabled"]);rows=[r for r in rows if bool(r.get("enabled"))==args["enabled"]]
        # List evidence is deliberately concise. Version details/steps remain
        # available through read_business_object(cases,id).
        rows=[{k:v for k,v in r.items() if k!="procedure_spec"} for r in rows]
    if q:
        filters["query"]=q;rows=[r for r in rows if q.casefold() in (str(r.get("name",""))+" "+str(r.get("code",""))).casefold()]
    offset=int(cursor or 0)
    if offset<0:raise ApiError(422,"invalid_cursor","游标无效。")
    items=rows[offset:offset+limit]
    if resource=="cases" and args.get("include_contexts") and items:
        counts=session.execute(text("""
            SELECT tc.public_id AS case_id,count(ci.id) FILTER (WHERE c.id IS NOT NULL AND a.id IS NOT NULL) AS visible_contexts,
                   count(ci.id) FILTER (WHERE a.id IS NOT NULL AND c.status IN ('PLANNED','ACTIVE') AND ci.status<>'CANCELLED') AS active_plan_contexts
            FROM catalog.test_case tc
            LEFT JOIN catalog.test_case_version v ON v.test_case_id=tc.id
            LEFT JOIN test.campaign_coverage_item ci ON ci.test_case_version_id=v.id
            LEFT JOIN test.test_campaign c ON c.id=ci.campaign_id
            LEFT JOIN test.asset a ON a.id=ci.target_asset_id
            WHERE tc.public_id=ANY(CAST(:ids AS uuid[]))
            GROUP BY tc.public_id
        """),{"ids":[str(r['id']) for r in items]}).mappings()
        by_id={str(r['case_id']):{'visible_contexts':r['visible_contexts'],'active_plan_contexts':r['active_plan_contexts']} for r in counts}
        items=[{**r,'plan_contexts':by_id.get(str(r['id']),{'visible_contexts':0,'active_plan_contexts':0})} for r in items]
        filters['include_contexts']=True
    result={"items":items,"next_cursor":str(offset+limit) if len(rows)>offset+limit else None,
            "total":len(rows),"applied_filters":filters,"returned_count":len(rows[offset:offset+limit]),
            "details_hint":"用例目录存在不等于上位机可选；候选来自当前账号可读取计划的关联上下文profiles，再按模块和样品部位过滤。include_contexts=true可查询当前账号可见计划关联数量（非远端实际加载状态）。源码入口：app.py的_refresh_case_choices，reliability_platform.py的profiles_from_setup。" if resource=="cases" else ""}
    if resource=="cases" and args.get("include_contexts"):
        # Fetch the two fixed rule entry points from the same versioned source
        # tool used by the model. These are evidence, not hand-written diagnoses.
        from app.services.assistant_sources import source_query
        evidence=[]
        try:
            for path,symbol in [("hmi/factory_hmi/desktop/app.py","def _refresh_case_choices("),("hmi/factory_hmi/sync/reliability_platform.py","def profiles_from_setup(")]:
                matches=source_query({"query":symbol,"directory":str(PurePosixPath(path).parent)})
                hit=next((m for m in matches.get('matches',[]) if m['path']==path),None)
                if hit:evidence.append(source_query({'path':path,'start':hit['line']}))
            result['availability_sources']=evidence
            result['client_state_verified']=False
        except (ApiError,OSError):
            result['availability_sources']=[]
            result['rule_warning']='当前源码规则未核验，不能断言远端候选生成方式；平台关联计数仍有效。'
    return result

def runtime_summary(session,args,context):
    repo=ReadModelRepository(session)
    if args.get("asset"):
        asset=wire(repo.get_asset(args["asset"]))
        return card("runtime_summary","样品运行时长",{"asset":asset,"hours":{k:v/3600 for k,v in asset["asset"]["durations"].items()}},"/api/v1/assets/"+args["asset"],"秒；hours字段为小时")
    data=wire(repo.dashboard_home())
    part=args.get("part") or context.get("target_part_code")
    codes=part_codes(session,part)
    if part and not codes:return card("runtime_summary","部位检索无匹配",{"items":[],"status":"not_found","part":part},"/api/v1/dashboard/home")
    rows=[]
    for group in data["groups"]:
        for item in group["objects"]:
            if codes and item["target_part_code"] not in codes:continue
            if args.get("asset_kind") and item["asset_kind"]!=args["asset_kind"]:continue
            totals=item["part_durations"]
            rows.append({**item,"hours":{k: v/3600 for k,v in totals.items()}})
    return card("runtime_summary","平台台账运行时长",{"items":rows,"freshness":data.get("freshness"),
        "meaning":"累计总时长、有效暴露、排除、待确认和当前执行时长分别展示；部位多样品合计不是单台连续运行时间。"},"/api/v1/dashboard/home","原始秒；hours为小时")

def compare_torque(session,args,context):
    if context.get("selection_empty") and (not args.get("archives") or context.get("archive_id") in args["archives"]):
        raise ApiError(422,"archive_selection_empty","当前选择没有重叠数据，请明确比较对象或归档范围。")
    repo=ArchiveRepository(session); references=args.get("archives") or ([context["archive_id"]] if context.get("archive_id") else [])
    selected=not args.get("archives") or references==[context.get("archive_id")]
    range_start=args.get("start",context.get("start") if selected else None)
    range_end=args.get("end",context.get("end") if selected else None)
    truncated=False;excluded=[];rows=[]
    if references:
        if len(references)>8:raise ApiError(422,"torque_scope_large","一次最多比较8份归档，可分批明确范围。")
        archives=[repo.get(x) for x in references]
    else:
        part=args.get("part") or context.get("target_part_code")
        codes=part_codes(session,part)
        if part and not codes:return card("joint_torque_comparison","没有匹配部位",{"status":"not_found","items":[]},"/api/v1/archives")
        ids=session.execute(text("""SELECT a.public_id FROM integration.execution_archive a
          JOIN LATERAL integration.archive_public_info(a.id) info ON true
          LEFT JOIN test.test_execution e ON e.public_id=info.execution_public_id
          LEFT JOIN test.asset asset ON asset.id=e.asset_id
          LEFT JOIN test.module_profile mp ON mp.asset_id=asset.id
          WHERE a.processing_state='READY'
          AND (:raw_scope OR info.submission_status='APPROVED')
          AND (:all_parts OR mp.target_part_code=ANY(CAST(:parts AS text[])))
          AND (:all_assets OR asset.public_id::text=:asset OR asset.asset_code=:asset)
          ORDER BY a.id DESC LIMIT 9"""),{"raw_scope":args.get("scope")=="raw","all_parts":not codes,"parts":codes,"all_assets":not args.get("asset"),"asset":str(args.get("asset") or "")}).scalars().all()
        truncated=len(ids)>8;archives=[repo.get(x) for x in ids[:8]]
    settings=get_settings();service=CurveAnalysisService(ArchiveStorage(settings.archive_root),settings.archive_query_memory_mb)
    started=time.monotonic()
    for archive in archives:
        if time.monotonic()-started>20:truncated=True;excluded.append({"archive_id":str(archive["id"]),"reason":"本次计算时间预算已到"});continue
        if args.get("scope","approved")!="raw" and archive.get("submission_status")!="APPROVED":
            excluded.append({"archive_id":str(archive["id"]),"reason":"未审批归档不进入正式比较"});continue
        catalog=(archive.get("manifest") or {}).get("catalog",[])
        torque=[x for x in catalog if "torque" in x.get("metric","").casefold() and x.get("unit","").replace(" ","") in {"Nm","N·m","N*m","N.m"}]
        if not torque:
            excluded.append({"archive_id":str(archive["id"]),"reason":"没有可比的Nm力矩原始曲线"});continue
        # Raw exact statistics, chunked by the service's 32-series boundary.
        for offset in range(0,len(torque),32):
            chosen=torque[offset:offset+32]
            metrics=list(dict.fromkeys(x["key"] for x in chosen))
            selection=service.select(archive,metrics,[],range_start,range_end)
            stat=service.statistics(selection)
            for item in stat["series"]:
                values=[v for v in (item.get("min"),item.get("max")) if isinstance(v,(int,float))]
                if values:
                    rows.append({"archive_id":str(archive["id"]),"archive_code":archive["archive_code"],
                                 "subject":item["subject"],"metric":item["metric"],"unit":"Nm","peak_abs":max(map(abs,values)),
                                 "min":item["min"],"max":item["max"],"range":stat["range"],"sha256":archive.get("sha256"),
                                 "submission_status":archive.get("submission_status")})
    rows.sort(key=lambda r:r["peak_abs"],reverse=True)
    return card("joint_torque_comparison","关节力矩绝对峰值比较",{"items":rows,"largest_in_scanned_scope":rows[0] if rows else None,
        "archives_scanned":len(archives),"excluded":excluded,"complete":not truncated and not excluded,
        "truncated":truncated,"status":"partial" if truncated or excluded else "ok" if rows else "no_data",
        "meaning":"仅比较列出的归档、区间和关节；存在排除项或截断时不能宣称全平台最大。"},
        "/api/v1/archives/{id}/statistics","Nm", "原始资料检查" if args.get("scope")=="raw" else "已审批归档正式比较")

def query(session,tool,args,context):
    if not isinstance(args,dict) or len(str(args))>12000:raise ApiError(422,"query_invalid","查询参数无效。")
    if tool=="search_business":return card("business_search","业务资料检索",search(session,args),"/api/v1/"+str(args.get("resource","assets")))
    if tool=="get_runtime_summary":return runtime_summary(session,args,context)
    if tool=="compare_joint_torque":return compare_torque(session,args,context)
    if tool=="read_business_object":
        resource=args.get("resource");identifier=args.get("id")
        if not identifier:raise ApiError(422,"object_required","请选择检索结果中的业务对象。")
        repo=ReadModelRepository(session);setup=SetupRepository(session)
        if resource=="assets":data=repo.get_asset(identifier)
        elif resource=="executions":data=repo.get_execution(identifier)
        elif resource=="cases":data=setup.case_versions(UUID(identifier))
        elif resource=="asset_executions":data=repo.asset_executions(identifier,20,args.get("cursor"))
        elif resource=="asset_events":data=repo.asset_events(identifier,20,args.get("cursor"))
        elif resource=="joint_analysis":data=repo.get_execution_joint_analysis(identifier,args.get("subject"))
        elif resource=="asset_performance":data=repo.asset_performance_trends(identifier)
        elif resource=="archives":
            archive=ArchiveRepository(session).get(identifier)
            data={k:archive.get(k) for k in ("id","archive_code","test_name","actual_object","station_label","execution_id","submission_status","processing_state","updated_at","manifest","acceptance")}
        elif resource=="campaigns":data={"contexts":setup.contexts(UUID(identifier))}
        else:raise ApiError(422,"resource_invalid","此资源没有详情读取入口，请先检索业务能力。")
        return card("business_object","业务对象详情",wire(data),"/api/v1/"+resource+"/"+identifier)
    if tool in {"search_project_source","read_project_source"}:
        from app.services.assistant_sources import source_query
        return card("source_evidence","项目源码与技术文档",source_query(args),"版本化项目源码快照")
    if tool in {"search_web","read_web_page"}:
        from app.services.assistant_web import web_query
        return card("web_evidence","公开技术资料",web_query(args),"公开HTTPS网页")
    if tool=="search_my_history":
        q=str(args.get("query","")).strip()
        if not q or len(q)>150:raise ApiError(422,"history_query","请输入1—150字历史检索词。")
        rows=session.execute(text("""SELECT thread_id,role,body,created_at FROM integration.assistant_message
          WHERE user_id=iam.current_user_id() AND created_at>=now()-interval '30 days'
          AND body ILIKE :q ORDER BY id DESC LIMIT 15"""),{"q":"%"+q.replace("%","\\%").replace("_","\\_")+"%"}).mappings().all()
        return card("history_evidence","本人近30天聊天检索",{"items":[dict(r) for r in rows]},"本账号聊天历史",scope="只读取当前账号自己的聊天，其他账号聊天不开放")
    raise ApiError(422,"unknown_assistant_tool","查询工具未实现，不能伪装成查询成功。")
