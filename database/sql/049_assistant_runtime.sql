CREATE TABLE integration.assistant_thread(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),user_id bigint NOT NULL REFERENCES iam.app_user(id),
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE integration.assistant_job(
 id uuid PRIMARY KEY,thread_id uuid NOT NULL REFERENCES integration.assistant_thread(id),
 user_id bigint NOT NULL REFERENCES iam.app_user(id),request_hash text NOT NULL,
 context jsonb NOT NULL DEFAULT '{}',result jsonb,
 state text NOT NULL DEFAULT 'QUEUED' CHECK(state IN ('QUEUED','RUNNING','COMPLETED','FAILED','TIMED_OUT','INTERRUPTED','CANCELLED')),
 created_at timestamptz NOT NULL DEFAULT now(),started_at timestamptz,deadline timestamptz,finished_at timestamptz,
 cancel_requested boolean NOT NULL DEFAULT false,rounds int NOT NULL DEFAULT 0 CHECK(rounds BETWEEN 0 AND 6)
);
CREATE TABLE integration.assistant_message(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 thread_id uuid NOT NULL REFERENCES integration.assistant_thread(id),user_id bigint NOT NULL REFERENCES iam.app_user(id),
 job_id uuid REFERENCES integration.assistant_job(id),role text NOT NULL CHECK(role IN ('user','assistant')),
 body text NOT NULL,context jsonb NOT NULL DEFAULT '{}',cards jsonb NOT NULL DEFAULT '[]',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX assistant_message_history ON integration.assistant_message(user_id,thread_id,created_at,id);
CREATE TABLE integration.assistant_budget_day(
 day date PRIMARY KEY,spent numeric(18,8) NOT NULL DEFAULT 0,reserved numeric(18,8) NOT NULL DEFAULT 0,
 CHECK(spent>=0 AND reserved>=0 AND spent+reserved<=100)
);
CREATE TABLE integration.assistant_finance(
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),blocked boolean NOT NULL DEFAULT false,reason text
);
INSERT INTO integration.assistant_finance(singleton) VALUES(true);
CREATE TABLE integration.assistant_model_call(
 job_id uuid NOT NULL REFERENCES integration.assistant_job(id),sequence int NOT NULL CHECK(sequence BETWEEN 1 AND 6),
 user_id bigint NOT NULL REFERENCES iam.app_user(id),day date NOT NULL REFERENCES integration.assistant_budget_day(day),
 state text NOT NULL CHECK(state IN ('RESERVED','SETTLED','UNKNOWN')),
 reserved numeric(18,8) NOT NULL CHECK(reserved>0),charged numeric(18,8),rates jsonb NOT NULL,usage jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),settled_at timestamptz,PRIMARY KEY(job_id,sequence)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['assistant_thread','assistant_job','assistant_message','assistant_model_call'] LOOP
  EXECUTE format('ALTER TABLE integration.%I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY own_assistant ON integration.%I USING(user_id=iam.current_user_id()) WITH CHECK(user_id=iam.current_user_id() AND iam.can_write_business())',t);
 END LOOP;
END $$;
GRANT SELECT,INSERT,UPDATE ON integration.assistant_thread,integration.assistant_job,integration.assistant_message TO __RP1_APP_USER__;
GRANT USAGE ON SEQUENCE integration.assistant_message_id_seq TO __RP1_APP_USER__;
GRANT SELECT ON integration.assistant_model_call,integration.assistant_budget_day,integration.assistant_finance TO __RP1_APP_USER__;

-- All model calls, including parallel jobs, serialize only the short accounting
-- transition. No model/network wait occurs while this lock is held.
CREATE FUNCTION integration.assistant_reserve(p_job uuid,p_sequence int,p_amount numeric,p_rates jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE j integration.assistant_job; b integration.assistant_budget_day; d date;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('rp1-assistant-finance',0));
 SELECT * INTO STRICT j FROM integration.assistant_job WHERE id=p_job AND user_id=iam.current_user_id() FOR UPDATE;
 IF j.state<>'RUNNING' OR j.cancel_requested OR j.deadline<=clock_timestamp() OR p_sequence<>j.rounds+1 OR p_sequence>6 THEN
  RAISE EXCEPTION 'task deadline or round limit' USING ERRCODE='P0001';
 END IF;
 IF (SELECT blocked FROM integration.assistant_finance WHERE singleton) OR EXISTS(
 SELECT 1 FROM integration.assistant_model_call WHERE job_id=p_job AND state<>'SETTLED') THEN
  RAISE EXCEPTION 'accounting requires reconciliation' USING ERRCODE='P0001';
 END IF;
 d:=(clock_timestamp() AT TIME ZONE 'Asia/Shanghai')::date;
 INSERT INTO integration.assistant_budget_day(day) VALUES(d) ON CONFLICT DO NOTHING;
 SELECT * INTO b FROM integration.assistant_budget_day WHERE day=d FOR UPDATE;
 IF p_amount<=0 OR b.spent+b.reserved+p_amount>100 THEN RAISE EXCEPTION 'daily budget insufficient' USING ERRCODE='P0001'; END IF;
 UPDATE integration.assistant_budget_day SET reserved=reserved+p_amount WHERE day=d;
 INSERT INTO integration.assistant_model_call(job_id,sequence,user_id,day,state,reserved,rates)
 VALUES(p_job,p_sequence,j.user_id,d,'RESERVED',p_amount,p_rates);
 UPDATE integration.assistant_job SET rounds=p_sequence WHERE id=p_job;
END $$;

