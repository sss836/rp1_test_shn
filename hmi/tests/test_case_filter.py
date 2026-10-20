import importlib.util
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "case_filter",
    Path(__file__).resolve().parents[1] / "factory_hmi/desktop/case_filter.py",
)
assert _spec is not None and _spec.loader is not None
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
profile_matches_limb = _module.profile_matches_limb
start_decision = _module.start_decision
profile_at_bench = _module.profile_at_bench
OTHER_CASE = _module.OTHER_CASE


def profile(case: str, sample: str) -> dict[str, str]:
    return {"case_code": case, "sample_id": sample}


class CaseFilterTests(unittest.TestCase):
    def test_waist_hip_keeps_new_identity_and_legacy_lower_without_cross_pairing(self) -> None:
        dedicated = profile("REL-WAISTHIP-001", "RP1.3-WAISTHIP-001")
        legacy = profile("REL-LOWER-001", "RP1.3-LOWER-001")
        self.assertTrue(profile_matches_limb(dedicated, "waist_hip"))
        self.assertTrue(profile_matches_limb(legacy, "waist_hip"))
        self.assertFalse(profile_matches_limb(dedicated, "biped_waist"))
        self.assertFalse(profile_matches_limb(dedicated, "lower_body"))
        self.assertFalse(profile_matches_limb(profile("REL-WAISTHIP-001", "RP1.3-LOWER-001"), "waist_hip"))
        self.assertFalse(profile_matches_limb(profile("REL-LOWER-001", "RP1.3-WAISTHIP-001"), "waist_hip"))

    def test_arm_hides_other_parts_and_short_arm_samples(self) -> None:
        rows = [
            profile("REL-ARM-002", "RP1.3-ARM-001"),
            profile("REL-ARM-002", "RP1.3-SARM-004"),
            profile("REL-LEG-001", "RP1.3-LEG-001"),
            profile("REL-UPPER-001", "RP1.3-UPPER-001"),
        ]
        visible = [row for row in rows if profile_matches_limb(row, "left_arm")]
        self.assertEqual(visible, [rows[0]])

    def test_short_arm_keeps_arm_cases_and_only_reduced_samples(self) -> None:
        rows = [
            profile("REL-ARM-002", "RP1.3-ARM-001"),
            profile("REL-ARM-002", "RP1.3-SARM-004"),
            profile("REL-SARM-002", "RP1.3-SARM-004"),
        ]
        visible = [row for row in rows if profile_matches_limb(row, "left_short_arm")]
        self.assertEqual(visible, rows[1:])

    def test_unknown_limb_keeps_every_case(self) -> None:
        row = profile("REL-CHEST-001", "RP1.3-CHEST-001")
        self.assertTrue(profile_matches_limb(row, "custom_module"))

    def test_start_requires_a_case_sample_context_and_actual_bench(self) -> None:
        self.assertEqual(start_decision(saved_bench="", case_code=None, has_context=False), "need_case")
        self.assertEqual(start_decision(saved_bench="a10", case_code=OTHER_CASE, has_context=False), "local_other")
        self.assertEqual(start_decision(saved_bench="a10", case_code="REL-ARM-002", has_context=False), "need_sample")
        self.assertEqual(start_decision(saved_bench="", case_code="REL-ARM-002", has_context=True), "need_bench")
        self.assertEqual(start_decision(saved_bench="a10", case_code="REL-ARM-002", has_context=True), "ready")

    def test_same_case_can_use_different_valid_stations_without_changing_profile(self) -> None:
        planned = {"case_code": "REL-ARM-002", "bench_id": "RD-01", "station_id": "planned", "ready": True}
        stations = [{"code": "A10", "id": "actual-a10"}, {"code": "MP-02", "id": "actual-mp02"}]
        for code, station in (("a10", "actual-a10"), ("mp-02", "actual-mp02")):
            with self.subTest(code=code):
                actual = profile_at_bench(planned, code, stations)
                self.assertEqual((actual["bench_id"], actual["station_code"], actual["station_id"]), (code.upper(), code.upper(), station))
                self.assertEqual(actual["case_code"], planned["case_code"])
                self.assertTrue(actual["ready"])
        self.assertEqual(planned["station_id"], "planned")
        self.assertEqual(planned["bench_id"], "RD-01")

    def test_actual_station_only_repairs_missing_station_context(self) -> None:
        stations = [{"code": "A10", "id": "actual-a10"}]
        planned = {"ready": False, "missing": ["station_id", "station_code", "campaign_id"]}
        actual = profile_at_bench(planned, "A10", stations)
        self.assertEqual(actual["missing"], ["campaign_id"])
        self.assertFalse(actual["ready"])
        actual = profile_at_bench({"ready": False, "missing": ["station_id", "bench_id"]}, "A10", stations)
        self.assertTrue(actual["ready"])

    def test_unknown_disabled_and_ambiguous_stations_are_rejected(self) -> None:
        for stations in ([], [{"code": "A10", "id": "actual", "active": False}],
                [{"code": "A10", "id": "actual", "enabled": False}],
                [{"code": "A10", "id": "one"}, {"code": "a10", "id": "two"}]):
            with self.subTest(stations=stations), self.assertRaisesRegex(ValueError, "有效工位"):
                profile_at_bench({"ready": True}, "A10", stations)


if __name__ == "__main__":
    unittest.main()
