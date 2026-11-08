#!/usr/bin/env python3
"""Verify 0059 -> current head preserves existing rows, in the disposable runner only."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url


NEW_TABLES = {
    "integration.robot_result_source", "integration.factory_robot_presence",
    "integration.robot_result_archive", "integration.robot_result_row",
    "integration.robot_result_review", "integration.robot_result_request",
}


def fingerprint(connection):
    tables = connection.execute(text("""SELECT schemaname,tablename FROM pg_tables
        WHERE schemaname NOT IN ('pg_catalog','information_schema')
          AND schemaname NOT LIKE 'pg_temp_%'
          AND NOT (schemaname='public' AND tablename='alembic_version')
        ORDER BY schemaname,tablename""")).all()
    quote = connection.dialect.identifier_preparer.quote_identifier
    result = {}
    for schema, table in tables:
        name = f"{quote(schema)}.{quote(table)}"
        count, digest = connection.execute(text(f"""SELECT count(*),md5(coalesce(
            string_agg(md5(to_jsonb(row)::text),'' ORDER BY md5(to_jsonb(row)::text)),'')
            ) FROM {name} AS row""")).one()
        result[f"{schema}.{table}"] = {"rows": count, "sha256_of_row_digest": hashlib.sha256(digest.encode()).hexdigest()}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("snapshot", "verify"))
    parser.add_argument("snapshot", type=Path)
    args = parser.parse_args()
    url = os.environ["DATABASE_URL"]
    if (os.environ.get("RP1_DISPOSABLE_UPGRADE_QA") != "yes"
            or not (make_url(url).database or "").startswith("rp1_presence")):
        raise RuntimeError("Only a disposable rp1_presence upgrade database is allowed")
    engine = create_engine(url)
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                head = connection.execute(text("SELECT version_num FROM public.alembic_version")).scalar_one()
                state = fingerprint(connection)
        if args.mode == "snapshot":
            assert head == "20261103_0059" and not (NEW_TABLES & state.keys())
            with args.snapshot.open("x", encoding="utf-8") as file:
                os.chmod(args.snapshot, 0o600)
                json.dump(state, file, sort_keys=True)
            print(json.dumps({"result": "SNAPSHOT", "head": head, "existing_tables": len(state), "production": False}), flush=True)
        else:
            assert head == "20261108_0061"
            before = json.loads(args.snapshot.read_text(encoding="utf-8"))
            assert set(state) - set(before) == NEW_TABLES
            assert all(state.get(key) == value for key, value in before.items()), "Existing rows changed during upgrade"
            assert all(state[key]["rows"] == 0 for key in NEW_TABLES)
            print(json.dumps({"result": "PASS", "upgrade": "0059 -> 0061", "existing_tables_preserved": len(before),
                              "new_empty_tables": len(NEW_TABLES), "production": False}), flush=True)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
