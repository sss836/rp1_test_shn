-- WP1: archive visibility and approval remain distinct from formal qualification.
-- Add gates without rewriting acquisition facts, old decisions or summaries.
CREATE FUNCTION integration.archive_has_formal_context(p_id bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,integration,catalog AS $$
    SELECT EXISTS(
        SELECT 1 FROM integration.execution_archive a
        JOIN integration.archive_classification c ON c.archive_id=a.id AND c.revision=a.classification_revision
        JOIN catalog.test_case_version v ON v.public_id=(c.payload->>'test_case_version_id')::uuid AND v.status='PUBLISHED'
        JOIN catalog.test_case tc ON tc.id=v.test_case_id AND upper(btrim(tc.case_code))<>'OTHER'
        WHERE a.id=p_id AND c.payload ?& ARRAY['campaign_id','cycle_id','segment_id','asset_id','configuration_id','test_case_version_id','station_id']
          AND integration.can_read_archive(a.id)
    )
$$;
REVOKE ALL ON FUNCTION integration.archive_has_formal_context(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_has_formal_context(bigint) TO __RP1_APP_USER__;

ALTER FUNCTION integration.factory_summary(bigint) RENAME TO factory_summary_pre_wp1;
CREATE FUNCTION integration.factory_summary(p_id bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
DECLARE a integration.execution_archive%ROWTYPE; c jsonb; summary jsonb;
BEGIN
    SELECT * INTO STRICT a FROM integration.execution_archive WHERE id=p_id FOR UPDATE;
    IF NOT integration.archive_has_formal_context(a.id) THEN
        RAISE EXCEPTION '正式工厂统计必须补齐已发布正式用例和执行上下文；OTHER可先归档查看曲线' USING ERRCODE='22023';
    END IF;
    SELECT payload INTO STRICT c FROM integration.archive_classification WHERE archive_id=a.id AND revision=a.classification_revision;
    PERFORM 1 FROM integration.archive_context(c,a.id);
    IF NOT FOUND THEN RAISE EXCEPTION '正式执行上下文无效或已停用' USING ERRCODE='22023'; END IF;
    summary:=integration.factory_summary_pre_wp1(p_id);
    IF summary->>'station_id' IS DISTINCT FROM c->>'station_id' THEN
        RAISE EXCEPTION '核实工位与正式执行上下文不一致' USING ERRCODE='22023';
    END IF;
    RETURN summary || jsonb_build_object('formal_context',c,'classification_revision',a.classification_revision);
END $$;
REVOKE ALL ON FUNCTION integration.factory_summary_pre_wp1(bigint),integration.factory_summary(bigint) FROM PUBLIC,__RP1_APP_USER__;

ALTER FUNCTION integration.archive_summary(bigint) RENAME TO archive_summary_pre_wp1;
CREATE FUNCTION integration.archive_summary(p_archive_id bigint) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration AS $$
BEGIN
    IF NOT integration.archive_has_formal_context(p_archive_id) THEN
        RAISE EXCEPTION '正式统计必须补齐已发布正式用例和执行上下文；OTHER可先归档查看曲线' USING ERRCODE='22023';
    END IF;
    RETURN integration.archive_summary_pre_wp1(p_archive_id);
END $$;
REVOKE ALL ON FUNCTION integration.archive_summary_pre_wp1(bigint) FROM PUBLIC,__RP1_APP_USER__;
REVOKE ALL ON FUNCTION integration.archive_summary(bigint) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_summary(bigint) TO __RP1_APP_USER__;
