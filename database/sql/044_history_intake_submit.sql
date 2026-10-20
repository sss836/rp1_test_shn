-- One user confirmation, one transaction, the existing archive/approval gates.
CREATE FUNCTION integration.archive_intake_submit(p_id uuid,p jsonb,p_key text) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,integration,iam,public AS $$
DECLARE a integration.execution_archive%ROWTYPE; receipt integration.archive_request%ROWTYPE;
    v jsonb; c jsonb; canonical jsonb; checked uuid; submitted uuid; stage_key text; request_digest text;
    reason text:='历史CSV补录送审'; differs boolean;
BEGIN
    IF iam.current_principal_kind()<>'HUMAN' OR NOT iam.can_write_business() THEN
        RAISE EXCEPTION '需要执行员或管理员' USING ERRCODE='42501'; END IF;
    IF p_id IS NULL OR p IS NULL OR jsonb_typeof(p)<>'object' OR octet_length(p::text)>24576
        OR p_key IS NULL OR length(p_key) NOT BETWEEN 8 AND 200
        OR (SELECT count(*) FROM jsonb_object_keys(p))<>3
        OR NOT (p ?& ARRAY['verification','context','expected_submission_revision'])
        OR jsonb_typeof(p->'verification') IS DISTINCT FROM 'object'
        OR jsonb_typeof(p->'context') IS DISTINCT FROM 'object'
        OR jsonb_typeof(p->'expected_submission_revision') IS DISTINCT FROM 'number'
        OR (p->>'expected_submission_revision')::numeric NOT BETWEEN 0 AND 2147483647
        OR trunc((p->>'expected_submission_revision')::numeric)<>(p->>'expected_submission_revision')::numeric THEN
        RAISE EXCEPTION '补录送审参数无效' USING ERRCODE='22023'; END IF;
    request_digest:=encode(public.digest(convert_to(jsonb_build_object('id',p_id,'payload',p)::text,'UTF8'),'sha256'),'hex');
    PERFORM pg_advisory_xact_lock(hashtextextended(iam.current_user_id()::text||':'||p_key,44));
    SELECT * INTO receipt FROM integration.archive_request WHERE actor_id=iam.current_user_id() AND idempotency_key=p_key;
    IF FOUND THEN
        IF receipt.operation<>'intake-submit' OR receipt.request_hash<>request_digest THEN
            RAISE EXCEPTION '送审幂等键内容冲突' USING ERRCODE='P0001'; END IF;
        IF NOT integration.can_read_archive(receipt.archive_id) THEN
            RAISE EXCEPTION '归档不可见' USING ERRCODE='42501'; END IF;
        RETURN (SELECT public_id FROM integration.execution_archive WHERE id=receipt.archive_id);
    END IF;
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id;
    IF NOT FOUND THEN RAISE EXCEPTION '归档不存在' USING ERRCODE='P0002'; END IF;
    IF a.owner_id<>iam.current_user_id() THEN RAISE EXCEPTION '请由原提交人送审' USING ERRCODE='42501'; END IF;
    IF a.origin<>'HISTORY' THEN RAISE EXCEPTION '此入口仅用于历史CSV补录' USING ERRCODE='22023'; END IF;
    -- Match the shared lock order used by summaries and reviews.
    PERFORM pg_advisory_xact_lock(hashtextextended(a.execution_uuid::text,38));
    SELECT * INTO a FROM integration.execution_archive WHERE public_id=p_id FOR UPDATE;
    v:=p->'verification'; c:=p->'context';
    differs:=v->>'verdict' IS DISTINCT FROM a.manifest#>>'{analysis,verdict}';
    IF (differs OR c->>'station_id' IS DISTINCT FROM v->>'station_id')
        AND length(btrim(coalesce(v->>'notes','')))=0 THEN
        RAISE EXCEPTION '结论或工位与原信息不同，请填写一条备注说明' USING ERRCODE='22023'; END IF;
    v:=v||jsonb_build_object('notes',coalesce(nullif(btrim(v->>'notes'),''),'无'),
        'discrepancy_reason',CASE WHEN differs THEN v->>'notes' ELSE '' END,'reason',reason);
    stage_key:='intake:'||encode(public.digest(convert_to(p_key,'UTF8'),'sha256'),'hex');
    checked:=integration.archive_edit(p_id,'verification',v,stage_key||':verify');
    checked:=integration.archive_edit(p_id,'classification',jsonb_build_object(
        'expected_revision',(v->>'expected_revision')::integer+1,
        'context',c||jsonb_build_object('station_id',v->'station_id'),'reason',reason),stage_key||':classify');
    canonical:=integration.archive_summary(a.id);
    submitted:=integration.submit_test_data(canonical,(p->>'expected_submission_revision')::integer,stage_key||':submit',reason);
    INSERT INTO integration.archive_request(actor_id,idempotency_key,request_hash,archive_id,operation)
        VALUES(iam.current_user_id(),p_key,request_digest,a.id,'intake-submit');
    RETURN p_id;
END $$;
REVOKE ALL ON FUNCTION integration.archive_intake_submit(uuid,jsonb,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION integration.archive_intake_submit(uuid,jsonb,text) TO __RP1_APP_USER__;
