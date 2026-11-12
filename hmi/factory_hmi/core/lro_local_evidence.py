"""Narrow, locally confirmed legacy protocol evidence; off unless explicitly provisioned.

The file is an operator declaration, not a firmware read-back or OTA approval.
It is bound to the exact effective station configuration and individual nodes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
_NAME = "lro-local-protocol-evidence.json"
_SCHEMA = "rp1-lro-local-protocol-evidence-v1"
_NODE_KEYS = {"interface", "motor_id", "motor_model", "numeric_software", "hardware", "protocol"}
_ROOT_KEYS = {"schema", "configuration_sha256", "limb", "confirmed_profile", "confirmed_firmware_generation", "evidence", "nodes"}


@dataclass(frozen=True)
class LocalLegacyEvidence:
    configuration_sha256: str
    limb: str
    evidence: str
    # interface, motor ID, configured model, numeric software, hardware, protocol
    nodes: frozenset[tuple[str, int, int, str, str, str]]


def _numeric(value: Any, count: int) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){" + str(count - 1) + r"}", value)) and all(int(part) <= 255 for part in value.split('.'))


def _parse(raw: bytes) -> LocalLegacyEvidence:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate local protocol evidence key")
            result[key] = value
        return result
    data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    if not isinstance(data, dict) or set(data) != _ROOT_KEYS or data["schema"] != _SCHEMA:
        raise ValueError("invalid local protocol evidence schema")
    digest, limb, evidence, nodes = (data[k] for k in ("configuration_sha256", "limb", "evidence", "nodes"))
    if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or not isinstance(limb, str) or re.fullmatch(r"[a-z0-9_]{1,40}", limb) is None
            or data["confirmed_profile"] != "legacy" or type(data["confirmed_firmware_generation"]) is not int
            or data["confirmed_firmware_generation"] != 108 or not isinstance(evidence, str)
            or not 8 <= len(evidence.strip()) <= 500 or evidence != evidence.strip()
            or not isinstance(nodes, list) or not 1 <= len(nodes) <= 64):
        raise ValueError("invalid local protocol evidence content")
    parsed, seen = set(), set()
    for node in nodes:
        if not isinstance(node, dict) or set(node) != _NODE_KEYS:
            raise ValueError("invalid local protocol evidence node")
        interface, motor_id, model, numeric, hardware, protocol = (node[k] for k in (
            "interface", "motor_id", "motor_model", "numeric_software", "hardware", "protocol"))
        if (not isinstance(interface, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", interface) is None
                or type(motor_id) is not int or not 1 <= motor_id <= 2047
                or type(model) is not int or model not in {0, 1, 2, 3}
                or not _numeric(numeric, 3) or not _numeric(hardware, 3) or not _numeric(protocol, 4)):
            raise ValueError("invalid local protocol evidence identity")
        identity = (interface, motor_id)
        if identity in seen:
            raise ValueError("duplicate local protocol evidence node")
        seen.add(identity)
        parsed.add((interface, motor_id, model, numeric, hardware, protocol))
    return LocalLegacyEvidence(digest, limb, evidence, frozenset(parsed))


def read_local_legacy_evidence(state_root: str | None) -> LocalLegacyEvidence | None:
    """Return a validated immutable rule or None. Never follow a file symlink."""
    if not state_root or not Path(state_root).is_absolute():
        return None
    path = Path(state_root) / "config" / _NAME
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > 65536:
                raise ValueError("local protocol evidence file must be owned by the user, mode 0600, and at most 64 KiB")
            raw = f.read(65537)
            if len(raw) > 65536:
                raise ValueError("local protocol evidence file too large")
        return _parse(raw)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring invalid local protocol evidence: %s", exc)
        return None
