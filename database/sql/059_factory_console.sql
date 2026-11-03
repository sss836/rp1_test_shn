-- Factory-only reviewer and operational visibility. No device commands or evidence rewrites.
ALTER TABLE iam.app_user DROP CONSTRAINT app_user_role_check;
ALTER TABLE iam.app_user ADD CONSTRAINT app_user_role_check
 CHECK(role IN ('VIEWER','TEST_EXECUTOR','SYSTEM_ADMIN','FACTORY_REVIEWER'));

CREATE FUNCTION iam.can_review_factory() RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT coalesce(iam.current_principal_kind()='HUMAN' AND iam.current_user_role() IN ('SYSTEM_ADMIN','TEST_EXECUTOR','FACTORY_REVIEWER'),false)
$$;
REVOKE ALL ON FUNCTION iam.can_review_factory() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION iam.can_review_factory() TO __RP1_APP_USER__;

-- Preserve the existing function OID used by RLS policies.
CREATE OR REPLACE FUNCTION iam.can_access_campaign(
    p_campaign_id bigint,
    p_require_edit boolean DEFAULT false
)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, iam
AS $$
    SELECT CASE
        WHEN iam.current_user_role()='FACTORY_REVIEWER' THEN false
        WHEN p_campaign_id IS NULL THEN false
        WHEN iam.is_system_admin() THEN true
        WHEN p_require_edit AND iam.current_user_role() <> 'TEST_EXECUTOR' THEN false
        ELSE EXISTS (
            SELECT 1
            FROM iam.user_campaign_access uca
            WHERE uca.user_id = iam.current_user_id()
              AND uca.campaign_id = p_campaign_id
              AND (NOT p_require_edit OR uca.access_level = 'EDIT')
        )
    END
$$;

