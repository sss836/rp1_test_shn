-- A bad live interval must not block unrelated workstation authentication.
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE integration.hmi_runtime_conflict (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id uuid NOT NULL DEFAULT public.uuid_v7() UNIQUE,
    execution_id bigint NOT NULL REFERENCES test.test_execution(id),
    installation_id uuid,
    proposed_started_at timestamptz NOT NULL,
    proposed_active_seconds numeric(20,3) NOT NULL CHECK (proposed_active_seconds >= 0),
    proposed_ended_at timestamptz NOT NULL,
    requested_status text NOT NULL CHECK (requested_status IN ('COMPLETED','FAILED')),
    conflicting_interval_ids bigint[] NOT NULL,
    evidence jsonb NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (execution_id, proposed_started_at, proposed_active_seconds, requested_status)
);
CREATE INDEX ix_hmi_runtime_conflict_installation ON integration.hmi_runtime_conflict(installation_id, execution_id);
ALTER TABLE integration.hmi_runtime_conflict ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON integration.hmi_runtime_conflict FROM PUBLIC, __RP1_APP_USER__;
GRANT SELECT ON integration.hmi_runtime_conflict TO __RP1_APP_USER__;
CREATE POLICY hmi_runtime_conflict_read ON integration.hmi_runtime_conflict FOR SELECT USING (
    iam.is_system_admin() OR EXISTS (
        SELECT 1 FROM test.test_execution e
        WHERE e.id=execution_id AND e.operator_id=iam.current_user_id()
    )
);
CREATE FUNCTION integration.hmi_runtime_conflict_immutable() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
BEGIN
    RAISE EXCEPTION '上位机时长冲突证据不可覆盖或删除' USING ERRCODE='22023';
END $$;
REVOKE ALL ON FUNCTION integration.hmi_runtime_conflict_immutable() FROM PUBLIC;
CREATE TRIGGER hmi_runtime_conflict_immutable BEFORE UPDATE OR DELETE ON integration.hmi_runtime_conflict
    FOR EACH ROW EXECUTE FUNCTION integration.hmi_runtime_conflict_immutable();
CREATE TRIGGER hmi_runtime_conflict_audit AFTER INSERT ON integration.hmi_runtime_conflict
    FOR EACH ROW EXECUTE FUNCTION audit.capture_row_change();

-- Called only by trusted database functions; the application cannot invoke this helper.
CREATE FUNCTION test.try_close_live_execution(p_id bigint,p_status text,p_active numeric)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,test,integration AS $$
DECLARE
    r test.test_execution%ROWTYPE;
    active numeric(20,3) := p_active;
    finish timestamptz;
    conflicts bigint[];
    constraint_name text;
