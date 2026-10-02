"""Apply the Resume-cloud schema only to a dedicated, otherwise empty database."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

EXPECTED = {("public", "resume_cloud_schema_version"), ("public", "resume_cloud_tasks")}


def main() -> None:
    database_url = os.environ.get("RESUME_CLOUD_DATABASE_URL", "")
    if not database_url:
        raise SystemExit("RESUME_CLOUD_DATABASE_URL is required")
    sql = (Path(__file__).parents[1] / "migrations" / "resume_cloud" / "0001_init.sql").read_text()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SET LOCAL search_path TO public")
        cur.execute("""SELECT n.nspname, c.relname
            FROM pg_class AS c JOIN pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema'
              AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')""")
        existing = set(cur.fetchall())
        if existing - EXPECTED:
            raise SystemExit("refusing database with unrelated user relations")
        if existing == EXPECTED:
            cur.execute("SELECT version FROM resume_cloud_schema_version")
            if cur.fetchone() != (1,):
                raise SystemExit("refusing unexpected Resume cloud schema version")
            return
        if existing:
            raise SystemExit("refusing incomplete Resume cloud schema")
        cur.execute(sql)


if __name__ == "__main__":
    main()
