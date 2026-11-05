"""Factory summary ledger, deliberately independent from execution/MTBF writers."""
from datetime import datetime, timezone
import json

from sqlalchemy import text

from app.core.errors import ApiError


DETAIL = """SELECT a.public_id AS id,a.id AS cursor,a.source_id,a.robot_id,a.source_kind,
 a.serial_number,a.model,a.protocol,a.filename,a.size_bytes,a.sha256,a.verified,a.completed,
 a.reason,a.status,a.revision,a.columns,a.row_count,a.created_at,
 previous.public_id AS previous_archive_id,
 integration.robot_result_actor_name(a.id,a.owner_id) AS owner_name,
 (iam.can_review_factory() AND a.status='PENDING') AS can_review,
 false AS formal_statistics_eligible,'FACTORY' AS data_domain,
 (SELECT count(*) FROM integration.robot_result_row r WHERE r.archive_id=a.id AND r.suite='SYS' AND r.level='项目') AS project_count
 FROM integration.robot_result_archive a
 LEFT JOIN integration.robot_result_archive previous ON previous.id=a.previous_archive_id"""


class FactoryRobotRepository:
    def __init__(self, session):
        self.session = session

    def presence(self, payload):
        count = self.session.execute(text("SELECT integration.record_factory_robot_presence(CAST(:p AS jsonb))"),
            {"p": json.dumps(payload, ensure_ascii=False)}).scalar_one()
        return {"source_id": payload["source_id"], "accepted": count,
                "as_of_at": datetime.now(timezone.utc), "offline_after_seconds": 45}

    def robots(self, source_id=None, robot_id=None, serial_number=None, limit=200):
        # Arm the outer statement before starting either query; a function-local
        # timeout alone does not reliably limit an already-running caller.
        self.session.execute(text("SELECT set_config('statement_timeout','3s',true)"))
        parameters, predicates = {"limit": limit + 1}, []
        for key, value in (("source_id", source_id), ("robot_id", robot_id), ("serial_number", serial_number)):
            if value is not None:
                predicates.append(f"p.{key}=:{key}"); parameters[key] = value
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        rows = self.session.execute(text("""SELECT p.source_id,p.robot_id,p.serial_number,p.model,p.station_name,p.source_kind,p.data_fresh,
          p.last_seen_at,p.observed_at,
          p.online AND p.last_seen_at>clock_timestamp()-interval '45 seconds'
            AND integration.robot_result_source_enabled(p.source_id)
            AND (p.observed_at IS NULL OR p.observed_at>clock_timestamp()-interval '45 seconds') AS online
          FROM integration.factory_robot_presence p""" + where + " ORDER BY p.source_id,p.robot_id LIMIT :limit"), parameters).mappings().all()
        # Only current approved project rows are evidence for the card's history.
        # Children, untested/not-applicable rows and pending/rejected rows do not
        # add cases or attempts. A newer unapproved correction hides old values.
        keys = [{key: row[key] for key in ("source_id", "robot_id", "serial_number")} for row in rows[:limit]]
        projects = self.session.execute(text("SELECT * FROM integration.robot_result_case_cards(CAST(:keys AS jsonb))"),
            {"keys": json.dumps(keys, ensure_ascii=False)}).mappings().all() if keys else []
        grouped = {}
        visible = {(row["source_id"], row["robot_id"], row["serial_number"]) for row in rows[:limit]}
        for project in projects:
            key = (project["source_id"], project["robot_id"], project["serial_number"])
            if key not in visible:
                continue
            cases = grouped.setdefault(key, {})
            cases[project["case_code"]] = {
                "case_code": project["case_code"], "test_name": project["test_name"],
                "record_count": project["record_count"],
                "latest_status": project["latest_status"],
                "approval_status": "APPROVED", "verdict": project["verdict"],
                "latest_archive_id": project["latest_archive_id"],
            }
        items = []
        for row in rows[:limit]:
            item = dict(row)
            item["cases"] = list(grouped.get((row["source_id"], row["robot_id"], row["serial_number"]), {}).values())
            item["formal_statistics_eligible"] = False
            items.append(item)
        return {"items": items, "as_of_at": datetime.now(timezone.utc),
                "offline_after_seconds": 45, "truncated": len(rows) > limit}

    def listing(self, source_id=None, robot_id=None, serial_number=None, status=None, before=None, limit=50):
        parameters, predicates = {"limit": limit + 1}, []
        for key, value in (("source_id", source_id), ("robot_id", robot_id), ("serial_number", serial_number), ("status", status)):
            if value is not None:
                predicates.append(f"a.{key}=:{key}"); parameters[key] = value
        if before is not None:
            predicates.append("a.id<:before"); parameters["before"] = before
        sql = DETAIL + (" WHERE " + " AND ".join(predicates) if predicates else "") + " ORDER BY a.id DESC LIMIT :limit"
        rows = self.session.execute(text(sql), parameters).mappings().all()
        return {"items": [dict(r) for r in rows[:limit]],
                "next_cursor": rows[limit-1]["cursor"] if len(rows) > limit else None,
                "as_of_at": datetime.now(timezone.utc)}

    def get(self, public_id, history=True):
        result = self.session.execute(text(DETAIL + " WHERE a.public_id=:id"), {"id": public_id}).mappings().first()
        if result is None:
            raise ApiError(404, "factory_result_not_found", "工厂结果归档不存在或当前账号不可见。")
        item = dict(result)
        if history:
            item["rows"] = [dict(row) for row in self.session.execute(text("""SELECT r.record_id,r.version,r.cell_values AS values,
              r.case_code,r.test_name,r.level,r.suite,r.original_check_id,r.verdict,r.row_sha256
              FROM integration.robot_result_row r JOIN integration.robot_result_archive a ON a.id=r.archive_id
              WHERE a.public_id=:id ORDER BY r.id"""), {"id": public_id}).mappings()]
            item["review_history"] = [dict(row) for row in self.session.execute(text("""SELECT r.revision,r.decision,r.reason,r.created_at,
              integration.robot_result_actor_name(r.archive_id,r.actor_id) AS reviewer_name
              FROM integration.robot_result_review r JOIN integration.robot_result_archive a ON a.id=r.archive_id
              WHERE a.public_id=:id ORDER BY r.created_at"""), {"id": public_id}).mappings()]
        return item

    def create(self, payload, key):
        reference = self.session.execute(text("SELECT integration.create_robot_result_archive(CAST(:p AS jsonb),:key)"),
            {"p": json.dumps(payload, ensure_ascii=False), "key": key}).scalar_one()
        return self.get(reference, history=False)

    def review(self, public_id, payload, key, file_sha256):
        reference = self.session.execute(text("SELECT integration.review_robot_result_archive(:id,CAST(:p AS jsonb),:key,:sha)"),
            {"id": public_id, "p": json.dumps(payload, ensure_ascii=False), "key": key, "sha": file_sha256}).scalar_one()
        return self.get(reference)
