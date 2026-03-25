"""
flow_audit.py -- ATLAS Flow Economy Reconciliation Engine
=========================================================
Read-only audit module that scans flow_economy.db for anomalies.

Runnable three ways:
  1. Standalone:  python flow_audit.py [--severity HIGH,CRITICAL] [--json]
  2. Admin cmd:   /boss flow audit  (via economy_cog integration)
  3. Daily loop:  @tasks.loop in economy_cog posts if CRITICAL/HIGH found

Every check returns a list of AuditResult findings.
All queries are SELECT-only -- this module never mutates data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
import aiosqlite

from atlas_colors import AtlasColors

log = logging.getLogger("flow_audit")

_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DB = os.getenv("FLOW_DB_PATH", os.path.join(_DIR, "flow_economy.db"))


# =============================================================================
#  DATA CLASSES
# =============================================================================

@dataclass
class AuditResult:
    severity: str           # CRITICAL | HIGH | MEDIUM | LOW
    check_name: str         # e.g. "balance_drift"
    entity_type: str        # e.g. "user", "bet", "parlay"
    entity_id: str | int
    description: str
    suggested_action: str
    data: dict = field(default_factory=dict)


@dataclass
class AuditReport:
    findings: list[AuditResult] = field(default_factory=list)
    ran_at: str = ""
    duration_ms: int = 0
    checks_run: int = 0
    checks_passed: int = 0

    def by_severity(self) -> dict[str, list[AuditResult]]:
        groups: dict[str, list[AuditResult]] = {}
        for f in self.findings:
            groups.setdefault(f.severity, []).append(f)
        return groups

    def has_critical_or_high(self) -> bool:
        return any(f.severity in ("CRITICAL", "HIGH") for f in self.findings)

    def summary_text(self) -> str:
        if not self.findings:
            return f"Flow Audit: ALL CLEAR ({self.checks_run} checks passed in {self.duration_ms}ms)"
        counts = {}
        for f in self.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
        parts = [f"{v} {k}" for k, v in sorted(counts.items())]
        return (
            f"Flow Audit: {len(self.findings)} finding(s) -- {', '.join(parts)} "
            f"({self.checks_run} checks in {self.duration_ms}ms)"
        )

    def to_embed_dict(self) -> dict:
        """Return dict suitable for discord.Embed construction."""
        color = AtlasColors.SUCCESS.value if not self.findings else (
            AtlasColors.ERROR.value if self.has_critical_or_high() else AtlasColors.WARNING.value
        )
        desc_lines = [self.summary_text(), ""]
        by_sev = self.by_severity()
        for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            items = by_sev.get(sev, [])
            if not items:
                continue
            desc_lines.append(f"**{sev}** ({len(items)})")
            for item in items[:5]:
                desc_lines.append(f"  {item.entity_type} `{item.entity_id}`: {item.description}")
            if len(items) > 5:
                desc_lines.append(f"  ... and {len(items) - 5} more")
            desc_lines.append("")
        return {
            "title": "Flow Economy Audit Report",
            "description": "\n".join(desc_lines),
            "color": color,
        }


# =============================================================================
#  AUDITOR
# =============================================================================

class FlowAuditor:
    def __init__(self, db_path: str = _DEFAULT_DB):
        self.db_path = db_path

    async def run_all(self) -> AuditReport:
        """Execute all checks and return a report."""
        start = time.monotonic()
        report = AuditReport()

        checks = [
            self.check_orphaned_bets,
            self.check_error_bets,
            self.check_balance_drift,
            self.check_orphaned_wagers,
            self.check_stuck_predictions,
            self.check_parlay_consistency,
            self.check_negative_balances,
            self.check_missing_wager_entries,
            self.check_jackpot_sanity,
            self.check_transaction_continuity,
        ]

        from datetime import datetime, timezone
        report.ran_at = datetime.now(timezone.utc).isoformat()

        for check_fn in checks:
            try:
                findings = await check_fn()
                report.findings.extend(findings)
                report.checks_run += 1
                if not findings:
                    report.checks_passed += 1
            except Exception as e:
                log.exception(f"Audit check {check_fn.__name__} failed: {e}")
                report.findings.append(AuditResult(
                    severity="HIGH",
                    check_name=check_fn.__name__,
                    entity_type="system",
                    entity_id="check_failure",
                    description=f"Check raised exception: {e}",
                    suggested_action="Investigate -- check may have a schema mismatch",
                ))
                report.checks_run += 1

        report.duration_ms = int((time.monotonic() - start) * 1000)
        return report

    # ── 1. Orphaned Bets ────────────────────────────────────────────────

    async def check_orphaned_bets(self) -> list[AuditResult]:
        """Bets stuck in Pending for >3 days."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Straight bets
            async with db.execute("""
                SELECT bet_id, discord_id, matchup, wager_amount, created_at
                FROM bets_table
                WHERE status = 'Pending'
                  AND parlay_id IS NULL
                  AND created_at < datetime('now', '-3 days')
            """) as cur:
                for row in await cur.fetchall():
                    age_days = await self._age_days(db, row["created_at"])
                    sev = "HIGH" if age_days > 7 else "MEDIUM"
                    results.append(AuditResult(
                        severity=sev,
                        check_name="orphaned_bets",
                        entity_type="bet",
                        entity_id=row["bet_id"],
                        description=f"Pending for {age_days}d: {row['matchup']} (${row['wager_amount']:,})",
                        suggested_action="Check autograde fuzzy match or manually grade",
                        data=dict(row),
                    ))

            # Parlay legs stuck pending on old parlays
            async with db.execute("""
                SELECT pl.parlay_id, pl.leg_index, pl.matchup, pl.status,
                       pt.created_at, pt.discord_id
                FROM parlay_legs pl
                JOIN parlays_table pt ON pt.parlay_id = pl.parlay_id
                WHERE pl.status = 'Pending'
                  AND pt.created_at < datetime('now', '-3 days')
            """) as cur:
                for row in await cur.fetchall():
                    age_days = await self._age_days(db, row["created_at"])
                    sev = "HIGH" if age_days > 7 else "MEDIUM"
                    results.append(AuditResult(
                        severity=sev,
                        check_name="orphaned_bets",
                        entity_type="parlay_leg",
                        entity_id=f"{row['parlay_id']}:{row['leg_index']}",
                        description=f"Parlay leg pending {age_days}d: {row['matchup']}",
                        suggested_action="Check autograde fuzzy match for this leg",
                        data=dict(row),
                    ))
        return results

    # ── 2. Error Bets ───────────────────────────────────────────────────

    async def check_error_bets(self) -> list[AuditResult]:
        """Bets or parlays in Error state -- user money locked."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute(
                "SELECT bet_id, discord_id, wager_amount, odds, matchup, status "
                "FROM bets_table WHERE status = 'Error'"
            ) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="CRITICAL",
                        check_name="error_bets",
                        entity_type="bet",
                        entity_id=row["bet_id"],
                        description=f"Error bet: {row['matchup']} ${row['wager_amount']:,} @ {row['odds']}",
                        suggested_action="Admin resolve: refund wager, cap payout, or void",
                        data=dict(row),
                    ))

            async with db.execute(
                "SELECT parlay_id, discord_id, wager_amount, combined_odds, status "
                "FROM parlays_table WHERE status = 'Error'"
            ) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="CRITICAL",
                        check_name="error_bets",
                        entity_type="parlay",
                        entity_id=row["parlay_id"],
                        description=f"Error parlay: ${row['wager_amount']:,} @ {row['combined_odds']}",
                        suggested_action="Admin resolve: refund wager, cap payout, or void",
                        data=dict(row),
                    ))
        return results

    # ── 3. Balance Drift ────────────────────────────────────────────────

    async def check_balance_drift(self) -> list[AuditResult]:
        """Compare last transaction balance_after vs current user balance."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            async with db.execute("""
                SELECT u.discord_id, u.balance,
                       (SELECT balance_after FROM transactions
                        WHERE discord_id = u.discord_id
                        ORDER BY txn_id DESC LIMIT 1) as last_txn_balance
                FROM users_table u
            """) as cur:
                for row in await cur.fetchall():
                    current = row["balance"]
                    last_txn = row["last_txn_balance"]
                    if last_txn is None:
                        continue  # no transactions yet -- likely new user
                    drift = current - last_txn
                    if drift != 0:
                        results.append(AuditResult(
                            severity="CRITICAL",
                            check_name="balance_drift",
                            entity_type="user",
                            entity_id=row["discord_id"],
                            description=f"Balance ${current:,} != last txn balance ${last_txn:,} (drift: {drift:+,})",
                            suggested_action="Investigate: balance mutated outside wallet layer",
                            data={"balance": current, "last_txn_balance": last_txn, "drift": drift},
                        ))
        return results

    # ── 4. Orphaned Wagers ──────────────────────────────────────────────

    async def check_orphaned_wagers(self) -> list[AuditResult]:
        """Wager registry 'open' entries where the source bet is already settled."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # TSL straight bets: wager open but bet settled
            async with db.execute("""
                SELECT w.wager_id, w.subsystem_id, w.discord_id, w.wager_amount
                FROM wagers w
                WHERE w.subsystem = 'TSL_BET' AND w.status = 'open'
                  AND EXISTS (
                    SELECT 1 FROM bets_table b
                    WHERE CAST(b.bet_id AS TEXT) = w.subsystem_id
                      AND b.status IN ('Won', 'Lost', 'Push', 'Cancelled', 'Error')
                  )
            """) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="HIGH",
                        check_name="orphaned_wagers",
                        entity_type="wager",
                        entity_id=row["wager_id"],
                        description=f"TSL_BET wager open but bet {row['subsystem_id']} is settled",
                        suggested_action="Settle wager registry entry to match source",
                        data=dict(row),
                    ))

            # Parlays: wager open but parlay settled
            async with db.execute("""
                SELECT w.wager_id, w.subsystem_id, w.discord_id, w.wager_amount
                FROM wagers w
                WHERE w.subsystem = 'PARLAY' AND w.status = 'open'
                  AND EXISTS (
                    SELECT 1 FROM parlays_table p
                    WHERE p.parlay_id = w.subsystem_id
                      AND p.status IN ('Won', 'Lost', 'Push', 'Cancelled', 'Error')
                  )
            """) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="HIGH",
                        check_name="orphaned_wagers",
                        entity_type="wager",
                        entity_id=row["wager_id"],
                        description=f"PARLAY wager open but parlay {row['subsystem_id']} is settled",
                        suggested_action="Settle wager registry entry to match source",
                        data=dict(row),
                    ))

            # Casino: open wager older than 1 hour (games resolve in seconds)
            async with db.execute("""
                SELECT w.wager_id, w.subsystem_id, w.discord_id, w.wager_amount
                FROM wagers w
                WHERE w.subsystem = 'CASINO' AND w.status = 'open'
                  AND w.created_at < datetime('now', '-1 hour')
            """) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="MEDIUM",
                        check_name="orphaned_wagers",
                        entity_type="wager",
                        entity_id=row["wager_id"],
                        description=f"Casino wager open >1hr for user {row['discord_id']}",
                        suggested_action="Check if casino session exists; refund if orphaned",
                        data=dict(row),
                    ))
        return results

    # ── 5. Stuck Predictions ────────────────────────────────────────────

    async def check_stuck_predictions(self) -> list[AuditResult]:
        """Open contracts on markets that are already resolved."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Check if prediction tables exist first
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='prediction_contracts'"
            ) as cur:
                if not await cur.fetchone():
                    return results

            async with db.execute("""
                SELECT c.id, c.user_id, c.market_id, c.cost_bucks, c.side,
                       m.status as market_status, m.result, m.resolved_by
                FROM prediction_contracts c
                JOIN prediction_markets m ON m.market_id = c.market_id
                WHERE c.status = 'open'
                  AND m.resolved_by != 'pending'
            """) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="CRITICAL",
                        check_name="stuck_predictions",
                        entity_type="prediction_contract",
                        entity_id=row["id"],
                        description=(
                            f"Open contract ${row['cost_bucks']:,} {row['side']} on "
                            f"resolved market {row['market_id']} (result: {row['result']})"
                        ),
                        suggested_action="Re-run market resolution or manually settle",
                        data=dict(row),
                    ))

            # Also check for stale markets (open contracts, market pending, no sync >30d)
            async with db.execute("""
                SELECT c.market_id, COUNT(*) as open_count,
                       SUM(c.cost_bucks) as total_at_risk,
                       m.last_synced
                FROM prediction_contracts c
                JOIN prediction_markets m ON m.market_id = c.market_id
                WHERE c.status = 'open'
                  AND m.resolved_by = 'pending'
                  AND m.last_synced < datetime('now', '-30 days')
                GROUP BY c.market_id
            """) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="HIGH",
                        check_name="stuck_predictions",
                        entity_type="prediction_market",
                        entity_id=row["market_id"],
                        description=(
                            f"Market stale >30d with {row['open_count']} open contracts "
                            f"(${row['total_at_risk']:,} at risk)"
                        ),
                        suggested_action="Admin: void contracts and refund, or manually resolve",
                        data=dict(row),
                    ))
        return results

    # ── 6. Parlay Consistency ───────────────────────────────────────────

    async def check_parlay_consistency(self) -> list[AuditResult]:
        """Parlays where master status contradicts leg statuses."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Parlays marked settled but still have Pending legs
            async with db.execute("""
                SELECT pt.parlay_id, pt.status, pt.discord_id, pt.wager_amount,
                       COUNT(*) as pending_legs
                FROM parlays_table pt
                JOIN parlay_legs pl ON pl.parlay_id = pt.parlay_id
                WHERE pt.status IN ('Won', 'Lost', 'Push')
                  AND pl.status = 'Pending'
                GROUP BY pt.parlay_id
            """) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="CRITICAL",
                        check_name="parlay_consistency",
                        entity_type="parlay",
                        entity_id=row["parlay_id"],
                        description=(
                            f"Parlay marked '{row['status']}' but has {row['pending_legs']} "
                            f"pending leg(s) (${row['wager_amount']:,})"
                        ),
                        suggested_action="Re-grade parlay legs or correct master status",
                        data=dict(row),
                    ))

            # Parlays still Pending but ALL legs are resolved
            async with db.execute("""
                SELECT pt.parlay_id, pt.status, pt.discord_id, pt.wager_amount
                FROM parlays_table pt
                WHERE pt.status = 'Pending'
                  AND NOT EXISTS (
                    SELECT 1 FROM parlay_legs pl
                    WHERE pl.parlay_id = pt.parlay_id AND pl.status = 'Pending'
                  )
                  AND EXISTS (
                    SELECT 1 FROM parlay_legs pl WHERE pl.parlay_id = pt.parlay_id
                  )
            """) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="CRITICAL",
                        check_name="parlay_consistency",
                        entity_type="parlay",
                        entity_id=row["parlay_id"],
                        description=(
                            f"Parlay still Pending but all legs resolved "
                            f"(${row['wager_amount']:,}, user {row['discord_id']})"
                        ),
                        suggested_action="Run parlay completion check to settle and pay out",
                        data=dict(row),
                    ))
        return results

    # ── 7. Negative Balances ────────────────────────────────────────────

    async def check_negative_balances(self) -> list[AuditResult]:
        """Users with balance < 0 -- should never happen."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT discord_id, balance FROM users_table WHERE balance < 0"
            ) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="CRITICAL",
                        check_name="negative_balances",
                        entity_type="user",
                        entity_id=row["discord_id"],
                        description=f"Negative balance: ${row['balance']:,}",
                        suggested_action="Investigate transaction history; likely a race condition",
                        data=dict(row),
                    ))
        return results

    # ── 8. Missing Wager Entries ────────────────────────────────────────

    async def check_missing_wager_entries(self) -> list[AuditResult]:
        """Transactions with subsystem tags but no matching wager row."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Only check debit transactions (negative amounts = wager placements)
            async with db.execute("""
                SELECT t.txn_id, t.discord_id, t.subsystem, t.subsystem_id, t.amount
                FROM transactions t
                WHERE t.subsystem IS NOT NULL
                  AND t.subsystem_id IS NOT NULL
                  AND t.subsystem IN ('TSL_BET', 'PARLAY', 'CASINO', 'PREDICTION')
                  AND t.amount < 0
                  AND NOT EXISTS (
                    SELECT 1 FROM wagers w
                    WHERE w.subsystem = t.subsystem AND w.subsystem_id = t.subsystem_id
                  )
            """) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="LOW",
                        check_name="missing_wager_entries",
                        entity_type="transaction",
                        entity_id=row["txn_id"],
                        description=(
                            f"Debit txn {row['subsystem']}:{row['subsystem_id']} "
                            f"(${abs(row['amount']):,}) has no wager registry entry"
                        ),
                        suggested_action="Backfill wager registry or investigate gap",
                        data=dict(row),
                    ))
        return results

    # ── 9. Jackpot Sanity ───────────────────────────────────────────────

    async def check_jackpot_sanity(self) -> list[AuditResult]:
        """Jackpot pools should never be negative."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Check if table exists
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='casino_jackpot'"
            ) as cur:
                if not await cur.fetchone():
                    return results

            async with db.execute(
                "SELECT tier, pool, seed FROM casino_jackpot WHERE pool < 0"
            ) as cur:
                for row in await cur.fetchall():
                    results.append(AuditResult(
                        severity="CRITICAL",
                        check_name="jackpot_sanity",
                        entity_type="jackpot",
                        entity_id=row["tier"],
                        description=f"Negative jackpot pool: {row['tier']} = ${row['pool']:,}",
                        suggested_action="Reset pool to seed value",
                        data=dict(row),
                    ))
        return results

    # ── 10. Transaction Continuity ──────────────────────────────────────

    async def check_transaction_continuity(self) -> list[AuditResult]:
        """Check balance_after chain: prev_balance_after + amount = current_balance_after."""
        results = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Find chain breaks: consecutive transactions where math doesn't add up
            async with db.execute("""
                SELECT t1.txn_id, t1.discord_id, t1.amount,
                       t1.balance_after as current_after,
                       t2.balance_after as prev_after,
                       t2.txn_id as prev_txn_id
                FROM transactions t1
                JOIN transactions t2 ON t2.txn_id = (
                    SELECT MAX(txn_id) FROM transactions
                    WHERE discord_id = t1.discord_id AND txn_id < t1.txn_id
                )
                WHERE t1.balance_after != t2.balance_after + t1.amount
                ORDER BY t1.txn_id DESC
                LIMIT 50
            """) as cur:
                for row in await cur.fetchall():
                    expected = row["prev_after"] + row["amount"]
                    actual = row["current_after"]
                    gap = actual - expected
                    results.append(AuditResult(
                        severity="CRITICAL",
                        check_name="transaction_continuity",
                        entity_type="transaction",
                        entity_id=row["txn_id"],
                        description=(
                            f"Chain break at txn {row['txn_id']}: "
                            f"expected ${expected:,} but got ${actual:,} (gap: {gap:+,})"
                        ),
                        suggested_action="Investigate: concurrent mutation or set_balance override",
                        data={
                            "txn_id": row["txn_id"],
                            "prev_txn_id": row["prev_txn_id"],
                            "prev_after": row["prev_after"],
                            "amount": row["amount"],
                            "expected": expected,
                            "actual": actual,
                            "gap": gap,
                        },
                    ))
        return results

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    async def _age_days(db, created_at: str) -> int:
        """Calculate days since created_at."""
        async with db.execute(
            "SELECT CAST(julianday('now') - julianday(?) AS INTEGER)", (created_at,)
        ) as cur:
            row = await cur.fetchone()
        return row[0] if row else 0


# =============================================================================
#  STANDALONE RUNNER
# =============================================================================

async def run_audit_standalone(db_path: str, severity_filter: set[str] | None = None,
                               output_json: bool = False) -> int:
    """Run all checks, print report, return exit code (0=clean, 1=findings)."""
    auditor = FlowAuditor(db_path)
    report = await auditor.run_all()

    if severity_filter:
        report.findings = [f for f in report.findings if f.severity in severity_filter]

    if output_json:
        data = {
            "summary": report.summary_text(),
            "ran_at": report.ran_at,
            "duration_ms": report.duration_ms,
            "checks_run": report.checks_run,
            "checks_passed": report.checks_passed,
            "findings": [asdict(f) for f in report.findings],
        }
        print(json.dumps(data, indent=2))
    else:
        print(report.summary_text())
        print()
        by_sev = report.by_severity()
        for sev in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            items = by_sev.get(sev, [])
            if not items:
                continue
            print(f"── {sev} ({len(items)}) ──")
            for item in items:
                print(f"  [{item.check_name}] {item.entity_type} {item.entity_id}: {item.description}")
                print(f"    Action: {item.suggested_action}")
            print()

    return 1 if report.findings else 0


def main():
    parser = argparse.ArgumentParser(description="ATLAS Flow Economy Audit")
    parser.add_argument("--db-path", default=_DEFAULT_DB, help="Path to flow_economy.db")
    parser.add_argument("--severity", default=None,
                        help="Comma-separated severity filter (e.g. HIGH,CRITICAL)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    sev_filter = set(args.severity.upper().split(",")) if args.severity else None
    exit_code = asyncio.run(run_audit_standalone(args.db_path, sev_filter, args.json))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
