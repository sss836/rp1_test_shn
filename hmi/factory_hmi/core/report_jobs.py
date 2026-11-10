"""Durable, root-scoped report work. Renderers own files only."""

from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)
MAX_PENDING_JOBS = 8
RENDER_TIMEOUT_S = 900
_ACTIVE = ("pending", "running", "cancelling", "publishing")
_dispatchers: dict[Path, threading.Thread] = {}
_dispatch_lock = threading.Lock()


@contextmanager
def _database(root):
    db = sqlite3.connect(root / "report-jobs.sqlite3", timeout=3)
    try:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS jobs("
                "id TEXT PRIMARY KEY,state TEXT,source TEXT,context TEXT,error TEXT,updated REAL)"
            )
            yield db
    finally:
        db.close()


def _queue(root, source, context):
    root.mkdir(parents=True, exist_ok=True)
    key = str(context["test_id"])
    payload = json.dumps(context, sort_keys=True, ensure_ascii=False)
    source = str(source)
    with _database(root) as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT state,source,context FROM jobs WHERE id=?", (key,)).fetchone()
        if existing is not None:
            state, previous_source, previous_payload = existing
            if (previous_source, previous_payload) != (source, payload):
                raise ValueError("report identity payload conflict")
            if state in {"cancelled", "cancelling"}:
                raise RuntimeError("report job cancelled")
            if state != "failed":
                return key
        count, = db.execute(
            "SELECT count(*) FROM jobs WHERE state IN ('pending','running','cancelling','publishing')"
        ).fetchone()
        if count >= MAX_PENDING_JOBS:
            raise RuntimeError("report queue full; source evidence retained")
        db.execute(
            "INSERT INTO jobs VALUES (?,'pending',?,?,NULL,?) "
            "ON CONFLICT(id) DO UPDATE SET state='pending',error=NULL,updated=excluded.updated",
            (key, source, payload, time.time()),
        )
    return key


def report_job_status(root, key):
    root = Path(root)
    if not (root / "report-jobs.sqlite3").is_file():
        raise ValueError("report job not found")
    with _database(root) as db:
        row = db.execute("SELECT state,error,updated FROM jobs WHERE id=?", (key,)).fetchone()
    if row is None:
        raise ValueError("report job not found")
    state, error, updated = row
    return {"id": key, "state": state, "error": error, "updated_at": updated}


def _set_state(root, key, state, error=None):
    with _database(root) as db:
        db.execute(
            "UPDATE jobs SET state=?,error=?,updated=? WHERE id=?",
            (state, error, time.time(), key),
        )


def _run_enqueued(root, key, *, timeout_s=RENDER_TIMEOUT_S):
    # The renderer inherits this lock. If the gateway exits mid-report, the
    # running child can still finish before a restarted dispatcher claims it.
    with (root / ".report-process.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with _database(root) as db:
            db.execute("BEGIN IMMEDIATE")
            state, = db.execute("SELECT state FROM jobs WHERE id=?", (key,)).fetchone()
            if state not in _ACTIVE:
                return
            if state in {"pending", "running"}:
                db.execute("UPDATE jobs SET state='running',updated=? WHERE id=?", (time.time(), key))

        env = {
            # The portable Debian interpreter and NumPy wheel use these
            # paths. RP1 credentials and device settings are not inherited.
            **{name: os.environ[name] for name in ("PYTHONHOME", "LD_LIBRARY_PATH") if name in os.environ},
            "PATH": os.environ.get("PATH", os.defpath),
            "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
        process = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "factory_hmi.core.report_jobs", str(root), key],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                pass_fds=(lock.fileno(),),
            )
            process.wait(timeout=timeout_s)
            state = report_job_status(root, key)["state"]
            if state in _ACTIVE:
                _set_state(root, key, "failed", f"report process exited without a receipt ({process.returncode})")
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if report_job_status(root, key)["state"] in _ACTIVE:
                _set_state(root, key, "failed", "report job timed out; source evidence retained")
        except Exception as exc:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            if report_job_status(root, key)["state"] in _ACTIVE:
                _set_state(root, key, "failed", f"report process unavailable ({type(exc).__name__})")


def _dispatch(root):
    try:
        while True:
            with _dispatch_lock:
                with _database(root) as db:
                    row = db.execute(
                        "SELECT id FROM jobs WHERE state IN ('pending','running','cancelling','publishing') "
                        "ORDER BY updated,id LIMIT 1"
                    ).fetchone()
                if row is None:
                    _dispatchers.pop(root, None)
                    return
            _run_enqueued(root, row[0])
    finally:
        with _dispatch_lock:
            if _dispatchers.get(root) is threading.current_thread():
                _dispatchers.pop(root, None)


