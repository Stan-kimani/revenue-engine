"""Manually suppress an address or a whole domain (docs/deliverability.md §5:
"Manually added addresses and domains"). Suppressions are append-only — there is
no unsuppress; the send gate reads this table immediately before every send.

Usage:
  uv run python scripts/suppress.py --address someone@example.com --reason manual
  uv run python scripts/suppress.py --domain example.com --reason manual
Env: DATABASE_URL (required).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

import asyncpg
from dotenv import load_dotenv

from revenue_engine.db import repositories as repo
from revenue_engine.db.models import SuppressionReason


async def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--address", help="suppress one email address")
    target.add_argument("--domain", help="suppress every address at this domain")
    parser.add_argument(
        "--reason",
        default=SuppressionReason.MANUAL.value,
        choices=[r.value for r in SuppressionReason],
    )
    parser.add_argument("--source", default=f"human:{getpass.getuser()}")
    args = parser.parse_args(argv)

    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 1

    address = args.address.strip().lower() if args.address else None
    if address is not None and address.count("@") != 1:
        print(f"not an email address: {args.address!r}", file=sys.stderr)
        return 2
    domain = (args.domain or address.rsplit("@", 1)[1]).strip().lower()  # type: ignore[union-attr]

    conn = await asyncpg.connect(database_url)
    try:
        suppression = await repo.insert_suppression(
            conn,
            address=address,
            domain=domain,
            reason=SuppressionReason(args.reason),
            source=args.source,
        )
    finally:
        await conn.close()
    scope = f"address {address}" if address else f"domain {domain} (every address)"
    print(f"Suppressed {scope}: {suppression.reason.value} ({suppression.id})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(sys.argv[1:])))
