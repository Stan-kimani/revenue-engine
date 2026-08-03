"""Apply pending SQL migrations from migrations/, tracked in schema_migrations.

Safe to run repeatedly: each migration is applied at most once, in its own
transaction, with the tracking row inserted in the same transaction.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


async def run() -> int:
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    try:
        conn = await asyncpg.connect(database_url)
    except Exception as exc:
        print(f"Could not connect to DATABASE_URL: {exc}", file=sys.stderr)
        return 1

    try:
        await conn.execute(CREATE_TRACKING_TABLE)

        applied = {
            row["filename"] for row in await conn.fetch("SELECT filename FROM schema_migrations")
        }

        migrations = sorted(MIGRATIONS_DIR.glob("*.sql"))
        pending = [m for m in migrations if m.name not in applied]

        if not pending:
            print(f"up to date, {len(applied)} previously applied")
            return 0

        for migration in pending:
            sql = migration.read_text()
            try:
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES ($1)",
                        migration.name,
                    )
            except Exception as exc:
                print(f"Migration failed: {migration.name}\n{exc}", file=sys.stderr)
                return 1
            print(f"Applied {migration.name}")

        print(f"{len(pending)} migrations applied")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
