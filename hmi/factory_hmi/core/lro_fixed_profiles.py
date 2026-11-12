"""Select existing fixed 108/109 SDK tables from actual version observations."""
from .motor_versions import firmware_generation, decode_version


def fixed_profile(observation):
    if observation.get("status") != "known":return None
    raw=observation.get("raw_firmware")
    generation=firmware_generation(raw)
    numeric=observation.get("numeric_software") or observation.get("software")
    numeric_generation={"0.1.8":108,"0.1.9":109}.get(numeric)
    if raw is not None and generation is None:return None
    if generation and numeric_generation and generation != numeric_generation:return None
    if generation is None:
        if observation.get("hardware") != "0.1.0" or observation.get("protocol") != "0.0.1.17":return None
        if observation.get('software') not in (None,numeric):return None
        errors=observation.get('errors') or {}
        if '0x1e' in errors:return None
        try:
            decoded=decode_version(bytes.fromhex(observation['raw_responses']['0x1e']))
        except (KeyError,TypeError,ValueError):return None
        if any(decoded.get(k)!=v for k,v in (('software',numeric),('hardware',observation.get('hardware')),('protocol',observation.get('protocol')))):return None
        generation=numeric_generation
    return {108:"legacy",109:"v1.03"}.get(generation)
