"""Process-wide budget shared by interactive curve and exact-analysis connections."""
from contextlib import contextmanager
import threading
from app.core.errors import ApiError
_slots=threading.BoundedSemaphore(2)
@contextmanager
def query_slot():
    if not _slots.acquire(blocking=False):
        raise ApiError(429,'curve_query_busy','查询资源正忙，请稍后重试或缩小范围。')
    try:yield
    finally:_slots.release()
