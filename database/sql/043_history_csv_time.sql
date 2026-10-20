-- Append derived timing provenance to new verifications; never rewrite original evidence.
-- Invoker security preserves archive RLS for both detail and list reads.
CREATE FUNCTION integration.archive_csv_timing(p_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY INVOKER SET search_path=pg_catalog,integration AS $$
DECLARE a integration.execution_archive%ROWTYPE; matches text[]; stamp text;
    starts timestamptz; first_sample timestamptz; ends timestamptz;
    first_offset numeric; last_offset numeric; q jsonb;
BEGIN
    SELECT * INTO a FROM integration.execution_archive WHERE id=p_id;
    IF NOT FOUND OR a.origin<>'HISTORY' OR a.upload_state<>'COMPLETE' OR a.processing_state<>'READY'
        OR a.facts->>'started_at' IS NOT NULL OR a.facts->>'ended_at' IS NOT NULL
        OR a.manifest->>'original_sha256' IS DISTINCT FROM a.sha256 THEN RETURN NULL; END IF;
    -- factory_hmi.core.service uses datetime.now(timezone.utc); the 18 digits
    -- come from build_local_execution_code('%y%m%d%H%M%S%f'). Other names are
    -- ambiguous (including minute-only formal codes) and are not guessed.
    matches:=regexp_match(a.filename,'^LOCAL_[A-Za-z0-9_.-]{1,64}_([0-9]{18})_[0-9a-fA-F]{32}[.]csv$');
    IF matches IS NULL THEN RETURN NULL; END IF;
    stamp:=matches[1];
    BEGIN
        starts:=make_timestamptz(2000+substr(stamp,1,2)::integer,substr(stamp,3,2)::integer,
            substr(stamp,5,2)::integer,substr(stamp,7,2)::integer,substr(stamp,9,2)::integer,
            substr(stamp,11,2)::double precision,'UTC')+substr(stamp,13,6)::integer*interval '1 microsecond';
    EXCEPTION WHEN datetime_field_overflow OR invalid_datetime_format THEN RETURN NULL;
    END;
    IF to_char(starts AT TIME ZONE 'UTC','YYMMDDHH24MISSUS')<>stamp THEN RETURN NULL; END IF;
    q:=a.manifest#>'{analysis,quality}';
    IF jsonb_typeof(a.manifest#>'{time_range,start}') IS DISTINCT FROM 'number'
        OR jsonb_typeof(a.manifest#>'{time_range,end}') IS DISTINCT FROM 'number'
        OR jsonb_typeof(q->'valid_time_rows') IS DISTINCT FROM 'number'
        OR jsonb_typeof(q->'invalid_time_rows') IS DISTINCT FROM 'number'
        OR jsonb_typeof(q->'non_monotonic') IS DISTINCT FROM 'number'
        OR (q->>'valid_time_rows')::numeric<2 OR (q->>'invalid_time_rows')::numeric<>0
        OR (q->>'non_monotonic')::numeric<>0 THEN RETURN NULL; END IF;
    first_offset:=(a.manifest#>>'{time_range,start}')::numeric;
    last_offset:=(a.manifest#>>'{time_range,end}')::numeric;
    IF first_offset<0 OR last_offset<=first_offset OR last_offset>315360000 THEN RETURN NULL; END IF;
    first_sample:=starts+round(first_offset*1000000)::bigint*interval '1 microsecond';
    ends:=starts+round(last_offset*1000000)::bigint*interval '1 microsecond';
    IF ends>statement_timestamp()+interval '5 minutes'
        OR (a.facts->>'active_seconds')::numeric>round(last_offset,6)+.001 THEN RETURN NULL; END IF;
    RETURN jsonb_build_object('version','rp1-hmi-filename-csv-time/1','filename',a.filename,
        'evidence_sha256',a.sha256,'timezone','UTC','end_basis','last_sample',
        'started_at',to_char(starts AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'first_sample_at',to_char(first_sample AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'ended_at',to_char(ends AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'recorded_seconds',round(last_offset,6),'first_offset_seconds',first_offset,'last_offset_seconds',last_offset);
END $$;
REVOKE ALL ON FUNCTION integration.archive_csv_timing(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_csv_timing(bigint) TO __RP1_APP_USER__;

CREATE OR REPLACE FUNCTION integration.archive_edit(p_id uuid,p_operation text,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; receipt integration.archive_request%ROWTYPE; ctx record;
    old_payload jsonb; current_status text; new_revision integer; detected jsonb; declared_key text; declared_value numeric;
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
        -- Derive on the server after validating the original request. The receipt
        -- digest still describes the incoming request, so retries stay idempotent.
        detected:=integration.archive_csv_timing(a.id);
        IF detected IS NOT NULL THEN
            p:=p||jsonb_build_object('declared_started_at',detected->'started_at',
                'declared_ended_at',detected->'ended_at','timing_evidence',detected);
            IF a.facts->>'active_seconds' IS NULL THEN
                p:=p||jsonb_build_object('declared_active_seconds',detected->'recorded_seconds');
            END IF;
        END IF;
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
