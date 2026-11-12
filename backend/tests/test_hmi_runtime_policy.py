"""New HMI policy changes acceptance, not measurement facts or old catalog rules."""
from copy import deepcopy

from app.services.curve_query import CurveProcessor, Statistic, catalog_item


def test_archived_lro_iq_current_retains_its_joint_and_metric():
    key = "left_elbow_joint_iq_current_a"
    assert catalog_item(key) == {
        "key": key, "subject": "left_elbow_joint", "metric": "iq_current_a", "unit": "A",
    }


def inputs():
    facts = dict(active_seconds=1.,completed_cycles=1,exception_count=0,
        machine_status='completed',automatic_verdict='passed')
    quality = dict(sample_completeness=1.,max_gap_seconds=.05,invalid_time_rows=0,non_monotonic=0)
    stats = {}
    for key, values in {'a_temp_c':[20.,99.], 'a_tracking_error_rad':[2.,2.],
                        'a_torque_nm':[999.,999.], 'a_error_id':[0.,0.]}.items():
        stats[key] = Statistic()
        for value in values:
            stats[key].add(value, is_fault=key.endswith('_error_id'))
    rules = dict(criteria_version='hmi-runtime-v2', required_active_seconds=0,
        required_cycles=0,min_sample_completeness=.95,max_gap_seconds=.2)
    return facts,quality,stats,rules


def test_new_policy_excludes_three_rules_and_retains_metrics():
    facts,quality,stats,rules = inputs()
    original=deepcopy(facts)
    report=CurveProcessor.analyze(facts,rules,quality,stats)
    assert report['verdict']=='passed'
    assert not any(r['name'].endswith(('.rise','.rms','.peak_abs')) for r in report['rules'])
    assert {m['key'] for m in report['metrics']}==set(stats)
    assert facts==original


def test_old_upload_and_published_case_semantics_remain():
    facts,quality,stats,rules = inputs()
    for version in ['generic-v1','CATALOG:11111111-1111-4111-8111-111111111111']:
        old=CurveProcessor.analyze(facts,{**rules,'criteria_version':version,
            'max_temperature_rise_c':30.,'max_tracking_rms_rad':.5,'max_torque_abs_nm':1.},quality,stats)
        assert old['verdict']=='failed'
        assert {r['name'] for r in old['rules'] if r['result']=='failed'} >= {
            'a_temp_c.rise','a_tracking_error_rad.rms','a_torque_nm.peak_abs'}


def test_new_policy_keeps_fault_duration_quality_and_approval_independent():
    facts,quality,stats,rules=inputs()
    stats['a_error_id'].faults=1
    assert CurveProcessor.analyze(facts,rules,quality,stats)['verdict']=='failed'
    stats['a_error_id'].faults=0
    assert CurveProcessor.analyze({**facts,'machine_status':'interrupted'},rules,quality,stats)['verdict']=='blocked'
    assert CurveProcessor.analyze(facts,{**rules,'required_active_seconds':2},quality,stats)['verdict']=='blocked'
    assert CurveProcessor.analyze(facts,rules,{**quality,'sample_completeness':.9},stats)['verdict']=='blocked'
    assert 'submission_status' not in CurveProcessor.analyze(facts,rules,quality,stats)
