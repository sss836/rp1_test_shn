"""Business partition metadata; deriving a view never changes sealed evidence."""
from __future__ import annotations

from collections.abc import Mapping
import re
from uuid import UUID

DOMAIN_LABELS = {"RESEARCH": "研发", "FACTORY": "工厂", "UNCLASSIFIED": "分区待归类"}


def bench_evidence(value: object) -> str:
    code = str(value or "").strip().upper()
    if code in {"", "UNKNOWN", "NONE", "NULL"} or not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,99}", code):
        return ""
    try:
        UUID(code)  # Some legacy manifests used a station UUID as bench_id.
        return ""
    except ValueError:
        return code


def domain_metadata(context: Mapping[str, object]) -> dict[str, str]:
    domain = str(context.get("data_domain") or "").upper()
    source = str(context.get("data_domain_source") or "")
    if domain in {"RESEARCH", "FACTORY"} and source not in {"HISTORICAL_BENCH", "NO_EVIDENCE"}:
        source = "EXPLICIT"
    else:
        bench = bench_evidence(context.get("bench_id"))
        domain = ("FACTORY" if bench.startswith("A") else "RESEARCH") if bench else "UNCLASSIFIED"
        source = "HISTORICAL_BENCH" if bench else "NO_EVIDENCE"
    serial = str(context.get("serial_number") or context.get("sample_id") or context.get("robot_id") or "").strip() if domain == "FACTORY" else ""
    if serial == "unknown" and source != "EXPLICIT":
        serial = ""
    return {"data_domain": domain, "data_domain_source": source, "serial_number": serial}


def archive_domain_fields(context: Mapping[str, object]) -> dict[str, str]:
    """Only explicit selection is sent as explicit; server derives legacy evidence."""
    metadata = domain_metadata(context)
    fields = {"bench_id": bench_evidence(context.get("bench_id"))}
    if metadata["data_domain_source"] == "EXPLICIT":
        fields["data_domain"] = metadata["data_domain"]
    if metadata["serial_number"]:
        fields["serial_number"] = metadata["serial_number"]
    return {key: value for key, value in fields.items() if value}
