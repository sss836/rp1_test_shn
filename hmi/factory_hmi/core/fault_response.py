"""Software fault response is not a mechanical safety assertion."""
import time

def new_fault_response():
    return {"playback_stop": "requested", "disable_request": "requested",
                "disable_feedback": "unknown", "safe_confirmed": False,
                "requested_at_monotonic_s": time.monotonic(), "error": None}

def request_fault_disable(backend, response=None):
    response = new_fault_response() if response is None else response
    try:
        backend.disable()
        response["disable_request"] = "sent"
        # A local enabled flag only describes command state. Hardware drivers
        # currently expose no independent disable acknowledgement.
        response["disable_feedback"] = backend.disable_feedback()
    except Exception as exc:
        response.update(disable_request="failed", error=f"{type(exc).__name__}: {exc}")
    return response
