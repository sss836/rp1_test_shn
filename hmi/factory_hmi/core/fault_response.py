"""Software fault response is not a mechanical safety assertion."""
import time

from .backend import MotorDisableError

def new_fault_response():
    return {"playback_stop": "requested", "disable_request": "requested",
                "disable_feedback": "unknown", "safe_confirmed": False,
                "requested_at_monotonic_s": time.monotonic(), "error": None}

def request_fault_disable(backend, response=None):
    response = new_fault_response() if response is None else response
    try:
        backend.disable()
    except MotorDisableError as exc:
        response.update(disable_request="failed" if exc.send_failed else "sent",
                        disable_feedback="unconfirmed", error=str(exc))
        return response
    except Exception as exc:
        response.update(disable_request="failed", disable_feedback="unknown", error=f"{type(exc).__name__}: {exc}")
        return response
    response["disable_request"] = "sent"
    try:
        response["disable_feedback"] = backend.disable_feedback()
    except Exception as exc:
        response.update(disable_feedback="unknown", error=f"{type(exc).__name__}: {exc}")
    return response


def disable_result_text(response):
    request = {"requested": "失能请求中", "sent": "失能请求已发送", "failed": "失能请求失败",
               "not_requested": "未发出失能请求"}.get(response.get("disable_request"), "失能请求未知")
    feedback = {"confirmed_mock": "mock反馈已失能", "confirmed": "本次失能反馈已确认",
                "unconfirmed": "实际失能反馈未确认"}.get(response.get("disable_feedback"), "实际失能反馈未知")
    return f"{request} · {feedback}"


def fault_error_detail(exc):
    detail = str(exc)
    response = getattr(exc, "motor_fault_response", None)
    if response is not None:
        detail += "\n本次故障保护：" + disable_result_text(response)
        if response.get("error"):
            detail += f"（{response['error']}）"
    return detail
