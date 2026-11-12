"""Schema for administrator-signed exact motor OTA transitions."""
import re

V1 = "rp1-lro-ota-compatibility-v1"
V2 = "rp1-lro-ota-compatibility-v2"
V3 = "rp1-lro-ota-compatibility-v3"
TRANSITION_SCHEMAS = (V2, V3)
NUMERIC_LEGACY_108 = "numeric_legacy_108"
MODEL_NAMES = {0: "LRO_5550", 1: "LRO_6562", 2: "LRO_8462", 3: "LRO_10062"}
# The 2026-10-28 15:37 binary pinned by RoboParty's deployment. This
# identifies the file only; it does not authorize any source motor.
BASELINE_109_SHA256 = "7277645da5e2d461e0339e87e21b5603abcb13065416fb416a1e279d48eb966a"
_SOURCE = {"model", "hardware", "protocol", "raw_version", "software_version",
           "ota_protocol", "evidence_url", "evidence_sha256"}
_DESTINATION = {"hardware", "protocol", "raw_version", "software_version", "firmware_sha256"}


def firmware_generation(raw_version: str | None) -> int | None:
    """Display/selection label only; never derive numeric or protocol versions."""
    for generation in (108, 109):
        if raw_version in (str(generation), f"5.0.0.{generation}", f"V5.0.0.{generation}"):
            return generation
    return None


def numeric_version(value):
    if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", value):
        raise ValueError("需要三段数值软件版本")
    parts = tuple(map(int, value.split(".")))
    if any(part > 255 for part in parts):
        raise ValueError("数值软件版本超出设备回读范围")
    return parts


def _exact(value):
    return re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._+\-]{0,63}", value)


def _sha256(value):
    return re.fullmatch(r"[0-9a-f]{64}", value)


def validate_policy(policy):
    if (not isinstance(policy, dict) or policy.get("schema") not in (V1, *TRANSITION_SCHEMAS)
            or not isinstance(policy.get("entries"), list)
            or not 1 <= len(policy["entries"]) <= 1024):
        raise ValueError("OTA兼容映射结构无效")
    transition = policy["schema"] in TRANSITION_SCHEMAS
    keys_expected = {"schema", "entries"}
    if transition and "preferred_109_sha256" in policy:
        keys_expected.add("preferred_109_sha256")
        preference = policy["preferred_109_sha256"]
        if not isinstance(preference, str) or not _sha256(preference):
            raise ValueError("当前 109 固件 SHA256 无效")
    if set(policy) != keys_expected:
        raise ValueError("OTA兼容映射结构无效")
    keys = set()
    target_hashes = set()
    for row in policy["entries"]:
        if not isinstance(row, dict):
            raise ValueError("OTA兼容映射条目无效")
        numeric_only = policy["schema"] == V3 and row.get("source_identity") == NUMERIC_LEGACY_108
        source_fields = (_SOURCE - {"raw_version"}) | {"source_identity"} if numeric_only else _SOURCE
        if (set(row) != (source_fields | {"target"} if transition else source_fields)
                or any(not isinstance(row[k], str) for k in source_fields)
                or row["model"] not in MODEL_NAMES.values()
                or row["ota_protocol"] != "lro-canfd-xcp-v1"
                or not all(_exact(row[k]) for k in ("hardware", "protocol"))
                or (not numeric_only and not _exact(row["raw_version"]))
                or not re.fullmatch(r"https://[^\s]+", row["evidence_url"])
                or not _sha256(row["evidence_sha256"])):
            raise ValueError("OTA兼容映射条目缺少明确版本或来源证据")
        current = numeric_version(row["software_version"])
        if numeric_only and row["software_version"] != "0.1.8":
            raise ValueError("108 数值来源必须明确签署软件版本 0.1.8")
        if not numeric_only and current < (1, 0, 8):
            raise ValueError("OTA兼容映射的数值软件版本低于支持范围")
        identity = (row["model"], row["hardware"], row["protocol"], row.get("raw_version"), numeric_only)
        if transition:
            dest = row["target"]
            if (not isinstance(dest, dict) or set(dest) != _DESTINATION
                    or any(not isinstance(v, str) for v in dest.values())
                    or not all(_exact(dest[k]) for k in ("hardware", "protocol", "raw_version"))
                    or not _sha256(dest["firmware_sha256"])
                    or dest["hardware"] != row["hardware"]
                    or (not numeric_only and firmware_generation(row["raw_version"]) != 108)
                    or firmware_generation(dest["raw_version"]) != 109
                    or numeric_version(dest["software_version"]) < current):
                raise ValueError("需要精确的108→109目标版本、原硬件及固件哈希")
            # Two aliases for the same signed package cannot authorize different readbacks.
            identity += (dest["firmware_sha256"], dest["raw_version"], dest["software_version"])
            target_hashes.add(dest["firmware_sha256"])
        if identity in keys:
            raise ValueError("OTA兼容映射有重复或歧义")
        keys.add(identity)
    if "preferred_109_sha256" in policy and policy["preferred_109_sha256"] not in target_hashes:
        raise ValueError("当前 109 固件缺少精确版本适用条目")
    return policy
