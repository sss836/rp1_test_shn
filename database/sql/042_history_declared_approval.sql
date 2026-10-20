-- Historical CSVs can be submitted after explicit, append-only human declarations.
-- Raw acquisition facts, CSV bytes and original analysis are never rewritten.
CREATE OR REPLACE FUNCTION integration.archive_edit(p_id uuid,p_operation text,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; receipt integration.archive_request%ROWTYPE; ctx record;
    old_payload jsonb; current_status text; new_revision integer; declared_key text; declared_value numeric;
    digest text:=encode(public.digest(convert_to(jsonb_build_object('id',p_id,'operation',p_operation,'payload',p)::text,'UTF8'),'sha256'),'hex');
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    IF p IS NULL OR jsonb_typeof(p)<>'object' OR octet_length(p::text)>16384 OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 OR p_operation NOT IN ('verification','classification') OR length(btrim(p->>'reason')) NOT BETWEEN 1 AND 300 THEN RAISE EXCEPTION '核实参数无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.archive_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>digest THEN RAISE EXCEPTION '归档幂等键内容冲突' USING ERRCODE='P0001'; END IF;
        RETURN (SELECT public_id FROM integration.execution_archive WHERE id=receipt.archive_id);
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.owner_id<>iam.current_user_id() AND NOT iam.is_system_admin() THEN RAISE EXCEPTION '仅提交人或管理员可核实归类' USING ERRCODE='42501'; END IF;
    SELECT status INTO current_status FROM integration.data_submission WHERE id=a.submission_id;
    IF coalesce(current_status,'') IN ('PENDING','APPROVED') OR a.revision<>(p->>'expected_revision')::integer THEN RAISE EXCEPTION '版本已变化或记录已送审' USING ERRCODE='P0001'; END IF;
    new_revision:=a.revision+1;
    IF p_operation='verification' THEN
        IF NOT (p ?& ARRAY['expected_revision','tester','station_id','verdict','notes','discrepancy_reason','declared_started_at','declared_ended_at','reason']) THEN
            RAISE EXCEPTION '核实必填字段不完整' USING ERRCODE='22023';
        END IF;
        IF (SELECT count(*) FROM jsonb_object_keys(p)) NOT BETWEEN 9 AND 12 OR EXISTS(SELECT 1 FROM jsonb_object_keys(p) k WHERE k<>ALL(ARRAY['expected_revision','tester','station_id','verdict','notes','discrepancy_reason','declared_started_at','declared_ended_at','declared_active_seconds','declared_completed_cycles','declared_exception_count','reason'])) THEN RAISE EXCEPTION '核实字段不得包含采集数据或编辑分析' USING ERRCODE='22023'; END IF;
        IF EXISTS(SELECT 1 FROM jsonb_each(p) f WHERE f.value='null'::jsonb AND f.key NOT IN ('declared_started_at','declared_ended_at','declared_active_seconds','declared_completed_cycles','declared_exception_count'))
            OR jsonb_typeof(p->'tester')<>'string' OR jsonb_typeof(p->'notes')<>'string' OR jsonb_typeof(p->'discrepancy_reason')<>'string'
            OR length(p->>'discrepancy_reason')>1000 THEN RAISE EXCEPTION '核实字段类型无效' USING ERRCODE='22023'; END IF;
        FOR declared_key IN SELECT unnest(ARRAY['declared_active_seconds','declared_completed_cycles','declared_exception_count']) LOOP
            IF p->>declared_key IS NOT NULL THEN
                IF a.origin<>'HISTORY' OR a.facts->>substr(declared_key,10) IS NOT NULL THEN
                    RAISE EXCEPTION '人工申报仅补充历史缺失信息，不能覆盖采集事实' USING ERRCODE='22023';
                END IF;
                IF jsonb_typeof(p->declared_key)<>'number' THEN
                    RAISE EXCEPTION '人工申报必须为数值' USING ERRCODE='22023';
                END IF;
                declared_value:=(p->>declared_key)::numeric;
                IF declared_value<0 OR (declared_key='declared_active_seconds' AND declared_value>315360000)
                    OR (declared_key<>'declared_active_seconds' AND (declared_value>2147483647 OR trunc(declared_value)<>declared_value)) THEN
                    RAISE EXCEPTION '人工申报时长或次数超出范围' USING ERRCODE='22023';
                END IF;
            END IF;
        END LOOP;
        IF ((p->>'declared_started_at') IS NULL) <> ((p->>'declared_ended_at') IS NULL)
            OR (p->>'declared_ended_at')::timestamptz>clock_timestamp()+interval '5 minutes' THEN
            RAISE EXCEPTION '人工申报开始结束时间不完整或在未来' USING ERRCODE='22023';
        END IF;
        IF coalesce((a.facts->>'active_seconds')::numeric,(p->>'declared_active_seconds')::numeric)
            >extract(epoch FROM (coalesce((a.facts->>'ended_at')::timestamptz,(p->>'declared_ended_at')::timestamptz)
                                -coalesce((a.facts->>'started_at')::timestamptz,(p->>'declared_started_at')::timestamptz)))+.001 THEN
            RAISE EXCEPTION '人工申报运行时长超过开始结束区间' USING ERRCODE='22023';
        END IF;
        IF a.processing_state<>'READY' THEN RAISE EXCEPTION '请先等待原始文件校验和分析完成' USING ERRCODE='P0001'; END IF;
        IF p->>'verdict' NOT IN ('passed','failed','blocked') OR length(btrim(p->>'tester')) NOT BETWEEN 1 AND 100 OR length(btrim(p->>'notes')) NOT BETWEEN 1 AND 2000
            OR NOT EXISTS(SELECT 1 FROM test.station st JOIN test.lab l ON l.id=st.lab_id JOIN test.site s ON s.id=l.site_id WHERE st.public_id=(p->>'station_id')::uuid AND st.enabled AND l.enabled AND s.enabled) THEN RAISE EXCEPTION '核实人、记录或有效工位无效' USING ERRCODE='22023'; END IF;
        IF p->>'verdict' IS DISTINCT FROM a.manifest#>>'{analysis,verdict}' AND length(btrim(coalesce(p->>'discrepancy_reason','')))=0 THEN RAISE EXCEPTION '人工结论与自动分析不同，必须填写原因' USING ERRCODE='22023'; END IF;
        IF (a.facts->>'started_at') IS NOT NULL AND ((p->>'declared_started_at') IS NOT NULL OR (p->>'declared_ended_at') IS NOT NULL) THEN RAISE EXCEPTION '已存在真实采集时间，不能补录覆盖' USING ERRCODE='22023'; END IF;
        IF (p->>'declared_started_at') IS NOT NULL AND ((p->>'declared_ended_at')::timestamptz<(p->>'declared_started_at')::timestamptz OR (a.facts->>'active_seconds')::numeric>extract(epoch FROM ((p->>'declared_ended_at')::timestamptz-(p->>'declared_started_at')::timestamptz))+.001) THEN RAISE EXCEPTION '人工补录时间与有效时长冲突' USING ERRCODE='22023'; END IF;
        INSERT INTO integration.archive_verification(archive_id,revision,actor_id,payload,reason) VALUES(a.id,new_revision,iam.current_user_id(),p-'expected_revision'-'reason',p->>'reason');
        UPDATE integration.execution_archive SET revision=new_revision,verification_revision=new_revision,updated_at=clock_timestamp() WHERE id=a.id;
    ELSE
        IF (SELECT count(*) FROM jsonb_object_keys(p))<>3 OR (SELECT count(*) FROM jsonb_object_keys(p->'context'))<>7 THEN RAISE EXCEPTION '归类字段无效' USING ERRCODE='22023'; END IF;
        SELECT * INTO ctx FROM integration.archive_context(p->'context',a.id);
        IF NOT FOUND THEN RAISE EXCEPTION '执行上下文无效或已停用' USING ERRCODE='22023'; END IF;
        SELECT payload INTO old_payload FROM integration.archive_verification WHERE archive_id=a.id AND revision=a.verification_revision;
        IF old_payload IS NOT NULL AND old_payload->>'station_id'<>p#>>'{context,station_id}' THEN RAISE EXCEPTION '归类工位必须与核实工位一致' USING ERRCODE='22023'; END IF;
        INSERT INTO integration.archive_classification(archive_id,revision,actor_id,payload,reason) VALUES(a.id,new_revision,iam.current_user_id(),p->'context',p->>'reason');
        UPDATE integration.execution_archive SET revision=new_revision,classification_revision=new_revision,updated_at=clock_timestamp() WHERE id=a.id;
    END IF;
    INSERT INTO integration.archive_request(actor_id,idempotency_key,request_hash,archive_id,operation) VALUES(iam.current_user_id(),p_key,digest,a.id,p_operation);
    RETURN a.public_id;
END $$;

CREATE OR REPLACE FUNCTION integration.archive_summary(p_archive_id bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, integration, iam AS $$
DECLARE a integration.execution_archive%ROWTYPE; v jsonb; c jsonb; starts timestamptz; ends timestamptz;
    active numeric; cycles bigint; exceptions bigint; source_status text; manual_history boolean;
BEGIN
    IF iam.current_principal_kind() <> 'HUMAN' OR NOT iam.can_write_business() THEN
        RAISE EXCEPTION '需要执行员或管理员身份' USING ERRCODE='42501';
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE id=p_archive_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.upload_state <> 'COMPLETE' OR a.processing_state <> 'READY'
       OR a.verification_revision IS NULL OR a.classification_revision IS NULL THEN
        RAISE EXCEPTION '归档校验、核实或用例归类尚未完成' USING ERRCODE='22023';
    END IF;
    IF a.manifest->>'original_sha256' IS DISTINCT FROM a.sha256 THEN
        RAISE EXCEPTION '归档证据哈希尚未校验' USING ERRCODE='22023';
    END IF;
    SELECT payload INTO STRICT v FROM integration.archive_verification
        WHERE archive_id=a.id AND revision=a.verification_revision;
    SELECT payload INTO STRICT c FROM integration.archive_classification
        WHERE archive_id=a.id AND revision=a.classification_revision;
    IF v->>'station_id' IS DISTINCT FROM c->>'station_id' THEN
        RAISE EXCEPTION '核实工位与正式执行上下文不一致' USING ERRCODE='22023';
    END IF;
    starts := coalesce((a.facts->>'started_at')::timestamptz, (v->>'declared_started_at')::timestamptz);
    ends := coalesce((a.facts->>'ended_at')::timestamptz, (v->>'declared_ended_at')::timestamptz);
    IF starts IS NULL OR ends IS NULL THEN
        RAISE EXCEPTION '历史记录需补充申报开始与结束时间，不能以文件时间代替' USING ERRCODE='22023';
    END IF;
    active:=coalesce((a.facts->>'active_seconds')::numeric,(v->>'declared_active_seconds')::numeric);
    cycles:=coalesce((a.facts->>'completed_cycles')::bigint,(v->>'declared_completed_cycles')::bigint);
    exceptions:=coalesce((a.facts->>'exception_count')::bigint,(v->>'declared_exception_count')::bigint);
    manual_history:=a.origin='HISTORY' AND (a.facts->>'active_seconds' IS NULL OR a.facts->>'completed_cycles' IS NULL
        OR a.facts->>'exception_count' IS NULL OR coalesce(a.facts->>'machine_status','unknown')='unknown');
    IF active IS NULL OR cycles IS NULL OR exceptions IS NULL THEN
        RAISE EXCEPTION '请在核实信息中补齐人工申报运行时长、完成循环和异常次数' USING ERRCODE='22023';
    END IF;
    IF active<0 OR active>315360000 OR cycles NOT BETWEEN 0 AND 2147483647 OR exceptions NOT BETWEEN 0 AND 2147483647
        OR ends<starts OR active>extract(epoch FROM (ends-starts))+.001 THEN
        RAISE EXCEPTION '申报时长或次数不符合要求' USING ERRCODE='22023';
    END IF;
    source_status:=CASE a.facts->>'machine_status' WHEN 'completed' THEN 'passed' WHEN 'failed' THEN 'failed'
        ELSE CASE WHEN manual_history AND coalesce(a.facts->>'machine_status','unknown')='unknown' THEN v->>'verdict' ELSE 'blocked' END END;
    RETURN c || jsonb_build_object(
        'execution_uuid',a.execution_uuid,'started_at',starts,'ended_at',ends,
        'active_seconds',active,'completed_cycles',cycles,
        'source_status',source_status,
        'data_quality',CASE
            WHEN a.facts->>'data_quality'='MISSING' OR a.manifest->>'quality_status'='MISSING' THEN 'MISSING'
            WHEN a.facts->>'data_quality'='INVALID' OR a.manifest->>'quality_status'='INVALID' THEN 'INVALID'
            WHEN a.facts->>'data_quality'='PARTIAL' OR a.manifest->>'quality_status'='PARTIAL' THEN 'PARTIAL'
            ELSE 'VALID' END,
        'clock_quality',CASE WHEN a.facts->>'started_at' IS NULL OR a.facts->>'ended_at' IS NULL
            OR a.facts->>'clock_quality' NOT IN ('VALID','PARTIAL') THEN 'INVALID' ELSE a.facts->>'clock_quality' END,
        'exception_count',exceptions,'summary',left(CASE WHEN manual_history THEN '历史CSV人工申报；'||coalesce(v->>'notes','') ELSE coalesce(a.facts->>'summary','') END,500),
        'evidence_sha256',a.sha256);
END $$;
REVOKE ALL ON FUNCTION integration.archive_summary(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_summary(bigint) TO __RP1_APP_USER__;