BEGIN
    IF p_status NOT IN ('COMPLETED','FAILED') OR active IS NULL OR active<0 THEN
        RAISE EXCEPTION 'invalid live closure' USING ERRCODE='22023';
    END IF;
    SELECT * INTO r FROM test.test_execution WHERE id=p_id FOR UPDATE;
    IF NOT FOUND OR r.live_connection_id IS NULL OR r.approved_submission_id IS NOT NULL
       OR r.status NOT IN ('RUNNING','PAUSED') THEN RETURN false; END IF;
    finish:=r.normalized_started_at+make_interval(secs=>active);
    SELECT array_agg(ri.id ORDER BY ri.id) INTO conflicts FROM test.runtime_interval ri
    WHERE r.normalized_started_at IS NOT NULL AND ri.asset_id=r.asset_id AND NOT ri.voided AND ri.source_kind IN ('NATIVE','MANUAL')
      AND ri.execution_id IS DISTINCT FROM r.id
      AND tstzrange(ri.started_at,ri.ended_at,'[)') && tstzrange(r.normalized_started_at,finish,'[)');
    IF conflicts IS NULL THEN
        BEGIN
            UPDATE test.test_execution SET status=p_status,reported_active_seconds=active,
                normalized_ended_at=clock_timestamp() WHERE id=r.id;
            IF r.normalized_started_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM test.runtime_interval WHERE execution_id=r.id AND NOT voided
            ) THEN
                INSERT INTO test.runtime_interval(campaign_id,asset_id,cycle_id,execution_id,source_kind,
                    started_at,ended_at,active_seconds,clock_quality,data_quality,source_method)
                VALUES(r.campaign_id,r.asset_id,r.cycle_id,r.id,'NATIVE',r.normalized_started_at,finish,
                    active,'VALID','PARTIAL','hmi-live-heartbeat');
            END IF;
            RETURN true;
        EXCEPTION WHEN exclusion_violation THEN
            GET STACKED DIAGNOSTICS constraint_name=CONSTRAINT_NAME;
            IF constraint_name<>'runtime_interval_asset_id_tstzrange_excl' THEN RAISE; END IF;
            -- The exception block rolls back this execution's state and interval together.
            SELECT array_agg(ri.id ORDER BY ri.id) INTO conflicts FROM test.runtime_interval ri
            WHERE r.normalized_started_at IS NOT NULL AND ri.asset_id=r.asset_id AND NOT ri.voided AND ri.source_kind IN ('NATIVE','MANUAL')
              AND ri.execution_id IS DISTINCT FROM r.id
              AND tstzrange(ri.started_at,ri.ended_at,'[)') && tstzrange(r.normalized_started_at,finish,'[)');
        END;
    END IF;
    IF EXISTS(SELECT 1 FROM integration.hmi_runtime_conflict c
        WHERE c.execution_id=r.id AND c.proposed_started_at=r.normalized_started_at
          AND c.proposed_active_seconds=active AND c.requested_status=p_status) THEN
        RETURN false;
    END IF;
    INSERT INTO integration.hmi_runtime_conflict(execution_id,installation_id,proposed_started_at,
        proposed_active_seconds,proposed_ended_at,requested_status,conflicting_interval_ids,evidence)
    SELECT r.id,p.installation_id,r.normalized_started_at,active,finish,p_status,coalesce(conflicts,'{}'),
        jsonb_build_object('execution_code',r.execution_code,'reported_active_seconds',r.reported_active_seconds,
            'received_at',r.received_at,'connection_id',r.live_connection_id,'session_key',r.live_session_key,
            'constraint','runtime_interval_asset_id_tstzrange_excl','sqlstate','23P01',
            'conflicting_intervals',(SELECT jsonb_agg(jsonb_build_object('id',ri.public_id,
                'started_at',ri.started_at,'ended_at',ri.ended_at,'active_seconds',ri.active_seconds,
                'execution_id',ri.execution_id,'source_method',ri.source_method) ORDER BY ri.id)
                FROM test.runtime_interval ri WHERE ri.id=ANY(conflicts)))
    FROM integration.hmi_presence p WHERE p.connection_id=r.live_connection_id
    ON CONFLICT(execution_id,proposed_started_at,proposed_active_seconds,requested_status) DO NOTHING;
    RETURN false;
END $$;
REVOKE ALL ON FUNCTION test.try_close_live_execution(bigint,text,numeric) FROM PUBLIC, __RP1_APP_USER__;

CREATE OR REPLACE FUNCTION test.expire_stale_live_executions()
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path=pg_catalog,test,integration AS $$
DECLARE closed integer:=0; r test.test_execution%ROWTYPE;
BEGIN
    FOR r IN SELECT e.* FROM test.test_execution e
        WHERE e.status='RUNNING' AND e.live_connection_id IS NOT NULL
          AND e.approved_submission_id IS NULL AND NOT test.is_live_running(e.id)
        ORDER BY e.id FOR UPDATE SKIP LOCKED
    LOOP
        IF test.try_close_live_execution(r.id,'COMPLETED',r.reported_active_seconds) THEN
            closed:=closed+1;
        END IF;
    END LOOP;
    RETURN closed;
END $$;

CREATE OR REPLACE FUNCTION test.sync_live_runs(p_connection_id uuid)
RETURNS jsonb
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, test, integration, iam, catalog
AS $$
DECLARE
    v_presence integration.hmi_presence%ROWTYPE;
    v_online boolean := false;
    v_run jsonb;
    v_key text;
    v_uuid uuid;
    previous_run test.test_execution%ROWTYPE;
    v_state text;
    v_active numeric(20,3);
    v_campaign bigint;
    v_cycle bigint;
    v_asset bigint;
    v_configuration bigint;
    v_version bigint;
    v_station bigint;
    v_execution test.test_execution%ROWTYPE;
    v_status text;
    v_previous_conflict boolean;
    v_result jsonb := '[]'::jsonb;