CREATE OR REPLACE FUNCTION integration.can_read_archive(p_id bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
 SELECT coalesce(iam.is_internal_service() OR (iam.current_principal_kind()='HUMAN' AND CASE
 WHEN iam.current_user_role()='FACTORY_REVIEWER' THEN
   (integration.archive_domain_internal(p_id)).data_domain='FACTORY'
 ELSE iam.can_write_business() OR EXISTS (
   SELECT 1 FROM integration.execution_archive a JOIN integration.data_submission s ON s.id=a.submission_id
   WHERE a.id=p_id AND a.classification_revision IS NOT NULL AND s.status='APPROVED'
     AND iam.can_access_campaign(s.campaign_id,false)
 ) OR (iam.current_user_role()='VIEWER' AND EXISTS (
   SELECT 1 FROM integration.factory_submission s WHERE s.archive_id=p_id AND s.status='APPROVED'
 )) END),false)
$$;

CREATE OR REPLACE FUNCTION integration.factory_review_archive(p_id uuid,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; s integration.factory_submission%ROWTYPE; snap integration.factory_submission_revision%ROWTYPE; receipt integration.partition_request%ROWTYPE; decision text;
    request_digest text:=encode(public.digest(jsonb_build_object('id',p_id,'operation','factory-review','payload',p)::text,'sha256'),'hex');
BEGIN
    IF NOT iam.can_review_factory() THEN RAISE EXCEPTION '需要真实测试数据审批权限' USING ERRCODE='42501'; END IF;
    IF p IS NULL OR (SELECT count(*) FROM jsonb_object_keys(p))<>3 OR NOT p ?& ARRAY['expected_submission_revision','decision','reason']
        OR jsonb_typeof(p->'expected_submission_revision') IS DISTINCT FROM 'number' OR (p->>'expected_submission_revision')::numeric NOT BETWEEN 1 AND 2147483647 OR trunc((p->>'expected_submission_revision')::numeric)<>(p->>'expected_submission_revision')::numeric
        OR jsonb_typeof(p->'decision') IS DISTINCT FROM 'string' OR p->>'decision' NOT IN ('APPROVE','REJECT') OR jsonb_typeof(p->'reason') IS DISTINCT FROM 'string' OR length(btrim(p->>'reason')) NOT BETWEEN 1 AND 300
        OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 THEN RAISE EXCEPTION '工厂审批参数无效' USING ERRCODE='22023'; END IF;
    IF NOT EXISTS(SELECT 1 FROM integration.execution_archive target_archive WHERE target_archive.public_id=p_id
       AND (integration.archive_domain_internal(target_archive.id)).data_domain='FACTORY') THEN
       RAISE EXCEPTION '仅可审批工厂归档' USING ERRCODE='42501';
    END IF;
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

CREATE OR REPLACE FUNCTION iam.admin_create_user(
    p_username text,
    p_display_name text,
    p_role text,
    p_password_hash text,
    p_must_change_password boolean,
    p_request_id text,
    p_source_ip inet
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, iam, audit, public
AS $$
DECLARE
    v_user_id bigint;
    v_public_id uuid;
    v_username text := lower(btrim(p_username));
BEGIN
    IF NOT iam.is_system_admin() THEN
        RAISE EXCEPTION 'administrator role required' USING ERRCODE = '42501';
    END IF;
    IF v_username = '' OR p_role NOT IN ('VIEWER', 'TEST_EXECUTOR', 'SYSTEM_ADMIN','FACTORY_REVIEWER')
       OR p_password_hash NOT LIKE '$argon2id$%' THEN
        RAISE EXCEPTION 'invalid user parameters' USING ERRCODE = '22023';
    END IF;

    INSERT INTO iam.app_user(
        username, display_name, role, principal_kind, enabled, must_change_password
    ) VALUES (
        v_username, btrim(p_display_name), p_role, 'HUMAN', true,
        CASE WHEN p_role='FACTORY_REVIEWER' THEN true ELSE coalesce(p_must_change_password, true) END
    )
    RETURNING id, public_id INTO v_user_id, v_public_id;

    INSERT INTO iam.user_credential(user_id, password_hash, hash_algorithm)
    VALUES (v_user_id, p_password_hash, 'argon2id');

    INSERT INTO audit.security_event(
        event_type, actor_user_id, username, outcome,
        request_id, source_ip, metadata
    ) VALUES (
        'USER_CREATE', iam.current_user_id(), v_username, 'SUCCESS',
        coalesce(p_request_id, ''), p_source_ip,
        jsonb_build_object('user_public_id', v_public_id, 'role', p_role)
    );
    RETURN v_public_id;
END;
$$;


CREATE TABLE integration.factory_station_scope(
 station_id bigint PRIMARY KEY REFERENCES test.station(id),enabled boolean NOT NULL DEFAULT true,
 source text NOT NULL CHECK(source IN ('ADMIN','ARCHIVE_DOMAIN_EVIDENCE','ARCHIVE_HISTORICAL_EVIDENCE','EXPLICIT_HEARTBEAT','REGISTERED_FACTORY_NAMESPACE')),
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 300),
 assigned_by bigint REFERENCES iam.app_user(id),assigned_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE integration.factory_station_scope ENABLE ROW LEVEL SECURITY;
CREATE POLICY factory_scope_read ON integration.factory_station_scope FOR SELECT USING(iam.can_review_factory());
REVOKE ALL ON integration.factory_station_scope FROM PUBLIC,__RP1_APP_USER__;
GRANT SELECT ON integration.factory_station_scope TO __RP1_APP_USER__;
CREATE TRIGGER factory_scope_audit AFTER INSERT OR UPDATE OR DELETE ON integration.factory_station_scope
 FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

-- Seed only from existing archive evidence or an explicit FACTORY heartbeat, never a name prefix alone.
WITH latest AS (
 SELECT DISTINCT ON (archive_id) * FROM integration.archive_domain_revision ORDER BY archive_id,revision DESC
), evidence AS (
 SELECT s.id,min(CASE WHEN d.data_domain_source='HISTORICAL_BENCH' THEN 'ARCHIVE_HISTORICAL_EVIDENCE' ELSE 'ARCHIVE_DOMAIN_EVIDENCE' END) AS source
 FROM test.station s JOIN latest d ON upper(btrim(d.bench_id))=upper(btrim(s.station_code)) AND d.data_domain='FACTORY'
 WHERE (SELECT count(*) FROM test.station t WHERE upper(btrim(t.station_code))=upper(btrim(s.station_code)))=1 GROUP BY s.id
)
INSERT INTO integration.factory_station_scope(station_id,source,reason)
 SELECT id,source,'从既有工厂归档的真实台架分区证据初始化，可由管理员纠正' FROM evidence;
WITH latest AS (
 SELECT DISTINCT ON (installation_id) * FROM integration.hmi_presence ORDER BY installation_id,connected_at DESC,connection_id DESC
)
INSERT INTO integration.factory_station_scope(station_id,source,reason)
 SELECT DISTINCT s.id,'EXPLICIT_HEARTBEAT','从明确标记为工厂的既有上报初始化，可由管理员纠正'
 FROM test.station s JOIN latest p ON upper(btrim(p.bench_id))=upper(btrim(s.station_code))
 WHERE (SELECT count(*) FROM test.station t WHERE upper(btrim(t.station_code))=upper(btrim(s.station_code)))=1
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(p.runs) r WHERE r->>'data_domain'='FACTORY')
 AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p.runs) r WHERE r->>'data_domain'='RESEARCH')
 ON CONFLICT(station_id) DO NOTHING;

