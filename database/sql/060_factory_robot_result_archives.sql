-- Dedicated 17-column factory result summaries. Never creates formal executions,
-- exposure assessments, interruptions or research MTBF inputs.
CREATE TABLE integration.robot_result_source (
 source_id text PRIMARY KEY CHECK(source_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$'),
 owner_id bigint NOT NULL REFERENCES iam.app_user(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE integration.factory_robot_presence (
 source_id text NOT NULL REFERENCES integration.robot_result_source(source_id),
 robot_id text NOT NULL CHECK(robot_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 serial_number text NOT NULL CHECK(length(btrim(serial_number)) BETWEEN 1 AND 200),
 model text NOT NULL CHECK(model IN ('24轴','28轴')),
 station_name text NOT NULL CHECK(length(btrim(station_name)) BETWEEN 1 AND 128),
 source_kind text NOT NULL CHECK(source_kind IN ('real','simulated')),
 online boolean NOT NULL, data_fresh boolean, observed_at timestamptz,
 last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(source_id,robot_id), UNIQUE(source_id,serial_number)
);
CREATE TABLE integration.robot_result_archive (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 public_id uuid NOT NULL UNIQUE,
 source_id text NOT NULL REFERENCES integration.robot_result_source(source_id),
 robot_id text NOT NULL,
 owner_id bigint NOT NULL REFERENCES iam.app_user(id),
 protocol text NOT NULL CHECK(protocol='reliability_v1'),
 source_kind text NOT NULL CHECK(source_kind IN ('real','simulated')),
 serial_number text NOT NULL CHECK(length(btrim(serial_number)) BETWEEN 1 AND 200),
 model text NOT NULL CHECK(model IN ('24轴','28轴')),
 filename text NOT NULL CHECK(length(filename) BETWEEN 5 AND 200 AND filename !~ '[/\\]' AND lower(filename) LIKE '%.csv'),
 size_bytes integer NOT NULL CHECK(size_bytes BETWEEN 1 AND 4194304),
 sha256 text NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 verified boolean NOT NULL CHECK(verified), completed boolean NOT NULL CHECK(completed),
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 300),
 columns jsonb NOT NULL CHECK(jsonb_typeof(columns)='array' AND jsonb_array_length(columns)=17),
 row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 1000),
 status text NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING','APPROVED','REJECTED')),
 revision integer NOT NULL DEFAULT 1 CHECK(revision BETWEEN 1 AND 2),
 previous_archive_id bigint REFERENCES integration.robot_result_archive(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 FOREIGN KEY(source_id,robot_id) REFERENCES integration.factory_robot_presence(source_id,robot_id)
);
CREATE INDEX robot_result_archive_source ON integration.robot_result_archive(source_id,robot_id,id DESC);
CREATE INDEX robot_result_archive_cards ON integration.robot_result_archive(source_id,robot_id,serial_number,status,id DESC);
CREATE INDEX robot_result_archive_digest ON integration.robot_result_archive(source_id,sha256,id DESC);
CREATE TABLE integration.robot_result_row (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 archive_id bigint NOT NULL REFERENCES integration.robot_result_archive(id),
 source_id text NOT NULL REFERENCES integration.robot_result_source(source_id),
 record_id text NOT NULL CHECK(length(btrim(record_id)) BETWEEN 1 AND 200),
 version integer NOT NULL CHECK(version>0),
 serial_number text NOT NULL, model text NOT NULL,
 suite text NOT NULL, level text NOT NULL CHECK(level IN ('项目','子项')),
 case_code text NOT NULL, original_check_id text NOT NULL, test_name text NOT NULL,
 verdict text NOT NULL CHECK(verdict IN ('未测试','通过','不通过','待复核','中断','不适用')),
 cell_values jsonb NOT NULL CHECK(jsonb_typeof(cell_values)='array' AND jsonb_array_length(cell_values)=17),
 row_sha256 text NOT NULL CHECK(row_sha256 ~ '^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(source_id,record_id,version), UNIQUE(archive_id,record_id)
);
CREATE TABLE integration.robot_result_review (
 archive_id bigint PRIMARY KEY REFERENCES integration.robot_result_archive(id),
 revision integer NOT NULL CHECK(revision=1),
 actor_id bigint NOT NULL REFERENCES iam.app_user(id),
 decision text NOT NULL CHECK(decision IN ('APPROVE','REJECT')),
 reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 1 AND 300),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX robot_result_row_projects ON integration.robot_result_row(archive_id,case_code,id)
 WHERE suite='SYS' AND level='项目' AND verdict NOT IN ('未测试','不适用');
CREATE TABLE integration.robot_result_request (
 actor_id bigint NOT NULL REFERENCES iam.app_user(id), idempotency_key text NOT NULL,
 operation text NOT NULL CHECK(operation IN ('CREATE','REVIEW')),
 request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[0-9a-f]{64}$'),
 archive_id bigint NOT NULL REFERENCES integration.robot_result_archive(id),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(actor_id,idempotency_key)
);

CREATE FUNCTION integration.robot_result_reader() RETURNS boolean LANGUAGE sql STABLE AS $$
 SELECT coalesce(iam.current_principal_kind()='HUMAN' AND iam.current_user_role() IN
  ('SYSTEM_ADMIN','TEST_EXECUTOR','FACTORY_REVIEWER','VIEWER'),false)
$$;
CREATE FUNCTION integration.can_read_robot_result(p_id bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
 SELECT integration.robot_result_reader() AND EXISTS(SELECT 1 FROM integration.robot_result_archive a
  WHERE a.id=p_id AND (iam.current_user_role()<>'VIEWER' OR a.status='APPROVED'))
$$;
CREATE FUNCTION integration.robot_result_actor_name(p_archive bigint,p_actor bigint) RETURNS text
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
 SELECT display_name FROM iam.app_user WHERE id=p_actor AND integration.can_read_robot_result(p_archive)
$$;
CREATE FUNCTION integration.robot_result_source_enabled(p_source text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
 SELECT EXISTS(SELECT 1 FROM integration.robot_result_source s JOIN iam.app_user u ON u.id=s.owner_id
  WHERE s.source_id=p_source AND u.enabled AND u.principal_kind='HUMAN' AND u.role IN ('SYSTEM_ADMIN','TEST_EXECUTOR'))
$$;
CREATE FUNCTION integration.robot_result_case_cards(p_keys jsonb)
RETURNS TABLE(source_id text,robot_id text,serial_number text,case_code text,test_name text,
 record_count bigint,latest_status text,approval_status text,verdict text,latest_archive_id uuid)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration SET statement_timeout='3s' AS $$
BEGIN
 IF NOT integration.robot_result_reader() OR jsonb_typeof(p_keys) IS DISTINCT FROM 'array' OR jsonb_array_length(p_keys)>200 THEN
  RAISE EXCEPTION '工厂卡片查询权限或数量无效' USING ERRCODE='42501'; END IF;
 RETURN QUERY
 WITH grouped AS (
  SELECT r.source_id,a.robot_id,r.serial_number,r.case_code,count(*) AS record_count,max(r.id) AS latest_id
  FROM jsonb_to_recordset(p_keys) AS k(source_id text,robot_id text,serial_number text)
  JOIN integration.robot_result_archive a ON a.source_id=k.source_id AND a.robot_id=k.robot_id AND a.serial_number=k.serial_number
   AND a.status='APPROVED' AND a.source_kind='real'
  JOIN integration.robot_result_row r ON r.archive_id=a.id AND r.suite='SYS' AND r.level='项目' AND r.verdict NOT IN ('未测试','不适用')
  WHERE NOT EXISTS(SELECT 1 FROM integration.robot_result_row newer
   WHERE newer.source_id=r.source_id AND newer.record_id=r.record_id AND newer.version>r.version)
  GROUP BY r.source_id,a.robot_id,r.serial_number,r.case_code
 )
 SELECT g.source_id,g.robot_id,g.serial_number,g.case_code,latest.test_name,g.record_count,
  CASE latest.verdict WHEN '待复核' THEN '待核实' WHEN '中断' THEN '中断' ELSE '已完成' END,
  'APPROVED'::text,latest.verdict,a.public_id
 FROM grouped g JOIN integration.robot_result_row latest ON latest.id=g.latest_id
 JOIN integration.robot_result_archive a ON a.id=latest.archive_id
 ORDER BY g.source_id,g.robot_id,g.case_code;
END
$$;

CREATE FUNCTION integration.robot_result_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='UPDATE' AND TG_TABLE_NAME='robot_result_archive'
  AND (to_jsonb(NEW)-'status'-'revision')=(to_jsonb(OLD)-'status'-'revision')
  AND OLD.status='PENDING' AND NEW.status IN ('APPROVED','REJECTED') AND NEW.revision=OLD.revision+1 THEN
  RETURN NEW;
 END IF;
 RAISE EXCEPTION '工厂原始汇总、来源身份、版本和审核证据不可改写或删除' USING ERRCODE='42501';
END $$;

DO $$ DECLARE target text; BEGIN
 FOREACH target IN ARRAY ARRAY['robot_result_source','factory_robot_presence','robot_result_archive','robot_result_row','robot_result_review','robot_result_request'] LOOP
  EXECUTE format('ALTER TABLE integration.%I ENABLE ROW LEVEL SECURITY',target);
  EXECUTE format('REVOKE ALL ON integration.%I FROM PUBLIC,__RP1_APP_USER__',target);
  EXECUTE format('GRANT SELECT ON integration.%I TO __RP1_APP_USER__',target);
  -- Presence is a bounded current-state projection, not an append-only log of
  -- every 10-second heartbeat. Source bindings and result evidence stay audited.
  IF target<>'factory_robot_presence' THEN
   EXECUTE format('CREATE TRIGGER robot_result_audit AFTER INSERT OR UPDATE OR DELETE ON integration.%I FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change()',target);
  END IF;
  IF target<>'factory_robot_presence' THEN
   EXECUTE format('CREATE TRIGGER robot_result_immutable BEFORE UPDATE OR DELETE ON integration.%I FOR EACH ROW EXECUTE FUNCTION integration.robot_result_immutable()',target);
  END IF;
 END LOOP;
END $$;
CREATE POLICY robot_source_read ON integration.robot_result_source FOR SELECT USING(integration.robot_result_reader());
CREATE POLICY robot_presence_read ON integration.factory_robot_presence FOR SELECT USING(integration.robot_result_reader());
CREATE POLICY robot_archive_read ON integration.robot_result_archive FOR SELECT USING(integration.can_read_robot_result(id));
CREATE POLICY robot_row_read ON integration.robot_result_row FOR SELECT USING(integration.can_read_robot_result(archive_id));
CREATE POLICY robot_review_read ON integration.robot_result_review FOR SELECT USING(integration.can_read_robot_result(archive_id));
CREATE POLICY robot_request_read ON integration.robot_result_request FOR SELECT USING(actor_id=iam.current_user_id());

CREATE FUNCTION integration.bind_robot_result_source(p_source text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE owner bigint;
BEGIN
 IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business()
  OR NOT EXISTS(SELECT 1 FROM iam.app_user WHERE id=iam.current_user_id() AND enabled
    AND role IN ('SYSTEM_ADMIN','TEST_EXECUTOR') AND principal_kind='HUMAN') THEN
  RAISE EXCEPTION '需要有效执行员或管理员身份' USING ERRCODE='42501'; END IF;
 IF p_source !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$' THEN RAISE EXCEPTION '来源编号无效' USING ERRCODE='22023'; END IF;
 INSERT INTO integration.robot_result_source(source_id,owner_id) VALUES(p_source,iam.current_user_id()) ON CONFLICT DO NOTHING;
 SELECT owner_id INTO STRICT owner FROM integration.robot_result_source WHERE source_id=p_source FOR UPDATE;
 IF owner<>iam.current_user_id() THEN RAISE EXCEPTION '上位机来源已绑定其他账号' USING ERRCODE='P0001'; END IF;
END $$;

CREATE FUNCTION integration.record_factory_robot_presence(p jsonb) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE robot jsonb; old integration.factory_robot_presence%ROWTYPE; source text:=p->>'source_id';
BEGIN
 IF p->>'protocol'<>'reliability_v1' OR p->>'source_kind' NOT IN ('real','simulated')
  OR jsonb_typeof(p->'robots') IS DISTINCT FROM 'array' OR jsonb_array_length(p->'robots')>100 THEN
  RAISE EXCEPTION '整机心跳协议无效' USING ERRCODE='22023'; END IF;
 PERFORM integration.bind_robot_result_source(source);
 -- A full source snapshot: omissions go offline, but history is never deleted.
 UPDATE integration.factory_robot_presence SET online=false WHERE source_id=source AND online
  AND robot_id NOT IN (SELECT value->>'robot_id' FROM jsonb_array_elements(p->'robots'));
 FOR robot IN SELECT value FROM jsonb_array_elements(p->'robots') LOOP
  IF robot->>'robot_id' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    OR length(btrim(coalesce(robot->>'serial_number',''))) NOT BETWEEN 1 AND 200
    OR robot->>'model' NOT IN ('24轴','28轴')
    OR length(btrim(coalesce(robot->>'station_name',''))) NOT BETWEEN 1 AND 128
    OR jsonb_typeof(robot->'online') IS DISTINCT FROM 'boolean' THEN
   RAISE EXCEPTION '机器人身份或状态无效' USING ERRCODE='22023'; END IF;
  SELECT * INTO old FROM integration.factory_robot_presence WHERE source_id=source AND robot_id=robot->>'robot_id';
  IF FOUND AND (old.serial_number<>robot->>'serial_number' OR old.model<>robot->>'model') THEN
   RAISE EXCEPTION '机器人与SN或机型已有不同绑定；本期不支持换绑，请保留现有身份与历史并人工核对' USING ERRCODE='P0001'; END IF;
  INSERT INTO integration.factory_robot_presence(source_id,robot_id,serial_number,model,station_name,source_kind,online,data_fresh,observed_at)
  VALUES(source,robot->>'robot_id',robot->>'serial_number',robot->>'model',robot->>'station_name',p->>'source_kind',
   (robot->>'online')::boolean,(robot->>'data_fresh')::boolean,(robot->>'observed_at')::timestamptz)
  ON CONFLICT(source_id,robot_id) DO UPDATE SET model=EXCLUDED.model,station_name=EXCLUDED.station_name,
   source_kind=EXCLUDED.source_kind,online=EXCLUDED.online,data_fresh=EXCLUDED.data_fresh,
   observed_at=EXCLUDED.observed_at,last_seen_at=clock_timestamp();
 END LOOP;
 RETURN jsonb_array_length(p->'robots');
END $$;

CREATE FUNCTION integration.create_robot_result_archive(p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE source text:=p->>'source_id'; archive integration.robot_result_archive%ROWTYPE;
 oldrow integration.robot_result_row%ROWTYPE; robot integration.factory_robot_presence%ROWTYPE;
 request integration.robot_result_request%ROWTYPE; item jsonb; cells jsonb; number integer;
 digest text:=encode(public.digest((p-'public_id')::text,'sha256'),'hex');
BEGIN
 IF p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 OR p->>'protocol'<>'reliability_v1'
  OR p->'verified' IS DISTINCT FROM 'true'::jsonb OR p->'completed' IS DISTINCT FROM 'true'::jsonb
  OR p->>'source_kind' NOT IN ('real','simulated')
  OR jsonb_typeof(p->'rows') IS DISTINCT FROM 'array' OR jsonb_array_length(p->'rows') NOT BETWEEN 1 AND 1000
  OR octet_length(p::text)>8388608
  OR p->'columns' IS DISTINCT FROM '["记录编号","上位机编号","样品编号","机型","测试体系","测试分类","记录层级","测试项目编号","原始检查ID","测试内容","开始时间","结束时间","测试人员","实际结果","通过标准","是否通过","备注"]'::jsonb THEN
  RAISE EXCEPTION '需核实已结束且符合17列的专用工厂结果归档' USING ERRCODE='22023'; END IF;
 PERFORM integration.bind_robot_result_source(source);
 PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,60));
 SELECT * INTO request FROM integration.robot_result_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
 IF FOUND THEN
  IF request.operation<>'CREATE' OR request.request_sha256<>digest THEN RAISE EXCEPTION '工厂结果幂等键冲突' USING ERRCODE='P0001'; END IF;
  RETURN (SELECT public_id FROM integration.robot_result_archive WHERE id=request.archive_id);
 END IF;
 SELECT * INTO robot FROM integration.factory_robot_presence WHERE source_id=source AND robot_id=p->>'robot_id';
 IF FOUND AND (robot.serial_number<>p->>'serial_number' OR robot.model<>p->>'model') THEN
  RAISE EXCEPTION '上传对象与已登记来源机器人身份冲突' USING ERRCODE='P0001'; END IF;
 IF NOT FOUND THEN
  INSERT INTO integration.factory_robot_presence(source_id,robot_id,serial_number,model,station_name,source_kind,online)
   VALUES(source,p->>'robot_id',p->>'serial_number',p->>'model',source,p->>'source_kind',false);
 END IF;
 SELECT * INTO archive FROM integration.robot_result_archive WHERE source_id=source AND sha256=p->>'sha256' ORDER BY id DESC LIMIT 1;
 IF FOUND AND (archive.robot_id<>p->>'robot_id' OR archive.source_kind<>p->>'source_kind' OR archive.serial_number<>p->>'serial_number') THEN
  RAISE EXCEPTION '相同原件不能重新绑定其他身份或来源类型' USING ERRCODE='P0001'; END IF;
 IF archive.id IS NULL OR archive.status='REJECTED' THEN
  INSERT INTO integration.robot_result_archive(public_id,source_id,robot_id,owner_id,protocol,source_kind,serial_number,model,filename,size_bytes,sha256,
    verified,completed,reason,columns,row_count,previous_archive_id)
  VALUES((p->>'public_id')::uuid,source,p->>'robot_id',iam.current_user_id(),'reliability_v1',p->>'source_kind',p->>'serial_number',p->>'model',
    p->>'filename',(p->>'size_bytes')::integer,p->>'sha256',true,true,p->>'reason',p->'columns',jsonb_array_length(p->'rows'),archive.id)
  RETURNING * INTO archive;
  FOR item IN SELECT value FROM jsonb_array_elements(p->'rows') LOOP
   cells:=item->'values';
   IF jsonb_typeof(cells) IS DISTINCT FROM 'array' OR jsonb_array_length(cells)<>17
    OR EXISTS(SELECT 1 FROM jsonb_array_elements(cells) value WHERE jsonb_typeof(value)<>'string')
    OR item->>'record_id' IS DISTINCT FROM cells->>0 OR cells->>1 IS DISTINCT FROM source
    OR cells->>2 IS DISTINCT FROM archive.serial_number OR cells->>3 IS DISTINCT FROM archive.model
    OR cells->>6 NOT IN ('项目','子项') OR cells->>15 NOT IN ('未测试','通过','不通过','待复核','中断','不适用')
    OR length(btrim(coalesce(cells->>9,''))) NOT BETWEEN 1 AND 500
    OR cells->>4 IS DISTINCT FROM 'SYS' OR cells->>7 !~ '^SYS-00[1-9]$|^SYS-01[0-2]$'
    OR (p->>'source_kind'='real' AND cells::text ~* 'simulated|simulation|数据来源[：:]\s*模拟') THEN
    RAISE EXCEPTION '工厂汇总行身份、来源或格式无效' USING ERRCODE='22023'; END IF;
   SELECT * INTO oldrow FROM integration.robot_result_row WHERE source_id=source AND record_id=item->>'record_id' ORDER BY version DESC LIMIT 1;
   IF FOUND AND (oldrow.serial_number<>archive.serial_number OR oldrow.model<>archive.model OR oldrow.suite<>cells->>4
    OR oldrow.level<>cells->>6 OR oldrow.case_code<>cells->>7 OR oldrow.original_check_id<>cells->>8) THEN
    RAISE EXCEPTION '同一记录编号不可改绑SN、机型、体系、项目或子项身份' USING ERRCODE='P0001'; END IF;
   number:=coalesce(oldrow.version,0)+1;
   INSERT INTO integration.robot_result_row(archive_id,source_id,record_id,version,serial_number,model,suite,level,case_code,original_check_id,test_name,verdict,cell_values,row_sha256)
   VALUES(archive.id,source,item->>'record_id',number,archive.serial_number,archive.model,cells->>4,cells->>6,cells->>7,cells->>8,cells->>9,cells->>15,cells,
    encode(public.digest(cells::text,'sha256'),'hex'));
  END LOOP;
 END IF;
 INSERT INTO integration.robot_result_request(actor_id,idempotency_key,operation,request_sha256,archive_id)
  VALUES(iam.current_user_id(),p_key,'CREATE',digest,archive.id);
 RETURN archive.public_id;
END $$;

CREATE FUNCTION integration.review_robot_result_archive(p_id uuid,p jsonb,p_key text,p_file_sha256 text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE archive integration.robot_result_archive%ROWTYPE; request integration.robot_result_request%ROWTYPE;
 digest text:=encode(public.digest(jsonb_build_object('id',p_id,'payload',p)::text,'sha256'),'hex');
BEGIN
 IF NOT iam.can_review_factory() THEN RAISE EXCEPTION '需要工厂数据审核权限' USING ERRCODE='42501'; END IF;
 IF p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200 OR p->>'decision' NOT IN ('APPROVE','REJECT')
  OR length(btrim(coalesce(p->>'reason',''))) NOT BETWEEN 1 AND 300 OR (p->>'expected_revision')::integer<1 THEN
  RAISE EXCEPTION '审核参数无效' USING ERRCODE='22023'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,60));
 SELECT * INTO request FROM integration.robot_result_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
 IF FOUND THEN
  IF request.operation<>'REVIEW' OR request.request_sha256<>digest THEN RAISE EXCEPTION '审核幂等键冲突' USING ERRCODE='P0001'; END IF;
  RETURN (SELECT public_id FROM integration.robot_result_archive WHERE id=request.archive_id);
 END IF;
 SELECT * INTO archive FROM integration.robot_result_archive WHERE public_id=p_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION '工厂结果归档不存在' USING ERRCODE='P0002'; END IF;
 IF archive.status<>'PENDING' OR archive.revision<>(p->>'expected_revision')::integer THEN
  RAISE EXCEPTION '审核版本已变化或已有结果' USING ERRCODE='P0001'; END IF;
 IF p->>'decision'='APPROVE' THEN
  IF archive.source_kind='simulated' OR EXISTS(SELECT 1 FROM integration.robot_result_row
    WHERE archive_id=archive.id AND cell_values::text ~* 'simulated|simulation|数据来源[：:]\s*模拟') THEN
   RAISE EXCEPTION '模拟数据只能归档核对，不得批准入账' USING ERRCODE='22023'; END IF;
  IF p_file_sha256 IS DISTINCT FROM archive.sha256 THEN RAISE EXCEPTION '原件尚未通过哈希复核' USING ERRCODE='22023'; END IF;
  IF NOT EXISTS(SELECT 1 FROM iam.app_user WHERE id=archive.owner_id AND enabled AND principal_kind='HUMAN' AND role IN ('SYSTEM_ADMIN','TEST_EXECUTOR')) THEN
   RAISE EXCEPTION '原提交人已停用或失去提交权限，不能批准' USING ERRCODE='42501'; END IF;
 END IF;
 PERFORM set_config('app.change_reason',p->>'reason',true);
 INSERT INTO integration.robot_result_review(archive_id,revision,actor_id,decision,reason)
 VALUES(archive.id,archive.revision,iam.current_user_id(),p->>'decision',p->>'reason');
 UPDATE integration.robot_result_archive SET status=CASE p->>'decision' WHEN 'APPROVE' THEN 'APPROVED' ELSE 'REJECTED' END,revision=revision+1 WHERE id=archive.id;
 INSERT INTO integration.robot_result_request(actor_id,idempotency_key,operation,request_sha256,archive_id)
 VALUES(iam.current_user_id(),p_key,'REVIEW',digest,archive.id);
 RETURN archive.public_id;
END $$;

-- Callers can read their permitted evidence, but cannot directly mutate any
-- source binding, raw row, review or receipt using the application SQL role.
REVOKE ALL ON FUNCTION integration.robot_result_reader(),integration.can_read_robot_result(bigint),integration.robot_result_actor_name(bigint,bigint),
 integration.robot_result_source_enabled(text),
 integration.robot_result_case_cards(jsonb),
 integration.bind_robot_result_source(text),integration.record_factory_robot_presence(jsonb),integration.create_robot_result_archive(jsonb,text),
 integration.review_robot_result_archive(uuid,jsonb,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.robot_result_reader(),integration.can_read_robot_result(bigint),integration.robot_result_actor_name(bigint,bigint),
 integration.robot_result_source_enabled(text),
 integration.robot_result_case_cards(jsonb),
 integration.record_factory_robot_presence(jsonb),integration.create_robot_result_archive(jsonb,text),integration.review_robot_result_archive(uuid,jsonb,text,text)
 TO __RP1_APP_USER__;
REVOKE ALL ON FUNCTION integration.bind_robot_result_source(text) FROM __RP1_APP_USER__;
