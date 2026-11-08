-- Read-only, permission-scoped usage check for completing a planned identity.
-- Ordinary editors may not see a different campaign's references under RLS.
-- The API holds the asset FOR UPDATE before calling this function and writing.
CREATE FUNCTION test.asset_identity_blocked_reason(p_asset_id bigint) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    sample test.asset%ROWTYPE;
    reference record;
    in_use boolean;
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business()
       OR NOT iam.can_access_asset(p_asset_id,true) THEN
        RAISE EXCEPTION '需要此样品的编辑权限' USING ERRCODE='42501';
    END IF;
    SELECT * INTO sample FROM test.asset WHERE id=p_asset_id AND NOT voided;
    IF NOT FOUND OR sample.lifecycle_status<>'CANDIDATE' THEN
        RETURN '仅尚未使用的计划样品可以补全实机身份';
    END IF;
    IF (sample.asset_kind='WHOLE_MACHINE' AND NOT EXISTS(
          SELECT 1 FROM test.whole_machine_profile WHERE asset_id=p_asset_id))
       OR (sample.asset_kind='MODULE' AND NOT EXISTS(
          SELECT 1 FROM test.module_profile WHERE asset_id=p_asset_id)) THEN
        RETURN '样品档案不完整，请联系管理员核对';
    END IF;
    IF NOT EXISTS(SELECT 1 FROM test.configuration_snapshot WHERE asset_id=p_asset_id)
       OR EXISTS(SELECT 1 FROM test.configuration_snapshot
           WHERE asset_id=p_asset_id AND NOT (
             fingerprint='registration:'||sample.public_id::text
             AND reliability_impact='UNKNOWN'
             AND captured_from='sample registration; device configuration not captured'
             AND hardware_manifest='{}'::jsonb AND software_manifest='{}'::jsonb
             AND parameter_manifest='{}'::jsonb)) THEN
        RETURN '样品已有实际配置快照，实机身份已锁定';
    END IF;
    -- Inspect all current FK references with the definer's complete view.
    -- Profiles and the validated UNKNOWN registration snapshot are the only
    -- exceptions. No hidden campaign identity or evidence is returned.
    FOR reference IN
        SELECT DISTINCT c.conrelid, source.attname
        FROM pg_catalog.pg_constraint c
        CROSS JOIN LATERAL unnest(c.conkey,c.confkey) AS keys(source_number,target_number)
        JOIN pg_catalog.pg_attribute source ON source.attrelid=c.conrelid
             AND source.attnum=keys.source_number
        JOIN pg_catalog.pg_attribute target ON target.attrelid=c.confrelid
             AND target.attnum=keys.target_number
        WHERE c.contype='f' AND c.confrelid='test.asset'::regclass AND target.attname='id'
          AND c.conrelid NOT IN ('test.whole_machine_profile'::regclass,
             'test.module_profile'::regclass,'test.configuration_snapshot'::regclass)
        ORDER BY c.conrelid,source.attname
    LOOP
        EXECUTE format('SELECT EXISTS(SELECT 1 FROM %s WHERE %I=$1)',
            reference.conrelid::regclass,reference.attname) INTO in_use USING p_asset_id;
        IF in_use THEN
            RETURN '样品已关联计划、执行或证据，实机身份已锁定';
        END IF;
    END LOOP;
    RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION test.asset_identity_blocked_reason(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION test.asset_identity_blocked_reason(bigint) TO __RP1_APP_USER__;
