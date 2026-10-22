-- Additive ownership. Do not infer historical creators or alter published facts.
ALTER TABLE test.asset ADD COLUMN created_by bigint REFERENCES iam.app_user(id) DEFAULT iam.current_user_id();
ALTER TABLE test.test_campaign ADD COLUMN created_by bigint REFERENCES iam.app_user(id) DEFAULT iam.current_user_id();
ALTER TABLE test.station ADD COLUMN created_by bigint REFERENCES iam.app_user(id) DEFAULT iam.current_user_id();
ALTER TABLE test.site ADD COLUMN created_by bigint REFERENCES iam.app_user(id) DEFAULT iam.current_user_id();
ALTER TABLE test.lab ADD COLUMN created_by bigint REFERENCES iam.app_user(id) DEFAULT iam.current_user_id();
ALTER TABLE catalog.test_case ADD COLUMN created_by bigint REFERENCES iam.app_user(id) DEFAULT iam.current_user_id();
ALTER TABLE catalog.test_case_version ADD COLUMN definition_schema smallint NOT NULL DEFAULT 0;

CREATE FUNCTION iam.owns_setup(p_kind text, p_id bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,iam,test,catalog AS $$
 SELECT iam.is_system_admin() OR (iam.can_write_business() AND CASE p_kind
   WHEN 'asset' THEN EXISTS(SELECT 1 FROM test.asset WHERE id=p_id AND created_by=iam.current_user_id())
   WHEN 'program' THEN EXISTS(SELECT 1 FROM test.test_program WHERE id=p_id AND owner_id=iam.current_user_id())
   WHEN 'campaign' THEN EXISTS(SELECT 1 FROM test.test_campaign WHERE id=p_id AND created_by=iam.current_user_id())
   WHEN 'station' THEN EXISTS(SELECT 1 FROM test.station WHERE id=p_id AND created_by=iam.current_user_id())
   WHEN 'site' THEN EXISTS(SELECT 1 FROM test.site WHERE id=p_id AND created_by=iam.current_user_id())
   WHEN 'lab' THEN EXISTS(SELECT 1 FROM test.lab WHERE id=p_id AND created_by=iam.current_user_id())
   WHEN 'case' THEN EXISTS(SELECT 1 FROM catalog.test_case WHERE id=p_id AND created_by=iam.current_user_id())
   ELSE false END)
$$;
REVOKE ALL ON FUNCTION iam.owns_setup(text,bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION iam.owns_setup(text,bigint) TO __RP1_APP_USER__;

CREATE OR REPLACE FUNCTION iam.can_access_asset(p_asset_id bigint,p_require_edit boolean DEFAULT false)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,iam,test AS $$
 SELECT iam.owns_setup('asset',p_asset_id) OR EXISTS(
   SELECT 1 FROM test.campaign_asset ca WHERE ca.asset_id=p_asset_id
   AND iam.can_access_campaign(ca.campaign_id,p_require_edit))
$$;

-- Preserve existing globally readable campaigns; add ownership only to EDIT.
CREATE OR REPLACE FUNCTION iam.can_access_campaign(p_campaign_id bigint,p_require_edit boolean DEFAULT false)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,iam,test AS $$
 SELECT CASE WHEN p_campaign_id IS NULL THEN false
 WHEN iam.is_internal_service() OR iam.is_system_admin() THEN true
 WHEN NOT p_require_edit AND iam.is_authenticated() AND iam.current_user_role() IN ('VIEWER','TEST_EXECUTOR','SYSTEM_ADMIN') THEN true
 WHEN iam.current_user_role()<>'TEST_EXECUTOR' THEN false
 ELSE iam.owns_setup('campaign',p_campaign_id) OR EXISTS(
   SELECT 1 FROM iam.user_campaign_access WHERE user_id=iam.current_user_id()
   AND campaign_id=p_campaign_id AND access_level='EDIT') END
$$;

-- Direct columns are essential for INSERT ... RETURNING: a helper querying the
-- same table cannot yet see the newly inserted tuple in that command snapshot.
CREATE POLICY asset_creator_read ON test.asset FOR SELECT USING (created_by=iam.current_user_id() AND iam.is_authenticated());
CREATE POLICY program_creator_read ON test.test_program FOR SELECT USING (owner_id=iam.current_user_id() AND iam.is_authenticated());
CREATE POLICY program_existing_executor_read ON test.test_program FOR SELECT USING(iam.can_write_business());
DROP POLICY program_write ON test.test_program;
CREATE POLICY program_insert ON test.test_program FOR INSERT WITH CHECK(iam.can_write_business() AND owner_id=iam.current_user_id());
CREATE POLICY program_update ON test.test_program FOR UPDATE USING(iam.owns_setup('program',id)) WITH CHECK(iam.owns_setup('program',id));
DROP POLICY campaign_insert ON test.test_campaign;
CREATE POLICY campaign_insert ON test.test_campaign FOR INSERT WITH CHECK(
 iam.can_write_business() AND created_by=iam.current_user_id() AND iam.owns_setup('program',program_id));

DO $$ DECLARE t text; k text; BEGIN
 FOR t,k IN SELECT * FROM (VALUES ('test.site','site'),('test.lab','lab'),('test.station','station'),('catalog.test_case','case')) v(t,k) LOOP
   EXECUTE format('DROP POLICY executor_write ON %s',t);
   EXECUTE format('CREATE POLICY setup_insert ON %s FOR INSERT WITH CHECK(iam.can_write_business() AND created_by=iam.current_user_id())',t);
   EXECUTE format('CREATE POLICY setup_update ON %s FOR UPDATE USING(iam.owns_setup(%L,id)) WITH CHECK(iam.owns_setup(%L,id))',t,k,k);
 END LOOP;
END $$;
DROP POLICY executor_write ON catalog.test_case_version;
CREATE POLICY case_version_insert ON catalog.test_case_version FOR INSERT WITH CHECK(iam.owns_setup('case',test_case_id));
CREATE POLICY case_version_update ON catalog.test_case_version FOR UPDATE USING(iam.owns_setup('case',test_case_id)) WITH CHECK(iam.owns_setup('case',test_case_id));

CREATE FUNCTION test.freeze_setup_owner() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_TABLE_NAME='test_program' THEN
   IF NEW.owner_id IS DISTINCT FROM OLD.owner_id THEN RAISE EXCEPTION 'creator is immutable' USING ERRCODE='23514'; END IF;
 ELSE
   IF NEW.created_by IS DISTINCT FROM OLD.created_by THEN RAISE EXCEPTION 'creator is immutable' USING ERRCODE='23514'; END IF;
 END IF;
 RETURN NEW;
END $$;
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['test.asset','test.test_program','test.test_campaign','test.station','test.site','test.lab','catalog.test_case'] LOOP
   EXECUTE format('CREATE TRIGGER setup_owner_immutable BEFORE UPDATE ON %s FOR EACH ROW EXECUTE FUNCTION test.freeze_setup_owner()',t);
 END LOOP;
END $$;

CREATE FUNCTION test.grant_campaign_creator() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,iam,test AS $$
BEGIN
 IF NEW.created_by IS NOT NULL THEN
 INSERT INTO iam.user_campaign_access(user_id,campaign_id,access_level,granted_by)
 VALUES(NEW.created_by,NEW.id,'EDIT',NEW.created_by) ON CONFLICT DO NOTHING;
 END IF;
 RETURN NEW;
END $$;
REVOKE ALL ON FUNCTION test.grant_campaign_creator() FROM PUBLIC;
CREATE TRIGGER setup_campaign_creator AFTER INSERT ON test.test_campaign FOR EACH ROW EXECUTE FUNCTION test.grant_campaign_creator();

CREATE FUNCTION catalog.protect_authored_case_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' OR (TG_OP='UPDATE' AND OLD.status IN ('PUBLISHED','RETIRED')) THEN
   RAISE EXCEPTION 'published versions are immutable; append a revision' USING ERRCODE='23514';
 END IF;
 IF NEW.definition_schema=1 AND NEW.status='PUBLISHED' THEN
   IF (NEW.procedure_spec='{}'::jsonb AND NEW.stage_definitions='[]'::jsonb)
      OR NEW.equipment_requirements='{}'::jsonb OR NEW.sampling_requirements='{}'::jsonb
      OR NEW.termination_rules='{}'::jsonb OR NEW.decision_rules->>'mode' IS NULL
      OR NEW.decision_rules->>'mode' NOT IN ('TEXT_ONLY','RECORD_ONLY','AUTOMATIC')
      OR (NEW.decision_rules->>'mode'='TEXT_ONLY' AND length(btrim(coalesce(NEW.decision_rules->>'text','')))=0)
      OR (NEW.decision_rules->>'mode' IN ('TEXT_ONLY','RECORD_ONLY') AND NEW.decision_rules ? 'archive_acceptance')
      OR (NEW.decision_rules->>'mode'='AUTOMATIC' AND jsonb_typeof(NEW.decision_rules->'archive_acceptance') IS DISTINCT FROM 'object')
      OR NEW.published_by IS DISTINCT FROM iam.current_user_id() OR NEW.published_at IS NULL THEN
     RAISE EXCEPTION 'incomplete published case definition' USING ERRCODE='23514';
   END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER authored_case_version_guard BEFORE INSERT OR UPDATE OR DELETE ON catalog.test_case_version
FOR EACH ROW EXECUTE FUNCTION catalog.protect_authored_case_version();

CREATE TABLE integration.setup_receipt(
 user_id bigint NOT NULL REFERENCES iam.app_user(id), request_key uuid NOT NULL,
 payload_hash text NOT NULL, request_payload jsonb NOT NULL, authorization_evidence jsonb NOT NULL,
 result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(user_id,request_key)
);
ALTER TABLE integration.setup_receipt ENABLE ROW LEVEL SECURITY;
CREATE POLICY setup_receipt_read ON integration.setup_receipt FOR SELECT USING(user_id=iam.current_user_id() OR iam.is_system_admin());
CREATE POLICY setup_receipt_insert ON integration.setup_receipt FOR INSERT WITH CHECK(user_id=iam.current_user_id() AND iam.can_write_business());
GRANT SELECT,INSERT ON integration.setup_receipt TO __RP1_APP_USER__;
CREATE TRIGGER setup_receipt_audit AFTER INSERT ON integration.setup_receipt FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();
