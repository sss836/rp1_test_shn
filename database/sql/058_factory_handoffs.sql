-- Operational handoffs never change measurements, verification or formal verdicts.
CREATE TABLE integration.factory_handoff (
 archive_id bigint NOT NULL REFERENCES integration.execution_archive(id), revision integer NOT NULL,
 actor_id bigint NOT NULL REFERENCES iam.app_user(id), kind text NOT NULL CHECK(kind IN ('REWORK','HANDOFF','ACKNOWLEDGED')),
 note text NOT NULL CHECK(length(btrim(note)) BETWEEN 1 AND 1000),destination text NOT NULL DEFAULT '' CHECK(length(destination)<=100),
 request_key text NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(archive_id,revision), UNIQUE(archive_id,actor_id,request_key)
);
ALTER TABLE integration.factory_handoff ENABLE ROW LEVEL SECURITY;
CREATE POLICY factory_handoff_read ON integration.factory_handoff FOR SELECT USING(integration.can_read_archive(archive_id));
GRANT SELECT ON integration.factory_handoff TO __RP1_APP_USER__;
CREATE FUNCTION integration.factory_handoff(p_id uuid,p_kind text,p_note text,p_destination text,p_expected integer,p_key text) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE aid bigint; current_revision integer; old integration.factory_handoff%ROWTYPE;
BEGIN
 PERFORM integration.archive_lock(p_id);
 SELECT id INTO STRICT aid FROM integration.execution_archive WHERE public_id=p_id;
 IF (SELECT data_domain FROM integration.archive_domain_info(aid))<>'FACTORY' THEN RAISE EXCEPTION '仅适用于生产执行' USING ERRCODE='22023'; END IF;
 SELECT * INTO old FROM integration.factory_handoff WHERE archive_id=aid AND actor_id=iam.current_user_id() AND request_key=p_key;
 IF FOUND THEN
  IF old.kind<>p_kind OR old.note<>p_note OR old.destination<>p_destination THEN RAISE EXCEPTION '交接请求内容冲突' USING ERRCODE='P0001'; END IF;
  RETURN old.revision;
 END IF;
 SELECT coalesce(max(revision),0) INTO current_revision FROM integration.factory_handoff WHERE archive_id=aid;
 IF current_revision<>p_expected THEN RAISE EXCEPTION '交接版本已变化，请刷新' USING ERRCODE='P0001'; END IF;
 IF p_kind='HANDOFF' AND length(btrim(p_destination))=0 THEN RAISE EXCEPTION '交接须注明去向' USING ERRCODE='22023'; END IF;
 INSERT INTO integration.factory_handoff VALUES(aid,current_revision+1,iam.current_user_id(),p_kind,p_note,p_destination,p_key,clock_timestamp());
 RETURN current_revision+1;
END $$;
REVOKE ALL ON FUNCTION integration.factory_handoff(uuid,text,text,text,integer,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.factory_handoff(uuid,text,text,text,integer,text) TO __RP1_APP_USER__;
