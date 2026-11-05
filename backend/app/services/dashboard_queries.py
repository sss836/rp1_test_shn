"""A request-local database deadline for the dashboard home read model."""
from __future__ import annotations

from contextlib import contextmanager
from math import floor
from time import monotonic
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.errors import ApiError

HOME_QUERY_BUDGET_SECONDS = 8.0


def _timeout_error() -> ApiError:
    return ApiError(
        503,
        "dashboard_home_timeout",
        "首页统计查询超时，请稍后重试。",
    )


@contextmanager
def dashboard_home_deadline(
    session: Session, *, seconds: float = HOME_QUERY_BUDGET_SECONDS
) -> Iterator[None]:
    """Give all home SQL statements one shared deadline, without changing RLS.

    Authentication and connection acquisition precede this read-model budget.
    SET LOCAL is reset by the request transaction's commit/rollback; the listener
    belongs only to this checked-out connection and is always removed.
    """
    deadline = monotonic() + seconds
    connection = session.connection()

    def remaining_ms() -> int:
        remaining = floor((deadline - monotonic()) * 1000)
        if remaining <= 0:
            raise _timeout_error()
        return remaining

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        # Use the same DBAPI cursor directly so the SET does not recursively fire
        # SQLAlchemy's listener. Each statement receives only the time left.
        cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (f"{remaining_ms()}ms",),
        )
        remaining_ms()

    event.listen(connection, "before_cursor_execute", before_cursor_execute)
    try:
        yield
        remaining_ms()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "57014":
            raise _timeout_error() from exc
        raise
    finally:
        event.remove(connection, "before_cursor_execute", before_cursor_execute)
