ALTER TABLE integration.assistant_job DROP CONSTRAINT assistant_job_state_check;
ALTER TABLE integration.assistant_job ADD CONSTRAINT assistant_job_state_check CHECK(state IN
 ('QUEUED','RUNNING','WAITING_PERMISSION','COMPLETED','FAILED','TIMED_OUT','INTERRUPTED','CANCELLED'));
ALTER TABLE integration.assistant_job
 ADD COLUMN active_elapsed_ms integer NOT NULL DEFAULT 0 CHECK(active_elapsed_ms BETWEEN 0 AND 120000),
 ADD COLUMN active_started_at timestamptz,
 ADD COLUMN read_count integer NOT NULL DEFAULT 0 CHECK(read_count BETWEEN 0 AND 12),
 ADD COLUMN resume_count integer NOT NULL DEFAULT 0 CHECK(resume_count>=0),
 ADD COLUMN resume_state jsonb NOT NULL DEFAULT '{}' CHECK(octet_length(resume_state::text)<=8388608),
 ADD COLUMN permission_wait_started_at timestamptz,
 ADD COLUMN waiting_elapsed_ms bigint NOT NULL DEFAULT 0 CHECK(waiting_elapsed_ms>=0);
CREATE TABLE integration.assistant_read_permission(
 id uuid PRIMARY KEY,job_id uuid NOT NULL REFERENCES integration.assistant_job(id),
 user_id bigint NOT NULL REFERENCES iam.app_user(id),origin_session_id uuid NOT NULL,
 installation_id uuid NOT NULL,window_id text NOT NULL,
 arguments jsonb NOT NULL,request_payload jsonb NOT NULL,request_sha256 text NOT NULL CHECK(request_sha256~'^[a-f0-9]{64}$'),
 state text NOT NULL CHECK(state IN ('pending','approved','denied','expired','completed','cancelled')),
 approval_origin text CHECK(approval_origin IN ('default','reuse','explicit')),
 approved_scope text CHECK(approved_scope IN ('file','directory','all')),
 scope_root text,decision_key uuid UNIQUE,decision_sha256 text,
 result jsonb,result_sha256 text,previous_request_id uuid,
 created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT(now()+interval '30 minutes'),
 decided_at timestamptz,completed_at timestamptz,
 CHECK((result IS NULL)=(result_sha256 IS NULL)),
 CHECK(result IS NULL OR octet_length(result::text)<=262144),
 CHECK(state<>'approved' OR approved_scope IS NOT NULL)
);
CREATE INDEX assistant_read_permission_job ON integration.assistant_read_permission(job_id,created_at,id);
ALTER TABLE integration.assistant_read_permission ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.assistant_read_permission FROM PUBLIC,__RP1_APP_USER__;
GRANT SELECT,INSERT ON integration.assistant_read_permission TO __RP1_APP_USER__;
GRANT UPDATE(state,approval_origin,approved_scope,scope_root,decision_key,decision_sha256,result,result_sha256,decided_at,completed_at)
 ON integration.assistant_read_permission TO __RP1_APP_USER__;
CREATE POLICY read_permission_owner ON integration.assistant_read_permission USING(user_id=iam.current_user_id())
 WITH CHECK(user_id=iam.current_user_id() AND iam.can_write_business() AND EXISTS(
 SELECT 1 FROM integration.assistant_job j WHERE j.id=job_id AND j.user_id=iam.current_user_id()
 AND j.origin_session_id=assistant_read_permission.origin_session_id));
CREATE FUNCTION integration.assistant_execution_session_active(p_id uuid) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
 SELECT EXISTS(SELECT 1 FROM integration.assistant_job j JOIN iam.app_user u ON u.id=j.user_id
 JOIN iam.auth_session s ON s.public_id=j.origin_session_id AND s.user_id=j.user_id
 WHERE j.id=p_id AND j.user_id=iam.current_user_id() AND u.enabled AND u.principal_kind='HUMAN'
 AND u.role IN ('TEST_EXECUTOR','SYSTEM_ADMIN') AND s.revoked_at IS NULL AND s.expires_at>now())
$$;
CREATE FUNCTION integration.assistant_take_read(p_id uuid) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE result integer;
BEGIN
 IF NOT iam.can_write_business() THEN RAISE EXCEPTION 'read authorization revoked' USING ERRCODE='42501'; END IF;
 UPDATE integration.assistant_job SET read_count=read_count+1 WHERE id=p_id AND user_id=iam.current_user_id()
 AND state='RUNNING' AND NOT cancel_requested AND deadline>clock_timestamp() AND read_count<12
 AND iam.can_write_business() RETURNING read_count INTO result;
 IF result IS NULL THEN RAISE EXCEPTION 'read budget or execution ended' USING ERRCODE='P0001'; END IF;
 RETURN result;
END $$;
CREATE FUNCTION integration.assistant_permission_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
 IF (NEW.job_id,NEW.user_id,NEW.origin_session_id,NEW.installation_id,NEW.window_id,NEW.arguments,NEW.request_payload,NEW.request_sha256,NEW.expires_at)
 IS DISTINCT FROM (OLD.job_id,OLD.user_id,OLD.origin_session_id,OLD.installation_id,OLD.window_id,OLD.arguments,OLD.request_payload,OLD.request_sha256,OLD.expires_at) THEN
  RAISE EXCEPTION 'permission identity immutable' USING ERRCODE='23514';
 END IF;
 IF OLD.result_sha256 IS NOT NULL AND (NEW.result,NEW.result_sha256) IS DISTINCT FROM(OLD.result,OLD.result_sha256) THEN
  RAISE EXCEPTION 'local read receipt immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER permission_immutable BEFORE UPDATE ON integration.assistant_read_permission FOR EACH ROW
 EXECUTE FUNCTION integration.assistant_permission_immutable();
