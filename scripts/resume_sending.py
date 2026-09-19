"""Resume sending after a docs/deliverability.md §6 hard pause — a manual action
with a recorded reason — and re-enqueue approved drafts the pause held back.

With no open pause, it only re-enqueues held drafts (e.g. after fixing
DEV_SANDBOX_EMAIL or the physical address). Every re-enqueued send still passes
every gate again at send time.

Usage:
  uv run python scripts/resume_sending.py --by stan --reason "list cleaned, bad rows removed"
Env: DATABASE_URL (required).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import asyncpg
from dotenv import load_dotenv

from revenue_engine.core.sending import resume_sending


async def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--by", required=True, help="who is resuming (recorded)")
    parser.add_argument("--reason", required=True, help="why it is safe to resume (recorded)")
    args = parser.parse_args(argv)
    if not args.by.strip() or not args.reason.strip():
        print("--by and --reason must not be blank.", file=sys.stderr)
        return 2

    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    conn = await asyncpg.connect(database_url)
    try:
        result = await resume_sending(
            conn, resumed_by=f"human:{args.by.strip()}", reason=args.reason.strip()
        )
    finally:
        await conn.close()
    if result.pause_id:
        print(f"Resumed pause {result.pause_id}.")
    else:
        print("No open pause.")
    print(f"Re-enqueued {len(result.requeued_message_ids)} held draft(s).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
