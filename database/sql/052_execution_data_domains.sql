-- Execution partition evidence and factory approval are additive. No raw evidence is rewritten.
CREATE TABLE integration.archive_domain_revision (
    archive_id bigint NOT NULL REFERENCES integration.execution_archive(id),
    revision integer NOT NULL CHECK(revision>0),
    data_domain text NOT NULL CHECK(data_domain IN ('RESEARCH','FACTORY','UNCLASSIFIED')),
    data_domain_source text NOT NULL CHECK(data_domain_source IN ('EXPLICIT','HISTORICAL_BENCH','NO_EVIDENCE','CORRECTION')),
    serial_number text CHECK(serial_number IS NULL OR length(btrim(serial_number)) BETWEEN 1 AND 200),
    bench_id text CHECK(bench_id IS NULL OR length(btrim(bench_id)) BETWEEN 1 AND 100),
    actor_id bigint REFERENCES iam.app_user(id),
    evidence jsonb NOT NULL,
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(archive_id,revision)
);
CREATE TABLE integration.execution_domain_evidence (
    execution_id bigint PRIMARY KEY REFERENCES test.test_execution(id),
    data_domain text NOT NULL CHECK(data_domain IN ('RESEARCH','FACTORY','UNCLASSIFIED')),
    data_domain_source text NOT NULL CHECK(data_domain_source IN ('EXPLICIT','HISTORICAL_BENCH','NO_EVIDENCE')),
    bench_id text,
    evidence jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE integration.partition_request (
    actor_id bigint NOT NULL REFERENCES iam.app_user(id),
    idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
    request_hash text NOT NULL,
    archive_id bigint NOT NULL REFERENCES integration.execution_archive(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(actor_id,idempotency_key)
);
CREATE TABLE integration.factory_submission (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    archive_id bigint NOT NULL UNIQUE REFERENCES integration.execution_archive(id),
    submitter_id bigint NOT NULL REFERENCES iam.app_user(id),
    revision integer NOT NULL CHECK(revision>0),
    status text NOT NULL CHECK(status IN ('PENDING','APPROVED','REJECTED')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE integration.factory_submission_revision (
    submission_id bigint NOT NULL REFERENCES integration.factory_submission(id),
    revision integer NOT NULL,
    verification_revision integer NOT NULL,
    domain_revision integer NOT NULL,
    payload jsonb NOT NULL,
    actor_id bigint NOT NULL REFERENCES iam.app_user(id),
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(submission_id,revision)
);
CREATE TABLE integration.factory_review (
    submission_id bigint NOT NULL,
    revision integer NOT NULL,
    decision text NOT NULL CHECK(decision IN ('APPROVED','REJECTED')),
    actor_id bigint NOT NULL REFERENCES iam.app_user(id),
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(submission_id,revision),
    FOREIGN KEY(submission_id,revision) REFERENCES integration.factory_submission_revision(submission_id,revision)
);

-- Exact station code is evidence. A free-form display label is never a code.
CREATE FUNCTION integration.trusted_bench(p_bench text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog AS $$
    SELECT CASE WHEN upper(btrim(p_bench)) ~ '^[A-Z0-9][A-Z0-9._-]{0,99}$'
        AND upper(btrim(p_bench)) NOT IN ('UNKNOWN','NONE','NULL')
        AND btrim(p_bench) !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN upper(btrim(p_bench)) END
$$;
CREATE FUNCTION integration.domain_from_bench(p_bench text) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,integration AS $$
    SELECT CASE WHEN integration.trusted_bench(p_bench) IS NULL THEN 'UNCLASSIFIED'
        WHEN left(integration.trusted_bench(p_bench),1)='A' THEN 'FACTORY' ELSE 'RESEARCH' END
$$;
CREATE FUNCTION integration.archive_domain_info(p_id bigint)
RETURNS TABLE(data_domain text,data_domain_source text,serial_number text,bench_id text,domain_revision integer)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
    SELECT coalesce(d.data_domain,'UNCLASSIFIED'),coalesce(d.data_domain_source,'NO_EVIDENCE'),d.serial_number,d.bench_id,coalesce(d.revision,0)
    FROM integration.execution_archive a LEFT JOIN LATERAL (
        SELECT * FROM integration.archive_domain_revision WHERE archive_id=a.id ORDER BY revision DESC LIMIT 1
    ) d ON true WHERE a.id=p_id AND integration.can_read_archive(a.id)
$$;
-- Internal raw resolver is deliberately not exposed to the application role.
CREATE FUNCTION integration.archive_domain_internal(p_id bigint) RETURNS integration.archive_domain_revision
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
    SELECT d FROM integration.archive_domain_revision d WHERE archive_id=p_id ORDER BY revision DESC LIMIT 1
$$;

-- Bootstrap append-only classification evidence for historical archives.
INSERT INTO integration.archive_domain_revision(archive_id,revision,data_domain,data_domain_source,serial_number,bench_id,evidence,reason)
SELECT a.id,1,coalesce(b.mode,integration.domain_from_bench(b.bench)),CASE WHEN b.mode IS NOT NULL THEN 'EXPLICIT' WHEN b.bench IS NULL THEN 'NO_EVIDENCE' ELSE 'HISTORICAL_BENCH' END,
    coalesce(nullif(a.manifest#>>'{identity,serial_number}',''),nullif(r.raw_payload->>'serial_number','')),b.bench,
    jsonb_build_object('original_sha256',a.sha256,'manifest_sha256',encode(public.digest(a.manifest::text,'sha256'),'hex'),'bench_evidence',b.source,'import_row_hash',r.row_hash,'explicit_mode',b.mode),
    '0052历史分区证据初始化；不改写原manifest或核实审批版本'
FROM integration.execution_archive a
LEFT JOIN integration.archive_classification c ON c.archive_id=a.id AND c.revision=a.classification_revision
LEFT JOIN test.station st ON st.public_id=(c.payload->>'station_id')::uuid
LEFT JOIN test.test_execution e ON e.acquisition_uuid=a.execution_uuid OR (e.approved_submission_id IS NOT NULL AND e.approved_submission_id=a.submission_id)
LEFT JOIN integration.execution_import_record r ON r.execution_id=e.id
CROSS JOIN LATERAL (SELECT coalesce(integration.trusted_bench(a.manifest#>>'{identity,bench_id}'),integration.trusted_bench(r.raw_payload->>'bench_id'),integration.trusted_bench(st.station_code)) AS bench,
    coalesce(CASE WHEN a.manifest#>>'{identity,data_domain}' IN ('RESEARCH','FACTORY')
        AND coalesce(a.manifest#>>'{identity,data_domain_source}','') NOT IN ('HISTORICAL_BENCH','NO_EVIDENCE') THEN a.manifest#>>'{identity,data_domain}' END,
        CASE WHEN r.raw_payload->>'data_domain' IN ('RESEARCH','FACTORY')
        AND coalesce(r.raw_payload->>'data_domain_source','') NOT IN ('HISTORICAL_BENCH','NO_EVIDENCE') THEN r.raw_payload->>'data_domain' END) AS mode,
    CASE WHEN integration.trusted_bench(a.manifest#>>'{identity,bench_id}') IS NOT NULL THEN 'manifest.identity.bench_id'
         WHEN integration.trusted_bench(r.raw_payload->>'bench_id') IS NOT NULL THEN 'import.raw_payload.bench_id'
         WHEN integration.trusted_bench(st.station_code) IS NOT NULL THEN 'classification.station_id' ELSE 'NO_EVIDENCE' END AS source) b;

INSERT INTO integration.execution_domain_evidence(execution_id,data_domain,data_domain_source,bench_id,evidence)
SELECT e.id,coalesce(b.mode,integration.domain_from_bench(b.bench)),CASE WHEN b.mode IS NOT NULL THEN 'EXPLICIT' WHEN b.bench IS NULL THEN 'NO_EVIDENCE' ELSE 'HISTORICAL_BENCH' END,b.bench,
    jsonb_build_object('bench_evidence',b.source,'import_row_hash',r.row_hash,'station_id',st.public_id,'explicit_mode',b.mode)
FROM test.test_execution e
LEFT JOIN integration.execution_import_record r ON r.execution_id=e.id
LEFT JOIN test.station st ON st.id=e.station_id
CROSS JOIN LATERAL (SELECT coalesce(integration.trusted_bench(r.raw_payload->>'bench_id'),integration.trusted_bench(st.station_code)) AS bench,
    CASE WHEN r.raw_payload->>'data_domain' IN ('RESEARCH','FACTORY')
        AND coalesce(r.raw_payload->>'data_domain_source','') NOT IN ('HISTORICAL_BENCH','NO_EVIDENCE') THEN r.raw_payload->>'data_domain' END AS mode,
    CASE WHEN integration.trusted_bench(r.raw_payload->>'bench_id') IS NOT NULL THEN 'import.raw_payload.bench_id'
         WHEN integration.trusted_bench(st.station_code) IS NOT NULL THEN 'execution.station_id' ELSE 'NO_EVIDENCE' END AS source) b;

CREATE OR REPLACE FUNCTION integration.can_read_archive(p_id bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
    SELECT coalesce(iam.is_internal_service() OR (iam.current_principal_kind()='HUMAN' AND (
        iam.can_write_business() OR EXISTS (
            SELECT 1 FROM integration.execution_archive a JOIN integration.data_submission s ON s.id=a.submission_id
            WHERE a.id=p_id AND a.classification_revision IS NOT NULL AND s.status='APPROVED'
                AND iam.can_access_campaign(s.campaign_id,false)
        ) OR (iam.current_user_role()='VIEWER' AND EXISTS (
            SELECT 1 FROM integration.factory_submission s WHERE s.archive_id=p_id AND s.status='APPROVED'
        )))),false)
$$;

DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['archive_domain_revision','partition_request','factory_submission','factory_submission_revision','factory_review','execution_domain_evidence'] LOOP
        EXECUTE format('ALTER TABLE integration.%I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('REVOKE ALL ON integration.%I FROM PUBLIC,__RP1_APP_USER__',t);
        EXECUTE format('GRANT SELECT ON integration.%I TO __RP1_APP_USER__',t);
        EXECUTE format('CREATE TRIGGER partition_audit AFTER INSERT OR UPDATE OR DELETE ON integration.%I FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change()',t);
        IF t<>'factory_submission' THEN
            EXECUTE format('CREATE TRIGGER partition_immutable BEFORE UPDATE OR DELETE ON integration.%I FOR EACH ROW EXECUTE FUNCTION integration.archive_immutable()',t);
        END IF;
    END LOOP;
END $$;
CREATE POLICY domain_read ON integration.archive_domain_revision FOR SELECT USING(integration.can_read_archive(archive_id));
CREATE POLICY domain_read ON integration.execution_domain_evidence FOR SELECT USING(EXISTS(SELECT 1 FROM test.test_execution e WHERE e.id=execution_id));
CREATE POLICY partition_request_read ON integration.partition_request FOR SELECT USING(actor_id=iam.current_user_id());
CREATE POLICY factory_read ON integration.factory_submission FOR SELECT USING(integration.can_read_archive(archive_id));
CREATE POLICY factory_read ON integration.factory_submission_revision FOR SELECT USING(EXISTS(SELECT 1 FROM integration.factory_submission s WHERE s.id=submission_id));
CREATE POLICY factory_read ON integration.factory_review FOR SELECT USING(EXISTS(SELECT 1 FROM integration.factory_submission s WHERE s.id=submission_id));

-- Creation keeps the old metadata validator, while the extension has its own full-payload receipt.
ALTER FUNCTION integration.archive_create(jsonb,text) RENAME TO archive_create_pre_partition;
CREATE FUNCTION integration.archive_create(p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE aid uuid; a integration.execution_archive%ROWTYPE; d integration.archive_domain_revision; b text; mode text; sn text;
    request_digest text:=encode(public.digest(jsonb_build_object('operation','partition-create','payload',p)::text,'sha256'),'hex'); receipt integration.partition_request%ROWTYPE;
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    IF p IS NULL OR jsonb_typeof(p)<>'object' OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 THEN RAISE EXCEPTION '归档参数无效' USING ERRCODE='22023'; END IF;
    mode:=p->>'data_domain'; b:=integration.trusted_bench(p->>'bench_id'); sn:=p->>'serial_number';
    IF (mode IS NOT NULL AND mode NOT IN ('RESEARCH','FACTORY')) OR (sn IS NOT NULL AND (jsonb_typeof(p->'serial_number')<>'string' OR length(btrim(sn)) NOT BETWEEN 1 AND 200))
        OR (b IS NOT NULL AND (jsonb_typeof(p->'bench_id')<>'string' OR length(b)>100)) OR (mode='FACTORY' AND sn IS NULL) THEN RAISE EXCEPTION '分区、SN或台架无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.partition_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>request_digest THEN RAISE EXCEPTION '分区归档幂等冲突' USING ERRCODE='P0001'; END IF;
        RETURN (SELECT public_id FROM integration.execution_archive WHERE id=receipt.archive_id);
    END IF;
    aid:=integration.archive_create_pre_partition(p-'data_domain'-'serial_number'-'bench_id',p_key);
    SELECT * INTO STRICT a FROM integration.execution_archive WHERE public_id=aid FOR UPDATE;
    IF b IS NULL THEN
        SELECT st.station_code INTO b FROM integration.archive_classification c JOIN test.station st ON st.public_id=(c.payload->>'station_id')::uuid WHERE c.archive_id=a.id AND c.revision=a.classification_revision;
    END IF;
    IF EXISTS(SELECT 1 FROM integration.execution_domain_evidence ed JOIN test.test_execution e ON e.id=ed.execution_id
        WHERE e.acquisition_uuid=a.execution_uuid AND ed.data_domain_source='EXPLICIT'
            AND ((mode IS NOT NULL AND ed.data_domain<>mode) OR (b IS NOT NULL AND ed.bench_id IS DISTINCT FROM b))) THEN
        RAISE EXCEPTION '归档与服务端启动模式或台架快照不一致' USING ERRCODE='22023';
    END IF;
    d:=integration.archive_domain_internal(a.id);
    IF d.archive_id IS NOT NULL THEN
        IF (mode IS NOT NULL AND (d.data_domain<>mode OR d.data_domain_source<>'EXPLICIT')) OR (sn IS NOT NULL AND sn IS DISTINCT FROM d.serial_number)
            OR (b IS NOT NULL AND b IS DISTINCT FROM d.bench_id) THEN RAISE EXCEPTION '原执行分区快照不可覆盖，请追加更正版本' USING ERRCODE='P0001'; END IF;
    ELSE
        INSERT INTO integration.archive_domain_revision(archive_id,revision,data_domain,data_domain_source,serial_number,bench_id,actor_id,evidence,reason)
        VALUES(a.id,1,coalesce(mode,integration.domain_from_bench(b)),CASE WHEN mode IS NOT NULL THEN 'EXPLICIT' WHEN b IS NOT NULL THEN 'HISTORICAL_BENCH' ELSE 'NO_EVIDENCE' END,
            sn,b,iam.current_user_id(),jsonb_build_object('original_sha256',a.sha256,'creation_payload_sha256',request_digest),'首次归档分区快照');
    END IF;
    INSERT INTO integration.partition_request(actor_id,idempotency_key,request_hash,archive_id) VALUES(iam.current_user_id(),p_key,request_digest,a.id);
    RETURN aid;
END $$;

ALTER FUNCTION integration.archive_import(jsonb,text) RENAME TO archive_import_pre_partition;
CREATE FUNCTION integration.archive_import(p jsonb,p_key text) RETURNS TABLE(archive_id uuid,reused boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE result record; a integration.execution_archive%ROWTYPE; b text; mode text:=p->>'data_domain'; sn text:=p->>'serial_number';
    request_digest text:=encode(public.digest(jsonb_build_object('operation','partition-import','payload',p)::text,'sha256'),'hex'); receipt integration.partition_request%ROWTYPE;
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    IF mode IS NOT NULL AND mode NOT IN ('RESEARCH','FACTORY') OR (sn IS NOT NULL AND (jsonb_typeof(p->'serial_number')<>'string' OR length(btrim(sn)) NOT BETWEEN 1 AND 200))
        OR (mode='FACTORY' AND sn IS NULL) OR length(coalesce(p->>'bench_id',''))>100 THEN RAISE EXCEPTION '分区或SN无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.partition_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>request_digest THEN RAISE EXCEPTION '分区补录幂等冲突' USING ERRCODE='P0001'; END IF;
        RETURN QUERY SELECT a0.public_id,true FROM integration.execution_archive a0 WHERE a0.id=receipt.archive_id; RETURN;
    END IF;
    SELECT * INTO result FROM integration.archive_import_pre_partition(p-'data_domain'-'serial_number'-'bench_id',p_key);
    SELECT * INTO STRICT a FROM integration.execution_archive a0 WHERE a0.public_id=result.archive_id;
    IF NOT result.reused THEN
        SELECT station_code INTO b FROM test.station WHERE public_id=(p->>'station_id')::uuid;
        IF nullif(p->>'bench_id','') IS NOT NULL AND p->>'bench_id' IS DISTINCT FROM b THEN RAISE EXCEPTION '补录台架必须与真实工位一致' USING ERRCODE='22023'; END IF;
        INSERT INTO integration.archive_domain_revision(archive_id,revision,data_domain,data_domain_source,serial_number,bench_id,actor_id,evidence,reason)
        VALUES(a.id,1,coalesce(mode,integration.domain_from_bench(b)),CASE WHEN mode IS NOT NULL THEN 'EXPLICIT' ELSE 'HISTORICAL_BENCH' END,sn,b,iam.current_user_id(),jsonb_build_object('original_sha256',a.sha256,'station_id',p->'station_id'),'历史CSV补录分区证据');
    END IF;
    INSERT INTO integration.partition_request(actor_id,idempotency_key,request_hash,archive_id) VALUES(iam.current_user_id(),p_key,request_digest,a.id);
    RETURN QUERY SELECT result.archive_id,result.reused;
END $$;

ALTER FUNCTION integration.archive_edit(uuid,text,jsonb,text) RENAME TO archive_edit_pre_partition;
CREATE FUNCTION integration.archive_edit(p_id uuid,p_operation text,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE a integration.execution_archive%ROWTYPE; d integration.archive_domain_revision; b text; result uuid;
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    IF EXISTS(SELECT 1 FROM integration.archive_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key) THEN
        RETURN integration.archive_edit_pre_partition(p_id,p_operation,p,p_key);
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF EXISTS(SELECT 1 FROM integration.factory_submission WHERE archive_id=a.id AND status IN ('PENDING','APPROVED')) THEN RAISE EXCEPTION '工厂记录已送审，不能覆盖核实版本' USING ERRCODE='P0001'; END IF;
    d:=integration.archive_domain_internal(a.id);
    IF p_operation='verification' THEN
        SELECT station_code INTO b FROM test.station WHERE public_id=(p->>'station_id')::uuid;
        IF d.bench_id IS NOT NULL AND d.bench_id IS DISTINCT FROM b THEN RAISE EXCEPTION '核实工位与封存台架不一致，请先追加台架更正证据' USING ERRCODE='22023'; END IF;
    END IF;
    result:=integration.archive_edit_pre_partition(p_id,p_operation,p,p_key);
    IF p_operation='verification' AND d.bench_id IS NULL THEN
        INSERT INTO integration.archive_domain_revision(archive_id,revision,data_domain,data_domain_source,serial_number,bench_id,actor_id,evidence,reason)
        VALUES(a.id,coalesce(d.revision,0)+1,CASE WHEN d.data_domain_source IN ('EXPLICIT','CORRECTION') THEN d.data_domain ELSE integration.domain_from_bench(b) END,
            CASE WHEN d.data_domain_source IN ('EXPLICIT','CORRECTION') THEN d.data_domain_source ELSE 'HISTORICAL_BENCH' END,d.serial_number,b,iam.current_user_id(),jsonb_build_object('original_sha256',a.sha256,'station_id',p->'station_id'),'首次核实真实工位补充分区证据');
    END IF;
    RETURN result;
END $$;

CREATE FUNCTION integration.archive_classify_domain(p_id uuid,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; d integration.archive_domain_revision; receipt integration.partition_request%ROWTYPE;
    request_digest text:=encode(public.digest(jsonb_build_object('id',p_id,'operation','domain','payload',p)::text,'sha256'),'hex');
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    IF p IS NULL OR (SELECT count(*) FROM jsonb_object_keys(p))<>5 OR NOT p ?& ARRAY['expected_revision','data_domain','serial_number','bench_id','reason']
        OR jsonb_typeof(p->'expected_revision') IS DISTINCT FROM 'number' OR (p->>'expected_revision')::numeric NOT BETWEEN 0 AND 2147483647 OR trunc((p->>'expected_revision')::numeric)<>(p->>'expected_revision')::numeric
        OR jsonb_typeof(p->'data_domain') IS DISTINCT FROM 'string' OR p->>'data_domain' NOT IN ('RESEARCH','FACTORY','UNCLASSIFIED')
        OR (p->>'serial_number' IS NOT NULL AND jsonb_typeof(p->'serial_number')<>'string') OR (p->>'bench_id' IS NOT NULL AND jsonb_typeof(p->'bench_id')<>'string')
        OR jsonb_typeof(p->'reason') IS DISTINCT FROM 'string' OR length(btrim(p->>'reason')) NOT BETWEEN 1 AND 300
        OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 THEN RAISE EXCEPTION '分区更正参数无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.partition_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>request_digest THEN RAISE EXCEPTION '分区更正幂等冲突' USING ERRCODE='P0001'; END IF;
        RETURN (SELECT public_id FROM integration.execution_archive WHERE id=receipt.archive_id);
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.owner_id<>iam.current_user_id() AND NOT iam.is_system_admin() THEN RAISE EXCEPTION '仅所有人或管理员可更正分区' USING ERRCODE='42501'; END IF;
    IF a.revision<>(p->>'expected_revision')::integer OR EXISTS(SELECT 1 FROM integration.factory_submission WHERE archive_id=a.id AND status='PENDING')
        OR EXISTS(SELECT 1 FROM integration.data_submission WHERE id=a.submission_id AND status='PENDING') THEN RAISE EXCEPTION '分区版本已变化或正在审批' USING ERRCODE='P0001'; END IF;
    IF (EXISTS(SELECT 1 FROM integration.factory_submission WHERE archive_id=a.id AND status='APPROVED')
        OR EXISTS(SELECT 1 FROM integration.data_submission WHERE id=a.submission_id AND status='APPROVED')) THEN RAISE EXCEPTION '已批准记录冻结业务分区；不得沿用旧批准切换分区' USING ERRCODE='42501'; END IF;
    d:=integration.archive_domain_internal(a.id);
    INSERT INTO integration.archive_domain_revision(archive_id,revision,data_domain,data_domain_source,serial_number,bench_id,actor_id,evidence,reason)
    VALUES(a.id,coalesce(d.revision,0)+1,p->>'data_domain','CORRECTION',coalesce(p->>'serial_number',d.serial_number),coalesce(p->>'bench_id',d.bench_id),iam.current_user_id(),
        jsonb_build_object('previous_domain_revision',d.revision,'original_sha256',a.sha256,'manifest_sha256',encode(public.digest(a.manifest::text,'sha256'),'hex')),p->>'reason');
    UPDATE integration.execution_archive SET revision=revision+1,updated_at=clock_timestamp() WHERE id=a.id;
    INSERT INTO integration.partition_request(actor_id,idempotency_key,request_hash,archive_id) VALUES(iam.current_user_id(),p_key,request_digest,a.id);
    RETURN a.public_id;
END $$;

-- Frozen independent factory identity requires checked CSV, verified actual station and bounded times.
CREATE FUNCTION integration.factory_summary(p_id bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,test AS $$
DECLARE a integration.execution_archive%ROWTYPE; d integration.archive_domain_revision; v jsonb; b text; starts timestamptz; ends timestamptz; active numeric;
BEGIN
    SELECT * INTO STRICT a FROM integration.execution_archive WHERE id=p_id FOR UPDATE;
    d:=integration.archive_domain_internal(a.id);
    IF d.data_domain IS DISTINCT FROM 'FACTORY' OR d.serial_number IS NULL OR d.bench_id IS NULL OR a.verification_revision IS NULL
        OR a.processing_state<>'READY' OR a.upload_state<>'COMPLETE' OR a.manifest->>'original_sha256' IS DISTINCT FROM a.sha256 THEN RAISE EXCEPTION '工厂SN、台架、核实或原始证据尚未完整校验' USING ERRCODE='22023'; END IF;
    SELECT payload INTO STRICT v FROM integration.archive_verification WHERE archive_id=a.id AND revision=a.verification_revision;
    SELECT st.station_code INTO b FROM test.station st JOIN test.lab l ON l.id=st.lab_id JOIN test.site s ON s.id=l.site_id
        WHERE st.public_id=(v->>'station_id')::uuid AND st.enabled AND l.enabled AND s.enabled;
    IF b IS DISTINCT FROM d.bench_id THEN RAISE EXCEPTION '工厂真实核实工位与台架快照不一致' USING ERRCODE='22023'; END IF;
    starts:=coalesce((a.facts->>'started_at')::timestamptz,(v->>'declared_started_at')::timestamptz);
    ends:=coalesce((a.facts->>'ended_at')::timestamptz,(v->>'declared_ended_at')::timestamptz);
    active:=coalesce((a.facts->>'active_seconds')::numeric,(v->>'declared_active_seconds')::numeric);
    IF starts IS NULL OR ends IS NULL OR active IS NULL OR NOT isfinite(starts) OR NOT isfinite(ends) OR ends<starts OR ends>clock_timestamp()+interval '5 minutes'
        OR active NOT BETWEEN 0 AND 315360000 OR active>extract(epoch FROM (ends-starts))+.001 THEN RAISE EXCEPTION '工厂真实运行时间不完整' USING ERRCODE='22023'; END IF;
    RETURN jsonb_build_object('execution_uuid',a.execution_uuid,'serial_number',d.serial_number,'bench_id',d.bench_id,'station_id',v->'station_id',
        'started_at',starts,'ended_at',ends,'active_seconds',active,'verdict',v->'verdict','tester',v->'tester','evidence_sha256',a.sha256,
        'verification_revision',a.verification_revision,'domain_revision',d.revision);
END $$;

CREATE FUNCTION integration.factory_submit(p_id uuid,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; s integration.factory_submission%ROWTYPE; summary jsonb; receipt integration.partition_request%ROWTYPE;
    request_digest text:=encode(public.digest(jsonb_build_object('id',p_id,'operation','factory-submit','payload',p)::text,'sha256'),'hex');
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    IF p IS NULL OR (SELECT count(*) FROM jsonb_object_keys(p))<>3 OR NOT p ?& ARRAY['expected_revision','expected_submission_revision','reason']
        OR jsonb_typeof(p->'expected_revision') IS DISTINCT FROM 'number' OR (p->>'expected_revision')::numeric NOT BETWEEN 0 AND 2147483647 OR trunc((p->>'expected_revision')::numeric)<>(p->>'expected_revision')::numeric
        OR jsonb_typeof(p->'expected_submission_revision') IS DISTINCT FROM 'number' OR (p->>'expected_submission_revision')::numeric NOT BETWEEN 0 AND 2147483647 OR trunc((p->>'expected_submission_revision')::numeric)<>(p->>'expected_submission_revision')::numeric
        OR jsonb_typeof(p->'reason') IS DISTINCT FROM 'string' OR length(btrim(p->>'reason')) NOT BETWEEN 1 AND 300 OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 THEN RAISE EXCEPTION '工厂送审参数无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.partition_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>request_digest THEN RAISE EXCEPTION '工厂送审幂等冲突' USING ERRCODE='P0001'; END IF;
        RETURN (SELECT public_id FROM integration.execution_archive WHERE id=receipt.archive_id);
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.owner_id<>iam.current_user_id() THEN RAISE EXCEPTION '请由归档所有人送审' USING ERRCODE='42501'; END IF;
    IF a.revision<>(p->>'expected_revision')::integer OR EXISTS(SELECT 1 FROM integration.data_submission WHERE id=a.submission_id AND status IN ('PENDING','APPROVED')) THEN RAISE EXCEPTION '核实版本已变更或已有研发提交' USING ERRCODE='P0001'; END IF;
    summary:=integration.factory_summary(a.id);
    SELECT * INTO s FROM integration.factory_submission WHERE archive_id=a.id FOR UPDATE;
    IF s.id IS NULL THEN
        IF (p->>'expected_submission_revision')::integer<>0 THEN RAISE EXCEPTION '送审版本冲突' USING ERRCODE='P0001'; END IF;
        INSERT INTO integration.factory_submission(archive_id,submitter_id,revision,status) VALUES(a.id,iam.current_user_id(),1,'PENDING') RETURNING * INTO s;
    ELSE
        IF s.revision<>(p->>'expected_submission_revision')::integer OR s.status<>'REJECTED' THEN RAISE EXCEPTION '只有当前驳回版本可重提' USING ERRCODE='P0001'; END IF;
        UPDATE integration.factory_submission SET revision=revision+1,status='PENDING',updated_at=clock_timestamp() WHERE id=s.id RETURNING * INTO s;
    END IF;
    INSERT INTO integration.factory_submission_revision(submission_id,revision,verification_revision,domain_revision,payload,actor_id,reason)
    VALUES(s.id,s.revision,a.verification_revision,(summary->>'domain_revision')::integer,summary,iam.current_user_id(),p->>'reason');
    INSERT INTO integration.partition_request(actor_id,idempotency_key,request_hash,archive_id) VALUES(iam.current_user_id(),p_key,request_digest,a.id);
    RETURN a.public_id;
END $$;

CREATE FUNCTION integration.factory_review_archive(p_id uuid,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; s integration.factory_submission%ROWTYPE; snap integration.factory_submission_revision%ROWTYPE; receipt integration.partition_request%ROWTYPE; decision text;
    request_digest text:=encode(public.digest(jsonb_build_object('id',p_id,'operation','factory-review','payload',p)::text,'sha256'),'hex');
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN RAISE EXCEPTION '需要真实测试数据审批权限' USING ERRCODE='42501'; END IF;
    IF p IS NULL OR (SELECT count(*) FROM jsonb_object_keys(p))<>3 OR NOT p ?& ARRAY['expected_submission_revision','decision','reason']
        OR jsonb_typeof(p->'expected_submission_revision') IS DISTINCT FROM 'number' OR (p->>'expected_submission_revision')::numeric NOT BETWEEN 1 AND 2147483647 OR trunc((p->>'expected_submission_revision')::numeric)<>(p->>'expected_submission_revision')::numeric
        OR jsonb_typeof(p->'decision') IS DISTINCT FROM 'string' OR p->>'decision' NOT IN ('APPROVE','REJECT') OR jsonb_typeof(p->'reason') IS DISTINCT FROM 'string' OR length(btrim(p->>'reason')) NOT BETWEEN 1 AND 300
        OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 THEN RAISE EXCEPTION '工厂审批参数无效' USING ERRCODE='22023'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,37));
    SELECT * INTO receipt FROM integration.partition_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.request_hash<>request_digest THEN RAISE EXCEPTION '工厂审批幂等冲突' USING ERRCODE='P0001'; END IF;
        RETURN (SELECT public_id FROM integration.execution_archive WHERE id=receipt.archive_id);
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    SELECT * INTO s FROM integration.factory_submission WHERE archive_id=a.id FOR UPDATE;
    IF s.id IS NULL OR s.status<>'PENDING' OR s.revision<>(p->>'expected_submission_revision')::integer THEN RAISE EXCEPTION '审批版本冲突或已有审批结果' USING ERRCODE='P0001'; END IF;
    decision:=CASE p->>'decision' WHEN 'APPROVE' THEN 'APPROVED' ELSE 'REJECTED' END;
    IF decision='APPROVED' THEN
        IF NOT EXISTS(SELECT 1 FROM iam.app_user u WHERE u.id=s.submitter_id AND u.enabled AND u.principal_kind='HUMAN' AND u.role IN ('SYSTEM_ADMIN','TEST_EXECUTOR')) THEN RAISE EXCEPTION '提交人权限已撤销' USING ERRCODE='42501'; END IF;
        SELECT * INTO STRICT snap FROM integration.factory_submission_revision WHERE submission_id=s.id AND revision=s.revision;
        IF snap.payload IS DISTINCT FROM integration.factory_summary(a.id) THEN RAISE EXCEPTION '原始证据、分区或核实版本已变化' USING ERRCODE='P0001'; END IF;
    END IF;
    INSERT INTO integration.factory_review(submission_id,revision,decision,actor_id,reason) VALUES(s.id,s.revision,decision,iam.current_user_id(),p->>'reason');
    UPDATE integration.factory_submission SET status=decision,updated_at=clock_timestamp() WHERE id=s.id;
    INSERT INTO integration.partition_request(actor_id,idempotency_key,request_hash,archive_id) VALUES(iam.current_user_id(),p_key,request_digest,a.id);
    RETURN a.public_id;
END $$;

CREATE FUNCTION integration.execution_data_domain(p_id bigint) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,test AS $$
    SELECT coalesce(ad.data_domain,ed.data_domain,CASE WHEN r.raw_payload->>'data_domain' IN ('RESEARCH','FACTORY')
        AND coalesce(r.raw_payload->>'data_domain_source','') NOT IN ('HISTORICAL_BENCH','NO_EVIDENCE') THEN r.raw_payload->>'data_domain' END,
        integration.domain_from_bench(coalesce(integration.trusted_bench(r.raw_payload->>'bench_id'),integration.trusted_bench(st.station_code))),'UNCLASSIFIED')
    FROM test.test_execution e
    LEFT JOIN integration.execution_archive a ON a.execution_uuid=e.acquisition_uuid OR (e.approved_submission_id IS NOT NULL AND a.submission_id=e.approved_submission_id)
    LEFT JOIN LATERAL (SELECT data_domain FROM integration.archive_domain_revision WHERE archive_id=a.id ORDER BY revision DESC LIMIT 1) ad ON true
    LEFT JOIN integration.execution_domain_evidence ed ON ed.execution_id=e.id
    LEFT JOIN integration.execution_import_record r ON r.execution_id=e.id
    LEFT JOIN test.station st ON st.id=e.station_id WHERE e.id=p_id AND (iam.is_internal_service() OR iam.can_access_campaign(e.campaign_id,false))
$$;
CREATE FUNCTION integration.interval_data_domain(p_id bigint) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,test AS $$
    SELECT coalesce(integration.execution_data_domain(ri.execution_id),'UNCLASSIFIED') FROM test.runtime_interval ri WHERE ri.id=p_id
$$;
CREATE FUNCTION integration.interruption_data_domain(p_id bigint) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,test,reliability AS $$
    SELECT coalesce(integration.execution_data_domain(ev.execution_id),'UNCLASSIFIED') FROM reliability.interruption i JOIN test.test_event ev ON ev.id=i.event_id WHERE i.id=p_id
$$;
ALTER FUNCTION reliability.interval_disposition(bigint,bigint) RENAME TO interval_disposition_pre_partition;
CREATE FUNCTION reliability.interval_disposition(p_interval_id bigint,p_scope_id bigint) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,reliability AS $$
    SELECT CASE WHEN integration.interval_data_domain(p_interval_id) IS DISTINCT FROM 'RESEARCH' THEN 'EXCLUDED'
        ELSE reliability.interval_disposition_pre_partition(p_interval_id,p_scope_id) END
$$;
-- Every route that publishes an R&D summary (including old direct APIs) hits this gate.
ALTER FUNCTION integration.archive_summary(bigint) RENAME TO archive_summary_pre_partition;
CREATE FUNCTION integration.archive_summary(p_archive_id bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
BEGIN
    IF (integration.archive_domain_internal(p_archive_id)).data_domain IS DISTINCT FROM 'RESEARCH' THEN
        RAISE EXCEPTION '仅研发分区可进入研发正式执行；工厂请使用独立审批流程' USING ERRCODE='22023';
    END IF;
    RETURN integration.archive_summary_pre_partition(p_archive_id);
END $$;

-- Old current cache remains stored as evidence, but cannot be presented as current.
ALTER TABLE reliability.current_mtbf_result ADD COLUMN data_domain_policy_version text;
ALTER TABLE reliability.current_mtbf_result ADD COLUMN data_domain_epoch bigint;
CREATE TABLE integration.campaign_domain_epoch (
    campaign_id bigint PRIMARY KEY REFERENCES test.test_campaign(id),
    revision bigint NOT NULL DEFAULT 0
);
ALTER TABLE integration.campaign_domain_epoch ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.campaign_domain_epoch FROM PUBLIC,__RP1_APP_USER__;
CREATE FUNCTION integration.campaign_domain_epoch(p_campaign bigint) RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
    SELECT coalesce((SELECT revision FROM integration.campaign_domain_epoch WHERE campaign_id=p_campaign),0)
    WHERE iam.is_internal_service() OR iam.can_access_campaign(p_campaign,false)
$$;
REVOKE ALL ON FUNCTION integration.campaign_domain_epoch(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.campaign_domain_epoch(bigint) TO __RP1_APP_USER__;
CREATE FUNCTION integration.invalidate_domain_mtbf() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,test,reliability AS $$
BEGIN
    INSERT INTO integration.campaign_domain_epoch(campaign_id,revision)
    SELECT DISTINCT e.campaign_id,1 FROM test.test_execution e JOIN integration.execution_archive a
        ON a.execution_uuid=e.acquisition_uuid OR (e.approved_submission_id IS NOT NULL AND a.submission_id=e.approved_submission_id)
        WHERE a.id=NEW.archive_id
    ON CONFLICT(campaign_id) DO UPDATE SET revision=integration.campaign_domain_epoch.revision+1;
    UPDATE reliability.current_mtbf_result SET data_domain_policy_version=NULL
    WHERE campaign_id IN (SELECT e.campaign_id FROM test.test_execution e JOIN integration.execution_archive a ON a.execution_uuid=e.acquisition_uuid OR (e.approved_submission_id IS NOT NULL AND a.submission_id=e.approved_submission_id) WHERE a.id=NEW.archive_id);
    RETURN NEW;
END $$;
CREATE TRIGGER domain_mtbf_invalidate AFTER INSERT ON integration.archive_domain_revision FOR EACH ROW EXECUTE FUNCTION integration.invalidate_domain_mtbf();

CREATE OR REPLACE FUNCTION test.sync_live_runs(p_connection_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, test, integration, iam, catalog
AS $$
DECLARE
    v_presence integration.hmi_presence%ROWTYPE;
    v_online boolean := false;
    v_run jsonb;
    v_partition integration.execution_domain_evidence%ROWTYPE;
    v_requested_domain text;
    v_requested_bench text;
    v_key text;
    v_uuid uuid;
    previous_run test.test_execution%ROWTYPE;
    v_state text;
    v_active numeric(20,3);
    v_campaign bigint;
    v_cycle bigint;
    v_asset bigint;
    v_configuration bigint;
    v_version bigint;
    v_station bigint;
    v_execution test.test_execution%ROWTYPE;
    v_status text;
    v_previous_conflict boolean;
    v_result jsonb := '[]'::jsonb;
BEGIN
    -- Global stale-run cleanup belongs to the worker, never to a login heartbeat.
    SELECT * INTO v_presence FROM integration.hmi_presence WHERE connection_id = p_connection_id;
    IF NOT FOUND THEN
        RETURN v_result;
    END IF;
    v_online := v_presence.disconnected_at IS NULL
        AND v_presence.last_seen_at > clock_timestamp() - interval '45 seconds';
    IF NOT v_online THEN
        RETURN v_result;
    END IF;

    FOR v_run IN SELECT value FROM jsonb_array_elements(v_presence.runs)
    LOOP
        -- Presence is still stored for all modes; only research gets a research execution projection.
        v_uuid:=nullif(v_run->>'execution_uuid','')::uuid;
        v_requested_bench:=coalesce(nullif(v_run->>'bench_id',''),nullif(v_presence.bench_id,''));
        SELECT d.* INTO v_partition FROM integration.execution_domain_evidence d JOIN test.test_execution e ON e.id=d.execution_id WHERE e.acquisition_uuid=v_uuid;
        IF v_partition.data_domain_source='EXPLICIT' AND
            ((v_run->>'data_domain' IS NOT NULL AND v_run->>'data_domain'<>v_partition.data_domain)
                OR (v_requested_bench IS NOT NULL AND v_requested_bench IS DISTINCT FROM v_partition.bench_id)) THEN
            RAISE EXCEPTION '本次采集模式和台架已固定，不得用后续心跳更换' USING ERRCODE='22023';
        END IF;
        IF v_uuid IS NOT NULL AND EXISTS(SELECT 1 FROM test.test_execution WHERE acquisition_uuid=v_uuid AND operator_id IS DISTINCT FROM v_presence.user_id) THEN
            RAISE EXCEPTION '采集身份属于其他操作账号' USING ERRCODE='42501';
        END IF;
        v_requested_domain:=coalesce(v_run->>'data_domain',v_partition.data_domain,integration.domain_from_bench(v_requested_bench));
        IF v_requested_domain <> 'RESEARCH' THEN CONTINUE; END IF;
        v_key := left(coalesce(v_run->>'session_key', ''), 128);
        v_state := lower(coalesce(v_run->>'state', ''));
        v_uuid := nullif(v_run->>'execution_uuid','')::uuid;
        v_active := least(315360000, greatest(0, coalesce((v_run->>'active_seconds')::numeric, 0)));
        IF v_key = '' THEN
            CONTINUE;
        END IF;
        IF v_uuid IS NOT NULL THEN PERFORM pg_advisory_xact_lock(hashtextextended(v_uuid::text,38)); END IF;
        IF v_uuid IS NOT NULL AND EXISTS(SELECT 1 FROM test.test_execution WHERE acquisition_uuid=v_uuid AND operator_id IS DISTINCT FROM v_presence.user_id) THEN
            RAISE EXCEPTION '采集身份属于其他操作账号' USING ERRCODE='42501';
        END IF;
        IF v_uuid IS NOT NULL AND EXISTS(SELECT 1 FROM integration.execution_archive WHERE execution_uuid=v_uuid AND owner_id IS DISTINCT FROM v_presence.user_id) THEN
            RAISE EXCEPTION '归档身份属于其他操作账号' USING ERRCODE='42501';
        END IF;

        -- A window can start a new acquisition before the next 10-second heartbeat.
        -- Close the previous online projection; it remains unapproved evidence.
        IF v_state='running' AND v_uuid IS NOT NULL THEN
            v_previous_conflict:=false;
            FOR previous_run IN SELECT * FROM test.test_execution
                WHERE live_connection_id=p_connection_id AND live_session_key=v_key
                  AND status IN ('RUNNING','PAUSED') AND acquisition_uuid IS NOT NULL
                  AND operator_id=v_presence.user_id AND approved_submission_id IS NULL
                  AND acquisition_uuid<>v_uuid FOR UPDATE
            LOOP
                IF NOT test.try_close_live_execution(previous_run.id,'COMPLETED',previous_run.reported_active_seconds) THEN
                    v_previous_conflict:=true;
                END IF;
            END LOOP;
            -- Preserve the new local acquisition; don't assign it the old execution's identity.
            IF v_previous_conflict THEN CONTINUE; END IF;
        END IF;
        SELECT * INTO v_execution
        FROM test.test_execution
        WHERE (v_uuid IS NOT NULL AND acquisition_uuid=v_uuid AND operator_id=v_presence.user_id)
           OR (live_connection_id=p_connection_id AND live_session_key=v_key AND (v_uuid IS NULL OR acquisition_uuid IS NULL))
        ORDER BY id DESC LIMIT 1 FOR UPDATE;
        IF v_execution.approved_submission_id IS NOT NULL AND v_execution.acquisition_uuid=v_uuid THEN
            v_result:=v_result||jsonb_build_array(jsonb_build_object('session_key',v_key,'execution_code',v_execution.execution_code,'execution_id',v_execution.public_id,'status',v_execution.status));
            v_execution:=NULL;
            CONTINUE;
        END IF;

        v_campaign := NULL;
        IF v_state = 'running'
           AND coalesce(v_run->>'campaign_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'cycle_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'asset_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'configuration_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'test_case_version_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'station_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN
            SELECT c.id, cy.id, a.id, cfg.id, ver.id, s.id
            INTO v_campaign, v_cycle, v_asset, v_configuration, v_version, v_station
            FROM test.test_campaign c
            JOIN test.test_cycle cy ON cy.public_id = (v_run->>'cycle_id')::uuid AND cy.campaign_id = c.id
            JOIN test.asset a ON a.public_id = (v_run->>'asset_id')::uuid
            JOIN test.configuration_snapshot cfg ON cfg.public_id = (v_run->>'configuration_id')::uuid AND cfg.asset_id = a.id
            JOIN catalog.test_case_version ver ON ver.public_id = (v_run->>'test_case_version_id')::uuid
            JOIN test.station s ON s.public_id = (v_run->>'station_id')::uuid
            WHERE c.public_id = (v_run->>'campaign_id')::uuid;
        END IF;

        IF v_state = 'running' AND v_campaign IS NOT NULL THEN
            IF v_execution.acquisition_uuid=v_uuid AND ROW(v_execution.campaign_id,v_execution.cycle_id,v_execution.asset_id,v_execution.configuration_snapshot_id,v_execution.test_case_version_id)
                IS DISTINCT FROM ROW(v_campaign,v_cycle,v_asset,v_configuration,v_version) THEN
                RAISE EXCEPTION '采集身份与执行上下文不一致' USING ERRCODE='22023';
            END IF;
            IF v_execution.id IS NOT NULL AND (v_execution.status IN ('RUNNING', 'PAUSED') OR v_execution.acquisition_uuid=v_uuid) THEN
                IF v_execution.status NOT IN ('RUNNING','PAUSED') THEN
                    UPDATE test.runtime_interval SET voided=true
                    WHERE execution_id=v_execution.id AND source_method='hmi-live-heartbeat' AND NOT voided;
                END IF;
                UPDATE test.test_execution
                SET status = 'RUNNING',
                    normalized_ended_at=NULL,
                    acquisition_uuid=coalesce(acquisition_uuid,v_uuid),
                    live_connection_id=p_connection_id,live_session_key=v_key,
                    reported_active_seconds = v_active,
                    received_at = clock_timestamp(),
                    operator_id = coalesce(operator_id, v_presence.user_id)
                WHERE id = v_execution.id
                RETURNING * INTO v_execution;
            ELSE
                INSERT INTO test.test_execution(
                    execution_code, campaign_id, cycle_id, asset_id, configuration_snapshot_id,
                    test_case_version_id, station_id, operator_id, status,
                    normalized_started_at, code_timestamp, source_time, received_at,
                    clock_quality, data_quality, live_connection_id, live_session_key,
                    reported_active_seconds, acquisition_uuid
                ) VALUES (
                    'GENERATED', v_campaign, v_cycle, v_asset, v_configuration,
                    v_version, v_station, v_presence.user_id, 'RUNNING',
                    clock_timestamp(), clock_timestamp(), clock_timestamp(), clock_timestamp(),
                    'VALID', 'PARTIAL', p_connection_id, v_key, v_active, v_uuid
                )
                RETURNING * INTO v_execution;
            END IF;
        ELSIF v_execution.id IS NOT NULL AND v_execution.status = 'RUNNING' THEN
            v_status := CASE v_state
                WHEN 'paused' THEN 'PAUSED'
                WHEN 'fault' THEN 'FAILED'
                ELSE 'COMPLETED'
            END;
            IF v_status='PAUSED' THEN
                UPDATE test.test_execution SET status='PAUSED',reported_active_seconds=v_active,
                    received_at=clock_timestamp(),normalized_ended_at=NULL WHERE id=v_execution.id;
            ELSE
                -- Preserve the newest telemetry even when interval materialization needs review.
                UPDATE test.test_execution SET reported_active_seconds=v_active,received_at=clock_timestamp()
                    WHERE id=v_execution.id;
                PERFORM test.try_close_live_execution(v_execution.id,v_status,v_active);
            END IF;
            SELECT * INTO v_execution FROM test.test_execution WHERE id=v_execution.id;
        END IF;

        IF v_execution.id IS NOT NULL THEN
            INSERT INTO integration.execution_domain_evidence(execution_id,data_domain,data_domain_source,bench_id,evidence)
            VALUES(v_execution.id,'RESEARCH',CASE WHEN v_run->>'data_domain'='RESEARCH' THEN 'EXPLICIT' ELSE 'HISTORICAL_BENCH' END,
                coalesce(nullif(v_run->>'bench_id',''),nullif(v_presence.bench_id,'')),jsonb_build_object('connection_id',p_connection_id,'run',v_run))
            ON CONFLICT(execution_id) DO NOTHING;
        END IF;
        IF v_execution.id IS NOT NULL AND v_execution.live_session_key = v_key THEN
            v_result := v_result || jsonb_build_array(jsonb_build_object(
                'session_key', v_key,
                'execution_code', v_execution.execution_code,
                'execution_id', v_execution.public_id,
                'status', v_execution.status
            ));
        END IF;
        v_execution := NULL;
        v_campaign := NULL;
    END LOOP;
    RETURN v_result;
END
$$;

REVOKE ALL ON FUNCTION integration.archive_create_pre_partition(jsonb,text),integration.archive_import_pre_partition(jsonb,text),integration.archive_edit_pre_partition(uuid,text,jsonb,text),integration.archive_summary_pre_partition(bigint),integration.archive_domain_internal(bigint),integration.factory_summary(bigint),integration.invalidate_domain_mtbf(),reliability.interval_disposition_pre_partition(bigint,bigint) FROM PUBLIC,__RP1_APP_USER__;
REVOKE ALL ON FUNCTION integration.domain_from_bench(text),integration.archive_domain_info(bigint),integration.archive_create(jsonb,text),integration.archive_import(jsonb,text),integration.archive_edit(uuid,text,jsonb,text),integration.archive_classify_domain(uuid,jsonb,text),integration.factory_submit(uuid,jsonb,text),integration.factory_review_archive(uuid,jsonb,text),integration.execution_data_domain(bigint),integration.interval_data_domain(bigint),integration.interruption_data_domain(bigint),integration.archive_summary(bigint),reliability.interval_disposition(bigint,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.domain_from_bench(text),integration.archive_domain_info(bigint),integration.archive_create(jsonb,text),integration.archive_import(jsonb,text),integration.archive_edit(uuid,text,jsonb,text),integration.archive_classify_domain(uuid,jsonb,text),integration.factory_submit(uuid,jsonb,text),integration.factory_review_archive(uuid,jsonb,text),integration.execution_data_domain(bigint),integration.interval_data_domain(bigint),integration.interruption_data_domain(bigint),integration.archive_summary(bigint),reliability.interval_disposition(bigint,bigint) TO __RP1_APP_USER__;

-- Narrow RLS-aware accessors retain legacy factory approval visibility for VIEWER.
-- They expose only a readable archive's bound snapshot, never arbitrary submissions.
CREATE FUNCTION integration.archive_legacy_factory_info(p_id bigint)
RETURNS TABLE(submission_id bigint,revision integer,status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
    SELECT s.id,s.revision,s.status FROM integration.execution_archive a JOIN integration.data_submission s ON s.id=a.submission_id
    WHERE a.id=p_id AND integration.can_read_archive(a.id)
$$;
CREATE FUNCTION integration.archive_legacy_factory_summary(p_id bigint) RETURNS TABLE(payload jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
    SELECT r.payload FROM integration.execution_archive a JOIN integration.data_submission s ON s.id=a.submission_id
    JOIN integration.data_submission_revision r ON r.submission_id=s.id AND r.revision=s.revision
    WHERE a.id=p_id AND integration.can_read_archive(a.id)
$$;
REVOKE ALL ON FUNCTION integration.archive_legacy_factory_info(bigint),integration.archive_legacy_factory_summary(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_legacy_factory_info(bigint),integration.archive_legacy_factory_summary(bigint) TO __RP1_APP_USER__;

-- Existing reliability read models are research-only. Base-table RLS still applies.
CREATE VIEW test.research_execution WITH (security_invoker=true) AS
    SELECT * FROM test.test_execution WHERE integration.execution_data_domain(id)='RESEARCH';
REVOKE ALL ON test.research_execution FROM PUBLIC;
GRANT SELECT ON test.research_execution TO __RP1_APP_USER__;
