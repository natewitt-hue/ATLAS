"""One-time backfill: generate embeddings for conversation_memory rows that have NULL embedding.

Usage:
    python backfill_embeddings.py              # live run
    python backfill_embeddings.py --dry-run    # count rows only, no writes
    python backfill_embeddings.py --limit 500  # cap at 500 rows (default: 1400)

Respects Gemini free-tier embedding quota (1,500 req/day) by defaulting to
1,400 rows per run with a 1-second delay between calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsl_history.db")


async def backfill(*, dry_run: bool = False, limit: int = 1400) -> None:
    async with aiosqlite.connect(DB_PATH, timeout=10) as db:
        db.row_factory = aiosqlite.Row
        # Check table exists (created on first bot startup)
        check = await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_memory'"
        )
        if not await check.fetchone():
            print("conversation_memory table does not exist yet. Start the bot first.")
            return
        cursor = await db.execute(
            "SELECT id, question FROM conversation_memory WHERE embedding IS NULL"
        )
        rows = await cursor.fetchall()

    total = len(rows)
    print(f"Found {total} rows with NULL embedding.")
    if total == 0:
        return
    if dry_run:
        print("Dry run — no changes made.")
        return

    # Import atlas_ai only when we need it (requires env keys)
    import atlas_ai

    capped = rows[:limit]
    print(f"Processing {len(capped)} of {total} rows (limit={limit})...\n")

    success = 0
    failed = 0
    for i, row in enumerate(capped, 1):
        row_id = row["id"]
        question = row["question"]
        try:
            embedding = await atlas_ai.embed_text(question)
        except Exception as e:
            print(f"  [{i}/{len(capped)}] FAILED row id={row_id}: {e}")
            failed += 1
            await asyncio.sleep(1)
            continue

        if embedding is None:
            print(f"  [{i}/{len(capped)}] SKIP row id={row_id}: embed_text returned None")
            failed += 1
            await asyncio.sleep(1)
            continue

        blob = json.dumps(embedding).encode()
        async with aiosqlite.connect(DB_PATH, timeout=10) as db:
            await db.execute(
                "UPDATE conversation_memory SET embedding = ? WHERE id = ?",
                (blob, row_id),
            )
            await db.commit()

        success += 1
        if i % 25 == 0 or i == len(capped):
            print(f"  [{i}/{len(capped)}] Embedded row id={row_id}")

        # Rate-limit: 1 req/sec to stay under 1,500/day Gemini free tier
        await asyncio.sleep(1)

    print(f"\nDone. {success} embedded, {failed} failed, {total - len(capped)} remaining.")


def main():
    parser = argparse.ArgumentParser(description="Backfill conversation_memory embeddings")
    parser.add_argument("--dry-run", action="store_true", help="Count rows only, no writes")
    parser.add_argument("--limit", type=int, default=1400, help="Max rows to process (default: 1400)")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run, limit=args.limit))


if __name__ == "__main__":
    main()