def resume_report_jobs(root):
    """Recover accepted jobs even when the new window has a different station directory."""
    root = Path(root).resolve()
    if not (root / "report-jobs.sqlite3").is_file():
        return
    with _dispatch_lock:
        if root not in _dispatchers:
            worker = threading.Thread(target=_dispatch, args=(root,), name="factory-report-dispatch", daemon=True)
            _dispatchers[root] = worker
            worker.start()


def enqueue_report_job(root, source, context):
    """Return after a durable enqueue. Any device/CSV owner may now close."""
    root = Path(root).resolve()
    key = _queue(root, source, context)
    resume_report_jobs(root)
    return key


def run_report_job(root, source, context, *, timeout_s=RENDER_TIMEOUT_S):
    """Synchronous compatibility entry point for explicit recovery callers."""
    root = Path(root).resolve()
    key = _queue(root, source, context)
    _run_enqueued(root, key, timeout_s=timeout_s)
    status = report_job_status(root, key)
    if status["state"] != "completed":
        raise RuntimeError(status["error"] or f"report job {status['state']}")
    from .report import ReportManager
    ReportManager(root).validate(key)


def cancel_report_job(root, key):
    root = Path(root)
    if not (root / "report-jobs.sqlite3").is_file():
        raise ValueError("report job not found")
    with _database(root) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT state FROM jobs WHERE id=?", (key,)).fetchone()
        if row is None:
            raise ValueError("report job not found")
        if row[0] in {"completed", "publishing", "failed"}:
            return {"cancelled": False, "state": row[0]}
        # A running job checks this fence before its atomic file publication.
        state = "cancelling" if row[0] in {"running", "cancelling"} else "cancelled"
        db.execute("UPDATE jobs SET state=?,updated=? WHERE id=?", (state, time.time(), key))
    return {"cancelled": state == "cancelled", "state": state, "source_evidence_retained": True}


def recover_report_jobs(root):
    root = Path(root).resolve()
    if not (root / "report-jobs.sqlite3").is_file():
        return []
    with _database(root) as db:
        rows = db.execute(
            "SELECT id,source,context,state FROM jobs "
            "WHERE state IN ('pending','running','cancelling','publishing','failed') ORDER BY updated LIMIT ?",
            (MAX_PENDING_JOBS,),
        ).fetchall()
    outcomes = []
    for key, source, payload, state in rows:
        try:
            if state in {"cancelling", "publishing"}:
                _run_enqueued(root, key)
            else:
                run_report_job(root, source, json.loads(payload))
            outcomes.append(report_job_status(root, key))
        except Exception as exc:
            outcomes.append({"id": key, "state": "failed", "error": str(exc), "reason": str(exc)})
    return outcomes


class _Cancelled(Exception):
    pass


def _render(root, key):
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (RENDER_TIMEOUT_S, RENDER_TIMEOUT_S))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    os.nice(10)
    try:
        from .recording.policy import CapacityReservations
        from .report import ReportManager, _safe_test_id

        with _database(root) as db:
            source, payload = db.execute("SELECT source,context FROM jobs WHERE id=?", (key,)).fetchone()
        context = json.loads(payload)
        manager = ReportManager(root)
        if key != _safe_test_id(key) or key != str(context["test_id"]):
            raise ValueError("invalid report identity")

        @contextmanager
        def publish():
            # Serialize cancellation and the short publication commit. Once
            # sealed evidence is published, a cancellation cannot undo it.
            with _database(root) as db:
                db.execute("BEGIN IMMEDIATE")
                state, = db.execute("SELECT state FROM jobs WHERE id=?", (key,)).fetchone()
                if state != "running":
                    raise _Cancelled
                yield
                db.execute("UPDATE jobs SET state='publishing',updated=? WHERE id=?", (time.time(), key))

        with manager.evidence_lock():
            if (manager.executions_root / key / "manifest.json").exists():
                # Recover the acknowledgement of a previously sealed report.
                manager.validate(key)
                manager.refresh_history_index(key)
                manager.write_ledger(manager.iter_history())
            else:
                manager._finalize(source, context, publication=publish)
        CapacityReservations(root / "record-capacity.sqlite3").complete(context.get("execution_uuid", ""))
        Path(source).with_suffix(".context.json").unlink(missing_ok=True)
        _set_state(root, key, "completed")
    except _Cancelled:
        _set_state(root, key, "cancelled", "report job cancelled; source evidence retained")
    except Exception as exc:
        if report_job_status(root, key)["state"] != "completed":
            _set_state(root, key, "failed", f"{type(exc).__name__}: {exc}"[:1000])


if __name__ == "__main__":
    _render(Path(sys.argv[1]), sys.argv[2])