BEGIN
    -- Global stale-run cleanup belongs to the worker, never to a login heartbeat.
    SELECT * INTO v_presence FROM integration.hmi_presence WHERE connection_id = p_connection_id;
    IF NOT FOUND THEN
        RETURN v_result;
    END IF;
    v_online := v_presence.disconnected_at IS NULL
        AND v_presence.last_seen_at > clock_timestamp() - interval '45 seconds';
    IF NOT v_online THEN
        RETURN v_result;
    END IF;

    FOR v_run IN SELECT value FROM jsonb_array_elements(v_presence.runs)
    LOOP
        v_key := left(coalesce(v_run->>'session_key', ''), 128);
        v_state := lower(coalesce(v_run->>'state', ''));
        v_uuid := nullif(v_run->>'execution_uuid','')::uuid;
        v_active := least(315360000, greatest(0, coalesce((v_run->>'active_seconds')::numeric, 0)));
        IF v_key = '' THEN
            CONTINUE;
        END IF;
        IF v_uuid IS NOT NULL THEN PERFORM pg_advisory_xact_lock(hashtextextended(v_uuid::text,38)); END IF;
        IF v_uuid IS NOT NULL AND EXISTS(SELECT 1 FROM test.test_execution WHERE acquisition_uuid=v_uuid AND operator_id IS DISTINCT FROM v_presence.user_id) THEN
            RAISE EXCEPTION '采集身份属于其他操作账号' USING ERRCODE='42501';
        END IF;
        IF v_uuid IS NOT NULL AND EXISTS(SELECT 1 FROM integration.execution_archive WHERE execution_uuid=v_uuid AND owner_id IS DISTINCT FROM v_presence.user_id) THEN
            RAISE EXCEPTION '归档身份属于其他操作账号' USING ERRCODE='42501';
        END IF;

        -- A window can start a new acquisition before the next 10-second heartbeat.
        -- Close the previous online projection; it remains unapproved evidence.
        IF v_state='running' AND v_uuid IS NOT NULL THEN
            v_previous_conflict:=false;
            FOR previous_run IN SELECT * FROM test.test_execution
                WHERE live_connection_id=p_connection_id AND live_session_key=v_key
                  AND status IN ('RUNNING','PAUSED') AND acquisition_uuid IS NOT NULL
                  AND operator_id=v_presence.user_id AND approved_submission_id IS NULL
                  AND acquisition_uuid<>v_uuid FOR UPDATE
            LOOP
                IF NOT test.try_close_live_execution(previous_run.id,'COMPLETED',previous_run.reported_active_seconds) THEN
                    v_previous_conflict:=true;
                END IF;
            END LOOP;
            -- Preserve the new local acquisition; don't assign it the old execution's identity.
            IF v_previous_conflict THEN CONTINUE; END IF;
        END IF;
        SELECT * INTO v_execution
        FROM test.test_execution
        WHERE (v_uuid IS NOT NULL AND acquisition_uuid=v_uuid AND operator_id=v_presence.user_id)
           OR (live_connection_id=p_connection_id AND live_session_key=v_key AND (v_uuid IS NULL OR acquisition_uuid IS NULL))
        ORDER BY id DESC LIMIT 1 FOR UPDATE;
        IF v_execution.approved_submission_id IS NOT NULL AND v_execution.acquisition_uuid=v_uuid THEN
            v_result:=v_result||jsonb_build_array(jsonb_build_object('session_key',v_key,'execution_code',v_execution.execution_code,'execution_id',v_execution.public_id,'status',v_execution.status));
            v_execution:=NULL;
            CONTINUE;
        END IF;

        v_campaign := NULL;
        IF v_state = 'running'
           AND coalesce(v_run->>'campaign_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'cycle_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'asset_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'configuration_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'test_case_version_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
           AND coalesce(v_run->>'station_id', '') ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN
            SELECT c.id, cy.id, a.id, cfg.id, ver.id, s.id
            INTO v_campaign, v_cycle, v_asset, v_configuration, v_version, v_station
            FROM test.test_campaign c
            JOIN test.test_cycle cy ON cy.public_id = (v_run->>'cycle_id')::uuid AND cy.campaign_id = c.id
            JOIN test.asset a ON a.public_id = (v_run->>'asset_id')::uuid
            JOIN test.configuration_snapshot cfg ON cfg.public_id = (v_run->>'configuration_id')::uuid AND cfg.asset_id = a.id
            JOIN catalog.test_case_version ver ON ver.public_id = (v_run->>'test_case_version_id')::uuid
            JOIN test.station s ON s.public_id = (v_run->>'station_id')::uuid
            WHERE c.public_id = (v_run->>'campaign_id')::uuid;
        END IF;

        IF v_state = 'running' AND v_campaign IS NOT NULL THEN
            IF v_execution.acquisition_uuid=v_uuid AND ROW(v_execution.campaign_id,v_execution.cycle_id,v_execution.asset_id,v_execution.configuration_snapshot_id,v_execution.test_case_version_id)
                IS DISTINCT FROM ROW(v_campaign,v_cycle,v_asset,v_configuration,v_version) THEN
                RAISE EXCEPTION '采集身份与执行上下文不一致' USING ERRCODE='22023';
            END IF;
            IF v_execution.id IS NOT NULL AND (v_execution.status IN ('RUNNING', 'PAUSED') OR v_execution.acquisition_uuid=v_uuid) THEN
                IF v_execution.status NOT IN ('RUNNING','PAUSED') THEN
                    UPDATE test.runtime_interval SET voided=true
                    WHERE execution_id=v_execution.id AND source_method='hmi-live-heartbeat' AND NOT voided;
                END IF;
                UPDATE test.test_execution
                SET status = 'RUNNING',
                    normalized_ended_at=NULL,
                    acquisition_uuid=coalesce(acquisition_uuid,v_uuid),
                    live_connection_id=p_connection_id,live_session_key=v_key,
                    reported_active_seconds = v_active,
                    received_at = clock_timestamp(),
                    operator_id = coalesce(operator_id, v_presence.user_id)
                WHERE id = v_execution.id
                RETURNING * INTO v_execution;
            ELSE
                INSERT INTO test.test_execution(
                    execution_code, campaign_id, cycle_id, asset_id, configuration_snapshot_id,
                    test_case_version_id, station_id, operator_id, status,
                    normalized_started_at, code_timestamp, source_time, received_at,
                    clock_quality, data_quality, live_connection_id, live_session_key,
                    reported_active_seconds, acquisition_uuid
                ) VALUES (
                    'GENERATED', v_campaign, v_cycle, v_asset, v_configuration,
                    v_version, v_station, v_presence.user_id, 'RUNNING',
                    clock_timestamp(), clock_timestamp(), clock_timestamp(), clock_timestamp(),
                    'VALID', 'PARTIAL', p_connection_id, v_key, v_active, v_uuid
                )
                RETURNING * INTO v_execution;
            END IF;
        ELSIF v_execution.id IS NOT NULL AND v_execution.status = 'RUNNING' THEN
            v_status := CASE v_state
                WHEN 'paused' THEN 'PAUSED'
                WHEN 'fault' THEN 'FAILED'
                ELSE 'COMPLETED'
            END;
            IF v_status='PAUSED' THEN
                UPDATE test.test_execution SET status='PAUSED',reported_active_seconds=v_active,
                    received_at=clock_timestamp(),normalized_ended_at=NULL WHERE id=v_execution.id;
            ELSE
                -- Preserve the newest telemetry even when interval materialization needs review.
                UPDATE test.test_execution SET reported_active_seconds=v_active,received_at=clock_timestamp()
                    WHERE id=v_execution.id;
                PERFORM test.try_close_live_execution(v_execution.id,v_status,v_active);
            END IF;
            SELECT * INTO v_execution FROM test.test_execution WHERE id=v_execution.id;
        END IF;

        IF v_execution.id IS NOT NULL AND v_execution.live_session_key = v_key THEN
            v_result := v_result || jsonb_build_array(jsonb_build_object(
                'session_key', v_key,
                'execution_code', v_execution.execution_code,
                'execution_id', v_execution.public_id,
                'status', v_execution.status
            ));
        END IF;
        v_execution := NULL;
        v_campaign := NULL;
    END LOOP;
    RETURN v_result;
END
$$;
