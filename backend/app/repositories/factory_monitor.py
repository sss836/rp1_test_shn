"""Read only factory console. Uses reported cache and immutable archive receipts."""
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session


def receipt_state(record: dict) -> str:
    if record.get('upload_state') == 'CORRUPT':
        return 'upload_error'
    if record.get('upload_state') != 'COMPLETE':
        return 'uploading'
    if record.get('processing_state') == 'FAILED':
        return 'parse_error'
    if record.get('processing_state') != 'READY':
        return 'parsing'
    if record.get('submission_status') == 'APPROVED':
        return 'archived'
    if record.get('submission_status') == 'PENDING':
        return 'awaiting_review'
    if record.get('submission_status') == 'REJECTED':
        return 'rejected'
    if not record.get('verified'):
        return 'awaiting_verification'
    if not record.get('classified'):
        return 'awaiting_context'
    return 'ready_to_submit'


RECORDS = """SELECT a.id AS cursor,a.public_id AS archive_id,a.execution_uuid,a.archive_code,a.test_name,
    d.serial_number,d.bench_id,a.upload_state,a.processing_state,left(a.processing_error,1000) AS processing_error,
    coalesce(fs.status,legacy.status) AS submission_status,
    a.verification_revision IS NOT NULL AS verified,a.classification_revision IS NOT NULL AS classified,a.created_at
    FROM integration.execution_archive a
    JOIN LATERAL integration.archive_domain_info(a.id) d ON true
    LEFT JOIN integration.factory_submission fs ON fs.archive_id=a.id
    LEFT JOIN LATERAL integration.archive_legacy_factory_info(a.id) legacy ON true
    WHERE d.data_domain='FACTORY' """


class FactoryMonitorRepository:
    def __init__(self, session: Session):
        self.session = session

    def monitor(self, cursor: UUID | None, limit: int, work_before: int | None, work_limit: int) -> dict:
        now = datetime.now(timezone.utc)
        rows = self.session.execute(text("SELECT * FROM integration.list_factory_benches(CAST(:cursor AS uuid),:limit)"),
                                    {'cursor': str(cursor) if cursor else None, 'limit': limit + 1}).mappings().all()
        benches = [dict(row) for row in rows[:limit]]
        identities = set()
        for bench in benches:
            for connection in bench['connections']:
                for run in connection.get('runs', []):
                    # Never correlate independent executions by SN, free text test ID or time.
                    try:
                        identities.add(str(UUID(str(run.get('execution_uuid')))))
                    except (ValueError, TypeError, AttributeError):
                        pass
        receipts = []
        if identities:
            receipts = [dict(row) for row in self.session.execute(text(RECORDS +
                ' AND a.execution_uuid=ANY(CAST(:identities AS uuid[])) ORDER BY a.id DESC LIMIT :limit'),
                {'identities': sorted(identities), 'limit': len(identities)}).mappings()]
        for record in receipts:
            record['receipt_state'] = receipt_state(record)
        work = [dict(row) for row in self.session.execute(text(RECORDS + """
            AND coalesce(fs.status,legacy.status,'')<>'APPROVED'
            AND (CAST(:before AS bigint) IS NULL OR a.id<:before)
            ORDER BY a.id DESC LIMIT :limit"""), {'before': work_before, 'limit': work_limit + 1}).mappings()]
        for record in work[:work_limit]:
            record['receipt_state'] = receipt_state(record)
        return {'items': benches,
                'next_cursor': str(rows[limit - 1]['station_id']) if len(rows) > limit else None,
                'receipts': receipts, 'work_items': work[:work_limit],
                'work_next_cursor': work[work_limit - 1]['cursor'] if len(work) > work_limit else None,
                'as_of_at': now, 'offline_after_seconds': 45, 'snapshot_stale_after_seconds': 20}
