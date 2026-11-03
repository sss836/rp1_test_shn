-- Structured feedback and published knowledge outlive personal chat retention.
-- Personal questions/answers are deliberately not copied into this table.
CREATE TABLE integration.assistant_feedback (
 id uuid PRIMARY KEY,user_id bigint NOT NULL REFERENCES iam.app_user(id),
 job_id uuid NOT NULL REFERENCES integration.assistant_job(id),request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{64}$'),
 rating text NOT NULL CHECK(rating IN ('helpful','issue')),note text NOT NULL CHECK(length(note)<=4000),
 share_for_faq boolean NOT NULL DEFAULT false,evidence jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE integration.assistant_faq_version (
 id uuid NOT NULL,revision integer NOT NULL CHECK(revision>0),request_key uuid NOT NULL UNIQUE,
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{64}$'),
 source_feedback_id uuid NOT NULL REFERENCES integration.assistant_feedback(id),
 published_by bigint NOT NULL REFERENCES iam.app_user(id),
 question text NOT NULL CHECK(length(btrim(question)) BETWEEN 1 AND 2000),
 answer text NOT NULL CHECK(length(btrim(answer)) BETWEEN 1 AND 12000),
 published_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(id,revision)
);
CREATE TABLE integration.assistant_repair (
 id uuid PRIMARY KEY,user_id bigint NOT NULL REFERENCES iam.app_user(id),
 job_id uuid NOT NULL REFERENCES integration.assistant_job(id),origin_session_id uuid NOT NULL,
 role text NOT NULL CHECK(role IN ('TEST_EXECUTOR','SYSTEM_ADMIN')),
 installation_id uuid NOT NULL,window_id text NOT NULL CHECK(length(window_id) BETWEEN 1 AND 128),
 kind text NOT NULL CHECK(kind IN ('config','code')),
 path text NOT NULL CHECK(length(path) BETWEEN 1 AND 4096),
 expected_sha256 text NOT NULL CHECK(expected_sha256 ~ '^[a-f0-9]{64}$'),
 original text CHECK(octet_length(original)<=65536),replacement text CHECK(octet_length(replacement)<=65536),
 replacement_sha256 text NOT NULL CHECK(replacement_sha256 ~ '^[a-f0-9]{64}$'),
 reason text NOT NULL CHECK(length(reason) BETWEEN 1 AND 2000),diff text,
 proposal_sha256 text NOT NULL CHECK(proposal_sha256 ~ '^[a-f0-9]{64}$'),
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{64}$'),
 state text NOT NULL DEFAULT 'proposed' CHECK(state IN ('proposed','queued','applied','conflict','failed','rejected')),
 receipt jsonb,receipt_sha256 text CHECK(receipt_sha256 IS NULL OR receipt_sha256 ~ '^[a-f0-9]{64}$'),
 created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL DEFAULT(now()+interval '1 day'),
 confirmed_at timestamptz,finished_at timestamptz,
 CHECK(kind<>'code' OR role='SYSTEM_ADMIN'),
 CHECK((receipt IS NULL)=(receipt_sha256 IS NULL)),
 CHECK(state NOT IN ('applied','conflict','failed') OR receipt IS NOT NULL),
 CHECK(state NOT IN ('queued','applied','conflict','failed') OR confirmed_at IS NOT NULL)
);
CREATE INDEX assistant_repair_queue ON integration.assistant_repair(user_id,origin_session_id,installation_id,window_id,state);

ALTER TABLE integration.assistant_feedback ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.assistant_faq_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE integration.assistant_repair ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.assistant_feedback,integration.assistant_faq_version,integration.assistant_repair FROM PUBLIC,__RP1_APP_USER__;
GRANT SELECT,INSERT ON integration.assistant_feedback,integration.assistant_faq_version TO __RP1_APP_USER__;
GRANT SELECT,INSERT ON integration.assistant_repair TO __RP1_APP_USER__;
GRANT UPDATE(state,confirmed_at,receipt,receipt_sha256,finished_at) ON integration.assistant_repair TO __RP1_APP_USER__;
CREATE POLICY feedback_read ON integration.assistant_feedback FOR SELECT USING(
 user_id=iam.current_user_id() OR (share_for_faq AND iam.is_system_admin()));
CREATE POLICY feedback_insert ON integration.assistant_feedback FOR INSERT WITH CHECK(
 user_id=iam.current_user_id() AND iam.can_write_business() AND EXISTS(
 SELECT 1 FROM integration.assistant_job j JOIN integration.assistant_thread t ON t.id=j.thread_id
 WHERE j.id=job_id AND j.user_id=iam.current_user_id() AND t.deleted_at IS NULL));
CREATE POLICY faq_read ON integration.assistant_faq_version FOR SELECT USING(iam.can_write_business());
CREATE POLICY faq_insert ON integration.assistant_faq_version FOR INSERT WITH CHECK(
 iam.is_system_admin() AND published_by=iam.current_user_id() AND EXISTS(
 SELECT 1 FROM integration.assistant_feedback f WHERE f.id=source_feedback_id
 AND (f.user_id=iam.current_user_id() OR f.share_for_faq)));
CREATE POLICY repair_read ON integration.assistant_repair FOR SELECT USING(user_id=iam.current_user_id() AND iam.can_write_business());
CREATE POLICY repair_insert ON integration.assistant_repair FOR INSERT WITH CHECK(
 user_id=iam.current_user_id() AND role=iam.current_user_role() AND iam.can_write_business()
 AND origin_session_id=nullif(current_setting('app.session_public_id',true),'')::uuid
 AND EXISTS(SELECT 1 FROM integration.assistant_job j JOIN integration.assistant_thread t ON t.id=j.thread_id
 WHERE j.id=job_id AND j.user_id=iam.current_user_id() AND t.deleted_at IS NULL
 AND j.origin_session_id=assistant_repair.origin_session_id
 AND j.context#>>'{hmi,installation_id}'=assistant_repair.installation_id::text
 AND j.context#>>'{hmi,window_id}'=assistant_repair.window_id));
CREATE POLICY repair_update ON integration.assistant_repair FOR UPDATE USING(
 user_id=iam.current_user_id() AND role=iam.current_user_role() AND iam.can_write_business()
 AND origin_session_id=nullif(current_setting('app.session_public_id',true),'')::uuid)
 WITH CHECK(user_id=iam.current_user_id() AND role=iam.current_user_role()
 AND origin_session_id=nullif(current_setting('app.session_public_id',true),'')::uuid);

CREATE TRIGGER feedback_immutable BEFORE UPDATE OR DELETE ON integration.assistant_feedback
 FOR EACH ROW EXECUTE FUNCTION integration.archive_immutable();
CREATE TRIGGER faq_immutable BEFORE UPDATE OR DELETE ON integration.assistant_faq_version
 FOR EACH ROW EXECUTE FUNCTION integration.archive_immutable();

CREATE FUNCTION integration.assistant_repair_transition() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,integration,iam AS $$
BEGIN
 IF OLD.state IN ('applied','conflict','failed','rejected') AND
    (NEW.state IS DISTINCT FROM OLD.state OR NEW.receipt IS DISTINCT FROM OLD.receipt
     OR NEW.receipt_sha256 IS DISTINCT FROM OLD.receipt_sha256) THEN
  -- Expiry cannot discard an already-performed operation's delayed receipt.
  -- Only the narrow owner-executed settlement function can record it; this
  -- exception never requeues execution or changes a recorded receipt.
  IF NOT (OLD.state='rejected' AND OLD.receipt_sha256 IS NULL AND OLD.confirmed_at IS NOT NULL
          AND OLD.expires_at<=now() AND NEW.receipt_sha256 IS NOT NULL
          AND NEW.state IN ('applied','conflict','failed','rejected')
          AND current_user=pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='integration.assistant_repair'::regclass))) THEN
   RAISE EXCEPTION 'repair receipt is immutable' USING ERRCODE='23514';
  END IF;
 END IF;
 IF OLD.state='proposed' AND NEW.state NOT IN ('proposed','queued','rejected') THEN
  RAISE EXCEPTION 'repair requires explicit queue confirmation' USING ERRCODE='23514';
 END IF;
 IF OLD.state='queued' AND NEW.state='proposed' THEN
  RAISE EXCEPTION 'queued repair cannot be rewound' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER repair_transition BEFORE UPDATE ON integration.assistant_repair
 FOR EACH ROW EXECUTE FUNCTION integration.assistant_repair_transition();

CREATE FUNCTION integration.assistant_repair_settle(p_id uuid,p_proposal text,p_receipt jsonb,p_hash text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam AS $$
DECLARE r integration.assistant_repair; sid uuid;
BEGIN
 SELECT * INTO r FROM integration.assistant_repair WHERE id=p_id AND user_id=iam.current_user_id() FOR UPDATE;
 IF NOT FOUND OR NOT iam.can_write_business() OR r.role<>iam.current_user_role()
    OR r.proposal_sha256 IS DISTINCT FROM p_proposal THEN RAISE EXCEPTION 'repair settlement forbidden' USING ERRCODE='42501'; END IF;
 sid:=nullif(current_setting('app.session_public_id',true),'')::uuid;
 IF sid IS NULL OR (sid<>r.origin_session_id AND
    current_setting('app.repair_settlement_verified',true) IS DISTINCT FROM(r.id::text||':'||r.proposal_sha256)) THEN
  RAISE EXCEPTION 'repair original session or verified settlement required' USING ERRCODE='42501';
 END IF;
 IF p_hash IS NULL OR p_hash !~ '^[a-f0-9]{64}$' OR jsonb_typeof(p_receipt)<>'object' OR octet_length(p_receipt::text)>65536
    OR coalesce(p_receipt->>'status','') NOT IN ('applied','conflict','failed','rejected') THEN
  RAISE EXCEPTION 'invalid repair receipt' USING ERRCODE='22023';
 END IF;
 IF r.receipt_sha256 IS NOT NULL THEN
  IF r.receipt_sha256<>p_hash THEN RAISE EXCEPTION 'repair receipt immutable' USING ERRCODE='23514'; END IF;
  RETURN jsonb_build_object('accepted',true,'replayed',true,'state',r.state);
 END IF;
 IF r.confirmed_at IS NULL OR r.state NOT IN ('queued','rejected') OR
    (r.state='rejected' AND r.expires_at>now()) THEN
  RAISE EXCEPTION 'repair was not confirmed or already ended' USING ERRCODE='23514';
 END IF;
 IF p_receipt->>'status'='applied' AND (
    p_receipt->>'before_sha256' IS DISTINCT FROM r.expected_sha256
    OR p_receipt->>'after_sha256' IS DISTINCT FROM r.replacement_sha256
    OR nullif(p_receipt->>'backup_path','') IS NULL) THEN
  RAISE EXCEPTION 'repair receipt hashes or backup missing' USING ERRCODE='22023';
 END IF;
 UPDATE integration.assistant_repair SET state=p_receipt->>'status',receipt=p_receipt,
    receipt_sha256=p_hash,finished_at=now() WHERE id=r.id;
 RETURN jsonb_build_object('accepted',true,'replayed',false,'state',p_receipt->>'status');
END $$;
REVOKE ALL ON FUNCTION integration.assistant_repair_settle(uuid,text,jsonb,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_repair_settle(uuid,text,jsonb,text) TO __RP1_APP_USER__;

CREATE FUNCTION integration.assistant_support_audit() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,audit AS $$
BEGIN
 IF TG_OP='INSERT' OR NEW.state IS DISTINCT FROM OLD.state OR NEW.receipt_sha256 IS DISTINCT FROM OLD.receipt_sha256 THEN
  INSERT INTO audit.security_event(event_type,actor_user_id,username,outcome,metadata)
  VALUES('ASSISTANT_REPAIR_RECORD',NEW.user_id,'','SUCCESS',jsonb_build_object(
    'id',NEW.id,'job_id',NEW.job_id,'kind',NEW.kind,'state',NEW.state,
    'proposal_sha256',NEW.proposal_sha256,'receipt_sha256',NEW.receipt_sha256,
    'expected_sha256',NEW.expected_sha256,'installation_id',NEW.installation_id));
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER repair_audit AFTER INSERT OR UPDATE ON integration.assistant_repair
 FOR EACH ROW EXECUTE FUNCTION integration.assistant_support_audit();

CREATE FUNCTION integration.assistant_faq_sequence() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,integration AS $$
DECLARE previous integer;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('assistant-faq-version:'||NEW.id::text,0));
 SELECT coalesce(max(revision),0) INTO previous FROM integration.assistant_faq_version WHERE id=NEW.id;
 IF NEW.revision<>previous+1 THEN RAISE EXCEPTION 'FAQ revision must append next version' USING ERRCODE='23514'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER faq_sequence BEFORE INSERT ON integration.assistant_faq_version
 FOR EACH ROW EXECUTE FUNCTION integration.assistant_faq_sequence();

-- Keep hashes and real receipts; expire unconfirmed execution ability after one
-- day and erase source text at the same 30-day boundary as personal chat.
ALTER FUNCTION integration.assistant_maintenance(boolean) RENAME TO assistant_maintenance_pre_support;
CREATE FUNCTION integration.assistant_maintenance(p_recover boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
BEGIN
 PERFORM integration.assistant_maintenance_pre_support(p_recover);
 UPDATE integration.assistant_repair SET state='rejected',finished_at=now()
 WHERE state IN ('proposed','queued') AND expires_at<=now();
 UPDATE integration.assistant_repair SET original=NULL,replacement=NULL,diff=NULL
 WHERE created_at<now()-interval '30 days' AND original IS NOT NULL;
END $$;
REVOKE ALL ON FUNCTION integration.assistant_maintenance_pre_support(boolean) FROM PUBLIC,__RP1_APP_USER__;
REVOKE ALL ON FUNCTION integration.assistant_maintenance(boolean),integration.assistant_repair_transition(),integration.assistant_support_audit(),integration.assistant_faq_sequence() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_maintenance(boolean) TO __RP1_APP_USER__;