-- Existing factory stations have two retained registration signals: a trusted
-- historical factory namespace AND an explicit registered factory name.
INSERT INTO integration.factory_station_scope(station_id,source,reason)
 SELECT s.id,'REGISTERED_FACTORY_NAMESPACE','既有建档台架：唯一工厂命名空间编号与工厂名称双证据；由管理员可纠正，未重写测试分区'
 FROM test.station s
 WHERE integration.domain_from_bench(s.station_code)='FACTORY' AND btrim(s.name) LIKE '工厂%'
 AND (SELECT count(*) FROM test.station t WHERE upper(btrim(t.station_code))=upper(btrim(s.station_code)))=1
 ON CONFLICT(station_id) DO NOTHING;

CREATE FUNCTION integration.set_factory_station_scope(p_station uuid,p_enabled boolean,p_reason text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,iam,integration,test AS $$
DECLARE target bigint;
BEGIN
 IF NOT iam.is_system_admin() OR p_enabled IS NULL OR length(btrim(coalesce(p_reason,''))) NOT BETWEEN 1 AND 300 THEN
  RAISE EXCEPTION '仅管理员可明确设置工厂台架归属，并须说明依据' USING ERRCODE='42501'; END IF;
 SELECT id INTO STRICT target FROM test.station WHERE public_id=p_station;
 IF (SELECT count(*) FROM test.station WHERE upper(btrim(station_code))=(SELECT upper(btrim(station_code)) FROM test.station WHERE id=target))<>1 THEN
  RAISE EXCEPTION '台架编号匹配不唯一' USING ERRCODE='22023'; END IF;
 INSERT INTO integration.factory_station_scope(station_id,enabled,source,reason,assigned_by)
 VALUES(target,p_enabled,'ADMIN',p_reason,iam.current_user_id())
 ON CONFLICT(station_id) DO UPDATE SET enabled=EXCLUDED.enabled,source='ADMIN',reason=EXCLUDED.reason,
 assigned_by=EXCLUDED.assigned_by,assigned_at=clock_timestamp();
 RETURN p_station;
END $$;
REVOKE ALL ON FUNCTION integration.set_factory_station_scope(uuid,boolean,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.set_factory_station_scope(uuid,boolean,text) TO __RP1_APP_USER__;

CREATE FUNCTION integration.list_factory_benches(p_after uuid,p_limit integer)
RETURNS TABLE(station_id uuid,bench_id text,name text,site_name text,lab_name text,available boolean,connections jsonb)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam,test AS $$
 WITH selected AS (
  SELECT s.*,f.source AS scope_source,l.name AS lab_name,l.enabled AS lab_enabled,site.name AS site_name,site.enabled AS site_enabled
  FROM integration.factory_station_scope f JOIN test.station s ON s.id=f.station_id
  JOIN test.lab l ON l.id=s.lab_id JOIN test.site site ON site.id=l.site_id
  WHERE f.enabled AND iam.can_review_factory() AND (p_after IS NULL OR s.public_id>p_after)
  ORDER BY s.public_id LIMIT greatest(1,least(p_limit,101))
 )
 SELECT s.public_id,s.station_code,s.name,s.site_name,s.lab_name,s.enabled AND s.lab_enabled AND s.site_enabled,
 coalesce((SELECT jsonb_agg(c.payload ORDER BY c.last_seen_at DESC,c.installation_id) FROM (
  SELECT p.last_seen_at,p.installation_id,jsonb_build_object(
   'scope_source',s.scope_source,'scope_unconfirmed',EXISTS(SELECT 1 FROM jsonb_array_elements(p.runs) r WHERE coalesce(r->>'data_domain','UNCLASSIFIED')='UNCLASSIFIED'),
   'connection_id',p.connection_id,'installation_id',p.installation_id,'station_name',p.station_name,'bench_id',p.bench_id,
   'operator_name',u.username,'operator_display_name',u.display_name,
   'online',p.disconnected_at IS NULL AND p.last_seen_at>statement_timestamp()-interval '45 seconds'
       AND session.revoked_at IS NULL AND session.expires_at>statement_timestamp() AND u.enabled AND NOT u.must_change_password,
   'connected_at',p.connected_at,'last_seen_at',p.last_seen_at,'snapshot_at',p.snapshot_at,
   'runs',coalesce((SELECT jsonb_agg(r||jsonb_build_object('scope_unconfirmed',coalesce(r->>'data_domain','UNCLASSIFIED')<>'FACTORY')) FROM jsonb_array_elements(p.runs) r WHERE coalesce(r->>'data_domain','UNCLASSIFIED') IN ('FACTORY','UNCLASSIFIED')),'[]'::jsonb),
   'scope_conflict',EXISTS(SELECT 1 FROM jsonb_array_elements(p.runs) r WHERE r->>'data_domain'='RESEARCH' AND coalesce(r->>'state','unknown') NOT IN ('idle','completed')),
   'plc',p.plc
  ) AS payload
  FROM integration.hmi_presence p JOIN iam.app_user u ON u.id=p.user_id JOIN iam.auth_session session ON session.id=p.auth_session_id
  WHERE upper(btrim(p.bench_id))=upper(btrim(s.station_code))
    AND NOT EXISTS(SELECT 1 FROM integration.hmi_presence newer WHERE newer.installation_id=p.installation_id
      AND (newer.connected_at,newer.connection_id)>(p.connected_at,p.connection_id))
  ORDER BY p.last_seen_at DESC,p.installation_id LIMIT 16
 ) c),'[]'::jsonb)
 FROM selected s ORDER BY s.public_id
$$;
REVOKE ALL ON FUNCTION integration.list_factory_benches(uuid,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.list_factory_benches(uuid,integer) TO __RP1_APP_USER__;
CREATE INDEX ix_hmi_presence_bench_console ON integration.hmi_presence(upper(btrim(bench_id)),last_seen_at DESC);

-- Explicit local maintenance entry: fixed human reviewer only, never an administrator or an existing-user reset.
-- Only the database superuser may execute it; application sessions cannot obtain this capability.
CREATE FUNCTION iam.provision_factory_reviewer(p_username text,p_display_name text,p_password_hash text,p_reason text,p_request_id text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,iam,audit,public AS $$
DECLARE uid bigint;public_id uuid;username text:=lower(btrim(p_username));
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=session_user AND rolsuper) THEN
  RAISE EXCEPTION '显式数据库管理员维护身份必需' USING ERRCODE='42501'; END IF;
 IF username !~ '^[a-z0-9][a-z0-9._-]{0,127}$' OR length(btrim(coalesce(p_display_name,''))) NOT BETWEEN 1 AND 200
    OR p_password_hash NOT LIKE '$argon2id$%' OR length(btrim(coalesce(p_reason,''))) NOT BETWEEN 1 AND 300
    OR length(coalesce(p_request_id,'')) NOT BETWEEN 8 AND 200 THEN
  RAISE EXCEPTION '工厂账号维护参数无效' USING ERRCODE='22023'; END IF;
 INSERT INTO iam.app_user(username,display_name,role,principal_kind,enabled,must_change_password)
 VALUES(username,btrim(p_display_name),'FACTORY_REVIEWER','HUMAN',true,true) RETURNING id,iam.app_user.public_id INTO uid,public_id;
 INSERT INTO iam.user_credential(user_id,password_hash,hash_algorithm) VALUES(uid,p_password_hash,'argon2id');
 INSERT INTO audit.security_event(event_type,actor_user_id,username,outcome,request_id,metadata)
 VALUES('FACTORY_REVIEWER_PROVISION',NULL,username,'SUCCESS',p_request_id,
  jsonb_build_object('user_public_id',public_id,'role','FACTORY_REVIEWER','operator','local-maintenance','database_session_user',session_user,'reason',p_reason));
 RETURN public_id;
END $$;
REVOKE ALL ON FUNCTION iam.provision_factory_reviewer(text,text,text,text,text) FROM PUBLIC,__RP1_APP_USER__;
