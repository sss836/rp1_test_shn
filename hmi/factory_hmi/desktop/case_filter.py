"""Keep the platform case list aligned with the selected limb."""
from __future__ import annotations

from typing import Mapping

# Case codes use the full-degree part (ARM, LEG). Sample codes keep S for the
# reduced-degree hardware. A limb may use the full-degree cases with only its
# own sample segment.
LIMB_PARTS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "left_arm": (frozenset({"ARM"}), frozenset({"ARM"})),
    "right_arm": (frozenset({"ARM"}), frozenset({"ARM"})),
    "left_short_arm": (frozenset({"ARM", "SARM"}), frozenset({"SARM"})),
    "right_short_arm": (frozenset({"ARM", "SARM"}), frozenset({"SARM"})),
    "left_leg": (frozenset({"LEG"}), frozenset({"LEG"})),
    "right_leg": (frozenset({"LEG"}), frozenset({"LEG"})),
    "left_short_leg": (frozenset({"LEG", "SLEG"}), frozenset({"SLEG"})),
    "right_short_leg": (frozenset({"LEG", "SLEG"}), frozenset({"SLEG"})),
    "upper_body": (frozenset({"UPPER"}), frozenset({"UPPER"})),
    "biped_waist": (frozenset({"LOWER"}), frozenset({"LOWER"})),
    "waist_hip": (frozenset({"WAISTHIP", "LOWER"}), frozenset({"WAISTHIP", "LOWER"})),
    "lower_body": (frozenset({"LOWER"}), frozenset({"LOWER"})),
}


def case_part(code: str) -> str:
    text = str(code or "").strip().upper()
    if text.startswith("SYS-"):
        return "SYS"
    pieces = text.split("-")
    return pieces[1] if len(pieces) >= 2 else ""


def sample_part(code: str) -> str:
    pieces = str(code or "").strip().upper().split("-")
    return pieces[1] if len(pieces) >= 3 and pieces[0].startswith("RP") else ""


LIMB_LABELS: tuple[tuple[str, str], ...] = (
    ("left_arm", "左臂（7自由度）"),
    ("right_arm", "右臂（7自由度）"),
    ("left_short_arm", "左臂（5自由度）"),
    ("right_short_arm", "右臂（5自由度）"),
    ("left_leg", "左腿（6自由度）"),
    ("right_leg", "右腿（6自由度）"),
    ("left_short_leg", "左腿（5自由度）"),
    ("right_short_leg", "右腿（5自由度）"),
    ("waist_hip", "腰髋"),
    ("biped_waist", "双足腰"),
    ("upper_body", "上肢"),
    ("lower_body", "下肢"),
)


def limb_label(key: str) -> str:
    for code, label in LIMB_LABELS:
        if code == key:
            return label
    return key or "未配置"


OTHER_CASE = "OTHER"


def start_decision(
    *,
    saved_bench: str,
    case_code: str | None,
    has_context: bool,
) -> str:
    """Require a case, sample context and an independently chosen actual bench."""
    code = str(case_code or "").strip()
    if not code:
        return "need_case"
    if code == OTHER_CASE:
        if not saved_bench.strip():
            return "need_bench"
        return "local_other"
    if not has_context:
        return "need_sample"
    if not saved_bench.strip():
        return "need_bench"
    return "ready"


def profile_at_bench(
    profile: Mapping[str, object], bench: str, stations: list[dict[str, object]],
) -> dict[str, object]:
    """Copy the selected context with the actual station's code and UUID together."""
    code = bench.strip().upper()
    matches = [station for station in stations
        if str(station.get("code") or "").strip().upper() == code
        and station.get("active", True) and station.get("enabled", True)]
    if len(matches) != 1 or not matches[0].get("id"):
        raise ValueError("本机台架不是平台有效工位，请选择已启用的工位编号或刷新平台选项。")
    result = dict(profile)
    # A planned station is a default in the campaign, not a constraint on the case.
    # Missing non-station context and conflicting case/sample codes still block.
    station_fields = {"station_id", "station_code", "bench_id", "station_code/bench_id 编号不一致"}
    missing = [str(field) for field in profile.get("missing", []) if field not in station_fields]
    result.update(bench_id=code, station_code=code, station_id=str(matches[0]["id"]), missing=missing)
    result["ready"] = not missing and (bool(profile.get("ready")) or bool(profile.get("missing")))
    return result


def profile_matches_limb(profile: Mapping[str, object], limb: str) -> bool:
    spec = LIMB_PARTS.get(str(limb or "").strip())
    if spec is None:
        return True
    case_parts, sample_parts = spec
    case = case_part(str(profile.get("case_code") or profile.get("test_case_id") or ""))
    if case not in case_parts:
        return False
    sample = sample_part(str(profile.get("sample_id") or profile.get("asset_code") or ""))
    if limb == "waist_hip" and sample and sample != case:
        return False
    return not sample or sample in sample_parts
