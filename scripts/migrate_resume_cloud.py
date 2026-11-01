"""Apply the Resume-cloud schema only to a dedicated, otherwise empty database."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from soloscale.resume_cloud_repository import (
    BASE_SCHEMA_RELATIONS,
    LATEST_SCHEMA_RELATIONS,
    LATEST_SCHEMA_VERSION,
    RATE_LIMIT_SCHEMA_RELATIONS,
)


def main() -> None:
    database_url = os.environ.get("RESUME_CLOUD_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("RESUME_CLOUD_DATABASE_URL is required")
    migration_dir = Path(__file__).parents[1] / "migrations" / "resume_cloud"
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL search_path TO public")
        cur.execute("""SELECT n.nspname, c.relname
            FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')""")
        existing = set(cur.fetchall())
        if existing - LATEST_SCHEMA_RELATIONS:
            raise SystemExit("refusing database with unrelated user relations")
        if existing and existing not in (
            BASE_SCHEMA_RELATIONS,
            RATE_LIMIT_SCHEMA_RELATIONS,
            LATEST_SCHEMA_RELATIONS,
        ):
            raise SystemExit("refusing incomplete Resume cloud schema")
        if not existing:
            cur.execute((migration_dir / "0001_init.sql").read_text())
            existing = BASE_SCHEMA_RELATIONS
        cur.execute("SELECT version FROM resume_cloud_schema_version ORDER BY version")
        versions = [row[0] for row in cur.fetchall()]
        if existing == BASE_SCHEMA_RELATIONS:
            if versions != [1]:
                raise SystemExit("refusing Resume cloud v1 relation/version mismatch")
            cur.execute((migration_dir / "0002_rate_limits.sql").read_text())
            cur.execute("UPDATE resume_cloud_schema_version SET version=2 WHERE version=1")
            existing = RATE_LIMIT_SCHEMA_RELATIONS
            versions = [2]
        if existing == RATE_LIMIT_SCHEMA_RELATIONS:
            if versions != [2]:
                raise SystemExit("refusing Resume cloud v2 relation/version mismatch")
            cur.execute((migration_dir / "0003_result_cache.sql").read_text())
            cur.execute("UPDATE resume_cloud_schema_version SET version=3 WHERE version=2")
            existing = LATEST_SCHEMA_RELATIONS
            versions = [3]
        if existing == LATEST_SCHEMA_RELATIONS and versions == [3]:
            cur.execute((migration_dir / "0004_traceparent.sql").read_text())
            cur.execute("UPDATE resume_cloud_schema_version SET version=4 WHERE version=3")
            versions = [LATEST_SCHEMA_VERSION]
        if existing != LATEST_SCHEMA_RELATIONS or versions != [LATEST_SCHEMA_VERSION]:
            raise SystemExit("refusing Resume cloud latest relation/version mismatch")


if __name__ == "__main__":
    main()
