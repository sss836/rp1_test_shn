-- An immutable segment set belongs to one existing, owner-scoped archive.
CREATE TABLE integration.archive_run_manifest (
 archive_id bigint PRIMARY KEY REFERENCES integration.execution_archive(id),
 payload jsonb NOT NULL, sha256 text NOT NULL CHECK(sha256~'^[0-9a-f]{64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE integration.archive_run_segment (
 archive_id bigint NOT NULL REFERENCES integration.archive_run_manifest(archive_id),
 sequence integer NOT NULL CHECK(sequence>=0 AND sequence<4096),
 received_bytes bigint NOT NULL DEFAULT 0 CHECK(received_bytes>=0),
 PRIMARY KEY(archive_id,sequence)
);
ALTER TABLE integration.archive_run_manifest ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.archive_run_segment ENABLE ROW LEVEL SECURITY;
CREATE POLICY run_manifest_read ON integration.archive_run_manifest FOR SELECT USING(integration.can_read_archive(archive_id));
CREATE POLICY run_segment_read ON integration.archive_run_segment FOR SELECT USING(integration.can_read_archive(archive_id));
GRANT SELECT ON integration.archive_run_manifest,integration.archive_run_segment TO __RP1_APP_USER__;
CREATE FUNCTION integration.register_run_manifest(p_id uuid,p jsonb,p_hash text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; old integration.archive_run_manifest%ROWTYPE; part jsonb; n integer:=0; prev text:='';
BEGIN
 PERFORM integration.archive_lock(p_id);
 SELECT * INTO STRICT a FROM integration.execution_archive WHERE public_id=p_id;
 SELECT * INTO old FROM integration.archive_run_manifest WHERE archive_id=a.id;
 IF FOUND THEN
  IF old.payload IS DISTINCT FROM p OR old.sha256<>p_hash THEN RAISE EXCEPTION '分段清单不可替换' USING ERRCODE='P0001'; END IF;
  RETURN;
 END IF;
 IF a.upload_state='COMPLETE' OR a.received_bytes<>0 OR a.verification_revision IS NOT NULL THEN RAISE EXCEPTION '已有上传或核实，不可切换格式' USING ERRCODE='P0001'; END IF;
 IF p->>'schema_version'<>'run-segments-v1' OR p->>'execution_uuid'<>a.execution_uuid::text OR p->>'logical_sha256'<>a.sha256
  OR (p->>'logical_size_bytes')::bigint<>a.size_bytes OR jsonb_array_length(p->'segments') NOT BETWEEN 1 AND 4096
  OR octet_length(p::text)>4194304 OR p_hash<>encode(public.digest(convert_to(p::text,'UTF8'),'sha256'),'hex') THEN
  RAISE EXCEPTION '分段身份或逻辑文件不一致' USING ERRCODE='22023'; END IF;
 FOR part IN SELECT value FROM jsonb_array_elements(p->'segments') LOOP
  IF (part->>'sequence')::integer<>n OR part->>'previous_sha256'<>prev OR part->>'sha256'!~'^[0-9a-f]{64}$'
   OR (part->>'size_bytes')::bigint NOT BETWEEN 1 AND 134234112 OR (part->>'sample_count')::bigint<0 THEN
   RAISE EXCEPTION '分段顺序、大小或哈希链无效' USING ERRCODE='22023'; END IF;
  prev:=part->>'sha256'; n:=n+1;
 END LOOP;
 INSERT INTO integration.archive_run_manifest VALUES(a.id,p,p_hash,clock_timestamp());
 INSERT INTO integration.archive_run_segment(archive_id,sequence) SELECT a.id,i FROM generate_series(0,n-1) i;
END $$;
CREATE FUNCTION integration.update_run_segment(p_id uuid,p_sequence integer,p_received bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
DECLARE aid bigint; maximum bigint; current bigint;
BEGIN
 PERFORM integration.archive_lock(p_id);
 SELECT id INTO STRICT aid FROM integration.execution_archive WHERE public_id=p_id;
 SELECT (payload->'segments'->p_sequence->>'size_bytes')::bigint INTO maximum FROM integration.archive_run_manifest WHERE archive_id=aid;
 SELECT received_bytes INTO current FROM integration.archive_run_segment WHERE archive_id=aid AND sequence=p_sequence FOR UPDATE;
 IF maximum IS NULL OR current IS NULL OR p_received<current OR p_received>maximum THEN RAISE EXCEPTION '分段偏移无效' USING ERRCODE='22023'; END IF;
 UPDATE integration.archive_run_segment SET received_bytes=p_received WHERE archive_id=aid AND sequence=p_sequence;
END $$;
REVOKE ALL ON FUNCTION integration.register_run_manifest(uuid,jsonb,text),integration.update_run_segment(uuid,integer,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.register_run_manifest(uuid,jsonb,text),integration.update_run_segment(uuid,integer,bigint) TO __RP1_APP_USER__;
CREATE FUNCTION integration.run_segments_complete(p_id bigint) RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,integration AS $$
 SELECT integration.can_read_archive(p_id) AND (
  NOT EXISTS(SELECT 1 FROM integration.archive_run_manifest WHERE archive_id=p_id)
  OR EXISTS(SELECT 1 FROM integration.archive_run_manifest m WHERE m.archive_id=p_id
   AND (SELECT count(*) FROM integration.archive_run_segment s WHERE s.archive_id=p_id)=jsonb_array_length(m.payload->'segments')
   AND NOT EXISTS(SELECT 1 FROM integration.archive_run_segment s WHERE s.archive_id=p_id
    AND s.received_bytes<>(m.payload->'segments'->s.sequence->>'size_bytes')::bigint)))
$$;
REVOKE ALL ON FUNCTION integration.run_segments_complete(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.run_segments_complete(bigint) TO __RP1_APP_USER__;
ALTER FUNCTION integration.archive_summary(bigint) RENAME TO archive_summary_pre_segments;
CREATE FUNCTION integration.archive_summary(p_id bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,integration AS $$
BEGIN
 IF NOT integration.run_segments_complete(p_id) THEN RAISE EXCEPTION '整次执行仍有缺失分段' USING ERRCODE='22023'; END IF;
 RETURN integration.archive_summary_pre_segments(p_id);
END $$;
REVOKE ALL ON FUNCTION integration.archive_summary_pre_segments(bigint) FROM PUBLIC,__RP1_APP_USER__;
GRANT EXECUTE ON FUNCTION integration.archive_summary(bigint) TO __RP1_APP_USER__;
ALTER FUNCTION integration.factory_summary(bigint) RENAME TO factory_summary_pre_segments;
CREATE FUNCTION integration.factory_summary(p_id bigint) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,integration AS $$
BEGIN
 IF NOT integration.run_segments_complete(p_id) THEN RAISE EXCEPTION '整次执行仍有缺失分段' USING ERRCODE='22023'; END IF;
 RETURN integration.factory_summary_pre_segments(p_id);
END $$;
REVOKE ALL ON FUNCTION integration.factory_summary_pre_segments(bigint),integration.factory_summary(bigint) FROM PUBLIC,__RP1_APP_USER__;