REVOKE ALL ON FUNCTION integration.assistant_execution_session_active(uuid),integration.assistant_take_read(uuid),integration.assistant_permission_immutable() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_execution_session_active(uuid),integration.assistant_take_read(uuid) TO __RP1_APP_USER__;

CREATE FUNCTION integration.assistant_pause(p_job uuid,p_user uuid,p_result jsonb,p_resume jsonb) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
BEGIN
 UPDATE integration.assistant_job j SET state='WAITING_PERMISSION',result=p_result,resume_state=p_resume,
  active_elapsed_ms=least(120000,active_elapsed_ms+coalesce(greatest(0,extract(epoch FROM(clock_timestamp()-active_started_at))*1000)::int,0)),
  active_started_at=NULL,deadline=NULL,permission_wait_started_at=coalesce(permission_wait_started_at,now())
 FROM iam.app_user u WHERE j.id=p_job AND u.id=j.user_id AND u.public_id=p_user
 AND j.state IN ('RUNNING','WAITING_PERMISSION') AND NOT j.cancel_requested;
 RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION integration.assistant_pause(uuid,uuid,jsonb,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_pause(uuid,uuid,jsonb,jsonb) TO __RP1_APP_USER__;

ALTER FUNCTION integration.assistant_maintenance(boolean) RENAME TO assistant_maintenance_pre_permissions;
CREATE FUNCTION integration.assistant_maintenance(p_recover boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
BEGIN
 -- Waiting is deliberately not terminal and has no open model reservation.
 UPDATE integration.assistant_read_permission SET state='expired' WHERE state IN ('pending','approved') AND expires_at<=now();
 IF p_recover THEN
  -- A resume queued before a crash must not be converted into a new task.
  UPDATE integration.assistant_job SET state='WAITING_PERMISSION',deadline=NULL,active_started_at=NULL
   WHERE state='QUEUED' AND resume_count>0;
 END IF;
 PERFORM integration.assistant_maintenance_pre_permissions(p_recover);
 UPDATE integration.assistant_read_permission p SET state='cancelled'
 FROM integration.assistant_job j WHERE j.id=p.job_id AND p.state IN ('pending','approved') AND
 j.state IN ('CANCELLED','FAILED','TIMED_OUT','INTERRUPTED');
 DELETE FROM integration.assistant_read_permission WHERE created_at<now()-interval '30 days';
 UPDATE integration.assistant_job SET resume_state='{}' WHERE created_at<now()-interval '30 days' AND resume_state<>'{}';
 UPDATE integration.assistant_job SET state='INTERRUPTED',finished_at=now()
  WHERE created_at<now()-interval '30 days' AND state='WAITING_PERMISSION';
END $$;
REVOKE ALL ON FUNCTION integration.assistant_maintenance_pre_permissions(boolean) FROM PUBLIC,__RP1_APP_USER__;
REVOKE ALL ON FUNCTION integration.assistant_maintenance(boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_maintenance(boolean) TO __RP1_APP_USER__;

-- Preserve the original finance/retention behavior while treating paused work as nonterminal.
CREATE OR REPLACE FUNCTION integration.assistant_maintenance_base(p_recover boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('rp1-assistant-finance',0));
 IF p_recover THEN
  UPDATE integration.assistant_job SET state='INTERRUPTED',finished_at=now() WHERE state IN ('QUEUED','RUNNING');
  IF EXISTS(SELECT 1 FROM integration.assistant_model_call WHERE state='RESERVED') THEN
   UPDATE integration.assistant_model_call SET state='UNKNOWN',settled_at=now() WHERE state='RESERVED';
   UPDATE integration.assistant_finance SET blocked=true,reason='restart_with_unsettled_usage' WHERE singleton;
  END IF;
 END IF;
 UPDATE integration.assistant_job SET state='TIMED_OUT',finished_at=now() WHERE state='RUNNING' AND deadline<=clock_timestamp();
 IF EXISTS(SELECT 1 FROM integration.assistant_model_call c JOIN integration.assistant_job j ON j.id=c.job_id WHERE c.state='RESERVED' AND j.state NOT IN ('RUNNING','QUEUED','WAITING_PERMISSION')) THEN
  UPDATE integration.assistant_model_call c SET state='UNKNOWN',settled_at=now() FROM integration.assistant_job j WHERE j.id=c.job_id AND c.state='RESERVED' AND j.state NOT IN ('RUNNING','QUEUED','WAITING_PERMISSION');
  UPDATE integration.assistant_finance SET blocked=true,reason='expired_task_without_usage' WHERE singleton;
 END IF;
 DELETE FROM integration.assistant_message WHERE created_at<now()-interval '30 days';
 UPDATE integration.assistant_job SET context='{}',result=NULL WHERE created_at<now()-interval '30 days' AND (context<>'{}' OR result IS NOT NULL);
END $$;
