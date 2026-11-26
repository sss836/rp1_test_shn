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
        # The backend reports what the drive feedback actually confirms.
        response["disable_feedback"] = backend.disable_feedback()
    except Exception as exc:
        response.update(disable_request="failed", error=f"{type(exc).__name__}: {exc}")
        # Preserve an explicit failure to confirm. Never promote an earlier
        # successful stop to confirmation of this failed request.
        try:
            if backend.disable_feedback() == "unconfirmed":
                response["disable_feedback"] = "unconfirmed"
        except Exception:
            pass
    return response
