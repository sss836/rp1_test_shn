ALTER TABLE integration.assistant_job
 ADD COLUMN input_message_id bigint,
 ADD COLUMN input_snapshot jsonb,
 ADD COLUMN input_hash text CHECK(input_hash IS NULL OR input_hash ~ '^[0-9a-f]{64}$');

-- Pre-upgrade pending work has no provable input boundary. Never reconstruct
-- it from the current thread or replay a possibly committed business request.
UPDATE integration.assistant_job SET state='INTERRUPTED',finished_at=now()
 WHERE state IN ('QUEUED','RUNNING');

CREATE FUNCTION integration.assistant_protect_input() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,integration AS $$
BEGIN
 IF OLD.input_hash IS NOT NULL AND
    (NEW.input_message_id IS DISTINCT FROM OLD.input_message_id OR
     NEW.input_hash IS DISTINCT FROM OLD.input_hash OR
     NEW.input_snapshot IS DISTINCT FROM OLD.input_snapshot OR
     NEW.context IS DISTINCT FROM OLD.context) THEN
  IF NOT (current_user=pg_get_userbyid((SELECT relowner FROM pg_class WHERE oid='integration.assistant_job'::regclass))
          AND OLD.created_at<now()-interval '30 days'
          AND NEW.input_hash=OLD.input_hash AND NEW.input_message_id=OLD.input_message_id
          AND NEW.input_snapshot IS NULL AND NEW.context='{}'::jsonb) THEN
   RAISE EXCEPTION 'assistant input is immutable' USING ERRCODE='P0001';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER assistant_input_immutable BEFORE UPDATE ON integration.assistant_job
 FOR EACH ROW EXECUTE FUNCTION integration.assistant_protect_input();

ALTER FUNCTION integration.assistant_maintenance(boolean) RENAME TO assistant_maintenance_base;
REVOKE ALL ON FUNCTION integration.assistant_maintenance_base(boolean) FROM __RP1_APP_USER__;
CREATE FUNCTION integration.assistant_maintenance(p_recover boolean DEFAULT false) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
BEGIN
 -- Clear expiring snapshots and context together before the older cleanup.
 UPDATE integration.assistant_job SET input_snapshot=NULL,context='{}'
 WHERE created_at<now()-interval '30 days' AND input_snapshot IS NOT NULL;
 PERFORM integration.assistant_maintenance_base(p_recover);
END $$;
REVOKE ALL ON FUNCTION integration.assistant_maintenance(boolean),integration.assistant_protect_input() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.assistant_maintenance(boolean) TO __RP1_APP_USER__;