CREATE FUNCTION integration.assistant_settle(p_job uuid,p_sequence int,p_charge numeric,p_usage jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE c integration.assistant_model_call;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('rp1-assistant-finance',0));
 SELECT * INTO STRICT c FROM integration.assistant_model_call WHERE job_id=p_job AND sequence=p_sequence AND user_id=iam.current_user_id() FOR UPDATE;
 IF c.state='SETTLED' THEN RETURN; END IF;
 -- A missing/invalid receipt keeps the full reservation and blocks the whole
 -- site across midnight and restarts. Only explicit operator reconciliation can
 -- clear this; the app never guesses zero cost or silently releases it.
 IF c.state='UNKNOWN' OR p_charge IS NULL OR p_charge<0 OR p_charge>c.reserved THEN
  UPDATE integration.assistant_model_call SET state='UNKNOWN',settled_at=now() WHERE job_id=p_job AND sequence=p_sequence;
  UPDATE integration.assistant_finance SET blocked=true,reason='provider_usage_unknown' WHERE singleton;
  RETURN;
 END IF;
 UPDATE integration.assistant_budget_day SET reserved=reserved-c.reserved,spent=spent+p_charge WHERE day=c.day;
 UPDATE integration.assistant_model_call SET state='SETTLED',charged=p_charge,usage=p_usage,settled_at=now() WHERE job_id=p_job AND sequence=p_sequence;
END $$;

CREATE FUNCTION integration.assistant_maintenance(p_recover boolean DEFAULT false) RETURNS void
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
 IF EXISTS(SELECT 1 FROM integration.assistant_model_call c JOIN integration.assistant_job j ON j.id=c.job_id WHERE c.state='RESERVED' AND j.state NOT IN ('RUNNING','QUEUED')) THEN
  UPDATE integration.assistant_model_call c SET state='UNKNOWN',settled_at=now() FROM integration.assistant_job j WHERE j.id=c.job_id AND c.state='RESERVED' AND j.state NOT IN ('RUNNING','QUEUED');
  UPDATE integration.assistant_finance SET blocked=true,reason='expired_task_without_usage' WHERE singleton;
 END IF;
 DELETE FROM integration.assistant_message WHERE created_at<now()-interval '30 days';
 UPDATE integration.assistant_job SET context='{}',result=NULL WHERE created_at<now()-interval '30 days' AND (context<>'{}' OR result IS NOT NULL);
END $$;
REVOKE ALL ON FUNCTION integration.assistant_reserve(uuid,int,numeric,jsonb),integration.assistant_settle(uuid,int,numeric,jsonb),integration.assistant_maintenance(boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_reserve(uuid,int,numeric,jsonb),integration.assistant_settle(uuid,int,numeric,jsonb),integration.assistant_maintenance(boolean) TO __RP1_APP_USER__;
-- Financial audit contains only usage/rates/task identifiers, never chat bodies.
CREATE TRIGGER assistant_call_audit AFTER INSERT OR UPDATE ON integration.assistant_model_call FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

CREATE FUNCTION integration.assistant_abandon(p_job uuid) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('rp1-assistant-finance',0));
 IF EXISTS(SELECT 1 FROM integration.assistant_model_call WHERE job_id=p_job AND state='RESERVED') THEN
  UPDATE integration.assistant_model_call SET state='UNKNOWN',settled_at=now() WHERE job_id=p_job AND state='RESERVED';
  UPDATE integration.assistant_finance SET blocked=true,reason='task_ended_without_usage' WHERE singleton;
 END IF;
END $$;
REVOKE ALL ON FUNCTION integration.assistant_abandon(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_abandon(uuid) TO __RP1_APP_USER__;

-- Finishing task metadata must still work after its browser account has been
-- disabled. This function cannot mutate any business object or choose a new user.
CREATE FUNCTION integration.assistant_finish(p_job uuid,p_user uuid,p_state text,p_result jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE j integration.assistant_job; actual jsonb; v_result jsonb:=p_result;
BEGIN
 IF p_state NOT IN ('COMPLETED','FAILED','TIMED_OUT','INTERRUPTED','CANCELLED') THEN RAISE EXCEPTION 'invalid terminal state'; END IF;
 SELECT job.* INTO STRICT j FROM integration.assistant_job job JOIN iam.app_user u ON u.id=job.user_id WHERE job.id=p_job AND u.public_id=p_user FOR UPDATE OF job;
 SELECT r.result||jsonb_build_object('object_types',(SELECT jsonb_object_agg(op->>'alias',op->>'kind') FROM jsonb_array_elements(r.request_payload->'operations') op))
 INTO actual FROM integration.setup_receipt r WHERE r.user_id=j.user_id AND r.request_key=j.id;
 IF actual IS NOT NULL THEN
  v_result:=v_result||jsonb_build_object('setup',jsonb_build_object('status','created','question','已核对实际保存回执。','receipt',actual));
 END IF;
 IF j.cancel_requested AND p_state='COMPLETED' THEN p_state:='CANCELLED'; END IF;
 IF j.deadline<=clock_timestamp() AND p_state='COMPLETED' THEN p_state:='TIMED_OUT'; END IF;
 UPDATE integration.assistant_job SET state=p_state,result=v_result,finished_at=now() WHERE id=p_job;
 INSERT INTO integration.assistant_message(thread_id,user_id,job_id,role,body,cards,context)
 VALUES(j.thread_id,j.user_id,j.id,'assistant',coalesce(v_result->>'reply','任务已结束，请核对实际回执。'),coalesce(v_result->'cards','[]'::jsonb),j.context);
 UPDATE integration.assistant_thread SET updated_at=now() WHERE id=j.thread_id;
END $$;
REVOKE ALL ON FUNCTION integration.assistant_finish(uuid,uuid,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_finish(uuid,uuid,text,jsonb) TO __RP1_APP_USER__;
