-- Only sample codes already visible in the asset catalog are exposed by the API.
-- Cross-account telemetry is reduced to a running-code set, without operator details.
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='30s';
CREATE FUNCTION integration.hmi_running_sample_codes()
RETURNS SETOF text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,integration,iam AS $$
    WITH latest AS (
        SELECT DISTINCT ON (installation_id) * FROM integration.hmi_presence
        ORDER BY installation_id,connected_at DESC,connection_id DESC
    )
    SELECT DISTINCT upper(btrim(run->>'sample_id'))
    FROM latest p
    JOIN iam.auth_session s ON s.id=p.auth_session_id
    JOIN iam.app_user u ON u.id=p.user_id
    CROSS JOIN LATERAL jsonb_array_elements(p.runs) run
    WHERE iam.current_principal_kind()='HUMAN'
      AND p.disconnected_at IS NULL
      AND p.last_seen_at>statement_timestamp()-interval '45 seconds'
      AND p.snapshot_at>statement_timestamp()-interval '20 seconds'
      AND s.revoked_at IS NULL AND s.expires_at>statement_timestamp()
      AND u.enabled AND NOT u.must_change_password
      AND run->>'state' IN ('running','stopping')
      AND coalesce(run->>'sample_id','')<>''
$$;
REVOKE ALL ON FUNCTION integration.hmi_running_sample_codes() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.hmi_running_sample_codes() TO __RP1_APP_USER__;

-- A registered candidate without a campaign must still be selectable on the HMI.
-- Limit this cross-campaign read to identifiers, part and the busy flag.
CREATE FUNCTION integration.hmi_sample_options()
RETURNS TABLE(id uuid,code text,asset_kind text,target_part_code text,running boolean)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,integration,test,iam AS $$
    SELECT a.public_id,a.asset_code,a.asset_kind,
        CASE WHEN a.asset_kind='WHOLE_MACHINE' THEN 'SYS' ELSE m.target_part_code END,
        EXISTS (SELECT 1 FROM integration.hmi_running_sample_codes() busy(code)
                WHERE busy.code=upper(a.asset_code))
    FROM test.asset a LEFT JOIN test.module_profile m ON m.asset_id=a.id
    WHERE NOT a.voided AND iam.current_principal_kind()='HUMAN'
      AND iam.current_user_role() IN ('SYSTEM_ADMIN','TEST_EXECUTOR')
    ORDER BY a.asset_code
$$;
REVOKE ALL ON FUNCTION integration.hmi_sample_options() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.hmi_sample_options() TO __RP1_APP_USER__;
