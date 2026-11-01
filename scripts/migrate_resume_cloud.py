"""Apply the Resume-cloud schema only to a dedicated, otherwise empty database."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

EXPECTED = {
    ("public", "resume_cloud_schema_version"),
    ("public", "resume_cloud_tasks"),
    ("public", "resume_cloud_rate_limits"),
}


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
        if existing - EXPECTED:
            raise SystemExit("refusing database with unrelated user relations")
        initial = {("public", "resume_cloud_schema_version"), ("public", "resume_cloud_tasks")}
        if existing and existing not in (initial, EXPECTED):
            raise SystemExit("refusing incomplete Resume cloud schema")
        if not existing:
            cur.execute((migration_dir / "0001_init.sql").read_text())
            existing = initial
        cur.execute("SELECT version FROM resume_cloud_schema_version")
        row = cur.fetchone()
        if row is None or row[0] not in {1, 2}:
            raise SystemExit("refusing unexpected Resume cloud schema version")
        if row[0] == 1:
            cur.execute((migration_dir / "0002_rate_limits.sql").read_text())
            cur.execute("UPDATE resume_cloud_schema_version SET version=2 WHERE version=1")


if __name__ == "__main__":
    main()
