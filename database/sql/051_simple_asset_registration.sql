-- Existing samples were created in R&D testing, as confirmed by the owner.
-- No sample-purpose flag; scenario is independent of execution/data approval.
SET LOCAL lock_timeout = '5s';
SELECT set_config('app.change_reason', '简化样品编号建档；已有样品场景归为研发测试', true);
ALTER TABLE test.asset ADD COLUMN creation_scenario text NOT NULL DEFAULT 'RND_TEST'
    CHECK (creation_scenario IN ('PRODUCTION', 'RND_TEST'));
ALTER TABLE test.asset ALTER COLUMN serial_number DROP NOT NULL;
ALTER TABLE test.whole_machine_profile ALTER COLUMN model DROP NOT NULL;
ALTER TABLE test.module_profile ALTER COLUMN module_type DROP NOT NULL;

-- The owner reads all allocated numbers even when another creator's sample is
-- hidden by RLS. Only the next number is exposed, never another sample's data.
-- The advisory lock is held until commit, including manual-number registrations.
CREATE FUNCTION test.allocate_asset_code(p_code text, p_auto boolean) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,test,iam AS $$
DECLARE v_prefix text; v_max integer;
BEGIN
    IF NOT iam.can_write_business() THEN
        RAISE EXCEPTION 'sample creation requires editor permission' USING ERRCODE='42501';
    END IF;
    IF p_code IS NULL OR p_code !~ '^RP[0-9]+\.[0-9]+-(SYS|ARM|SARM|LEG|SLEG|UPPER|LOWER|WAISTHIP|CHEST|HEAD|BAT)-[0-9]{3}$'
       OR p_auto IS NULL THEN
        RAISE EXCEPTION 'invalid sample number or numbering choice' USING ERRCODE='23514';
    END IF;
    v_prefix := regexp_replace(p_code, '-[0-9]{3}$', '');
    PERFORM pg_advisory_xact_lock(hashtextextended('asset-number:' || v_prefix, 0));
    IF NOT p_auto THEN RETURN p_code; END IF;
    SELECT max(right(asset_code, 3)::integer) INTO v_max
    FROM test.asset WHERE left(asset_code, length(asset_code)-4)=v_prefix;
    IF v_max IS NULL THEN RETURN p_code; END IF;
    IF v_max >= 999 THEN RETURN NULL; END IF;
    RETURN v_prefix || '-' || lpad((v_max+1)::text, 3, '0');
END $$;
REVOKE ALL ON FUNCTION test.allocate_asset_code(text,boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION test.allocate_asset_code(text,boolean) TO __RP1_APP_USER__;
