"""Versioned HMI module aliases; canonical business parts remain distinct."""

# These module keys correspond to desktop/case_filter.py. Short-arm hardware is
# SARM, not the UPPER assembly (waist plus two arms).
PART_ALIASES = {
    "upper_body": "UPPER", "上肢": "UPPER", "上肢总成": "UPPER",
    "lower_body": "LOWER", "下肢": "LOWER", "biped_waist": "LOWER", "双足腰": "LOWER",
    "left_arm": "ARM", "right_arm": "ARM", "手臂": "ARM", "左臂": "ARM", "右臂": "ARM",
    "left_short_arm": "SARM", "right_short_arm": "SARM", "短臂": "SARM",
    "left_leg": "LEG", "right_leg": "LEG", "腿": "LEG",
    "left_short_leg": "SLEG", "right_short_leg": "SLEG", "短腿": "SLEG",
    "waist_hip": "WAISTHIP", "腰髋": "WAISTHIP", "整机": "SYS",
}
PART_CODES = frozenset({"UPPER", "LOWER", "ARM", "SARM", "LEG", "SLEG", "WAISTHIP", "SYS"})


def known_part(value):
    text = str(value or "").strip()
    key=text.casefold().replace("-","_").replace(" ","_")
    return PART_ALIASES.get(key) or (text.upper() if text.upper() in PART_CODES else None)


def canonical_part(value):
    return known_part(value) or str(value or "").strip().upper()
