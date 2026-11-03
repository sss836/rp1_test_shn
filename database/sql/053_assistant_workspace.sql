-- Conversation UX and append-only progress. No measurement/approval mutations.
ALTER TABLE integration.assistant_thread
 ADD COLUMN title text NOT NULL DEFAULT '' CHECK(length(title)<=120),
 ADD COLUMN title_updated_at timestamptz NOT NULL DEFAULT now(),
 ADD COLUMN deleted_at timestamptz;
ALTER TABLE integration.assistant_job
 ADD COLUMN origin_session_id uuid,
 ADD COLUMN after_job_id uuid REFERENCES integration.assistant_job(id),
 ADD COLUMN steering_closed boolean NOT NULL DEFAULT false,
 ADD COLUMN execution_sources jsonb NOT NULL DEFAULT '{}' CHECK(octet_length(execution_sources::text)<=20000),
 ADD CONSTRAINT assistant_job_dependency_not_self CHECK(after_job_id IS DISTINCT FROM id);

CREATE TABLE integration.assistant_event(
 seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 job_id uuid NOT NULL REFERENCES integration.assistant_job(id),
 user_id bigint NOT NULL REFERENCES iam.app_user(id),
 kind text NOT NULL CHECK(kind IN ('text','tool','state','input')),
 data jsonb NOT NULL CHECK(octet_length(data::text)<=20000),
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX assistant_event_job ON integration.assistant_event(job_id,seq);
CREATE TABLE integration.assistant_input(
 request_key uuid PRIMARY KEY,job_id uuid NOT NULL REFERENCES integration.assistant_job(id),
 user_id bigint NOT NULL REFERENCES iam.app_user(id),session_id uuid NOT NULL,
 message text NOT NULL CHECK(length(message) BETWEEN 1 AND 8000),
 delivered_at timestamptz,consumed_at timestamptz,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX assistant_input_job ON integration.assistant_input(job_id,created_at,request_key);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['assistant_event','assistant_input'] LOOP
  EXECUTE format('ALTER TABLE integration.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY assistant_workspace_owner ON integration.%I USING(user_id=iam.current_user_id()) WITH CHECK(user_id=iam.current_user_id() AND iam.can_write_business() AND EXISTS(SELECT 1 FROM integration.assistant_job j WHERE j.id=job_id AND j.user_id=iam.current_user_id()))',t);
 END LOOP;
END $$;
GRANT SELECT,INSERT ON integration.assistant_event TO __RP1_APP_USER__;
GRANT SELECT,INSERT,UPDATE ON integration.assistant_input TO __RP1_APP_USER__;
GRANT USAGE ON SEQUENCE integration.assistant_event_seq_seq TO __RP1_APP_USER__;

-- Keep an accepted but unconsumed supplement visible even on timeout, abort or
-- the last model round. Final metadata survives original-login revocation;
-- this wrapper cannot choose a new owner or mutate business objects.
ALTER FUNCTION integration.assistant_finish(uuid,uuid,text,jsonb) RENAME TO assistant_finish_workspace_base;
REVOKE ALL ON FUNCTION integration.assistant_finish_workspace_base(uuid,uuid,text,jsonb) FROM __RP1_APP_USER__;
CREATE FUNCTION integration.assistant_finish(p_job uuid,p_user uuid,p_state text,p_result jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE pending jsonb; final_result jsonb:=p_result;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('assistant-input:'||p_job::text,0));
 IF NOT EXISTS(SELECT 1 FROM integration.assistant_job j JOIN iam.app_user u ON u.id=j.user_id WHERE j.id=p_job AND u.public_id=p_user) THEN
  RAISE EXCEPTION 'assistant task owner mismatch';
 END IF;
 SELECT jsonb_agg(jsonb_build_object('request_key',request_key,'message',message) ORDER BY created_at,request_key)
 INTO pending FROM integration.assistant_input WHERE job_id=p_job AND consumed_at IS NULL;
 IF pending IS NOT NULL THEN
  final_result:=final_result||jsonb_build_object('cards',coalesce(final_result->'cards','[]'::jsonb)||jsonb_build_array(jsonb_build_object(
   'type','unprocessed_input','title','补充未用于本次诊断','source','task_supplement_receipt','updated_at',now(),
   'data',jsonb_build_object('items',pending,'summary','以下补充未用于本次诊断，可恢复到草稿后作为下一条问题发送。'))));
 END IF;
 UPDATE integration.assistant_job SET steering_closed=true WHERE id=p_job;
 PERFORM integration.assistant_finish_workspace_base(p_job,p_user,p_state,final_result);
END $$;
REVOKE ALL ON FUNCTION integration.assistant_finish(uuid,uuid,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_finish(uuid,uuid,text,jsonb) TO __RP1_APP_USER__;

CREATE FUNCTION integration.assistant_protect_dependency() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,integration AS $$
BEGIN
 IF OLD.input_hash IS NOT NULL AND
    (NEW.after_job_id IS DISTINCT FROM OLD.after_job_id OR NEW.origin_session_id IS DISTINCT FROM OLD.origin_session_id) THEN
  RAISE EXCEPTION 'assistant execution identity is immutable';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER assistant_execution_identity_immutable BEFORE UPDATE ON integration.assistant_job
 FOR EACH ROW EXECUTE FUNCTION integration.assistant_protect_dependency();
REVOKE ALL ON FUNCTION integration.assistant_protect_dependency() FROM PUBLIC;

ALTER FUNCTION integration.assistant_maintenance(boolean) RENAME TO assistant_maintenance_inputs;
REVOKE ALL ON FUNCTION integration.assistant_maintenance_inputs(boolean) FROM __RP1_APP_USER__;
CREATE FUNCTION integration.assistant_maintenance(p_recover boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
DECLARE interrupted record;
BEGIN
 IF p_recover THEN
  FOR interrupted IN SELECT j.id,u.public_id FROM integration.assistant_job j JOIN iam.app_user u ON u.id=j.user_id
    WHERE j.state IN ('QUEUED','RUNNING') AND j.created_at>=now()-interval '30 days' ORDER BY j.id LOOP
   PERFORM integration.assistant_finish(interrupted.id,interrupted.public_id,'INTERRUPTED',jsonb_build_object(
    'reply','服务重启中断了本次任务；未自动续发操作，请核对实际回执及尚未用于诊断的补充。',
    'cards','[]'::jsonb,'completion','partial','termination_reason','service_restart'));
  END LOOP;
 END IF;
 DELETE FROM integration.assistant_event WHERE created_at<now()-interval '30 days';
 DELETE FROM integration.assistant_input WHERE created_at<now()-interval '30 days';
 UPDATE integration.assistant_thread SET title='' WHERE title_updated_at<now()-interval '30 days' AND title<>'';
 PERFORM integration.assistant_maintenance_inputs(p_recover);
END $$;
REVOKE ALL ON FUNCTION integration.assistant_maintenance(boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_maintenance(boolean) TO __RP1_APP_USER__;
