"""
PATCH 3 — sportsbook.py — Fix Sync/Async SQLite Conflicts
==========================================================

Multiple edits required. Apply in order.

KEY CHANGES:
  1. WAL journal mode — allows concurrent reads from casino's aiosqlite
  2. 10-second timeout on all connections — prevents instant lock errors
  3. run_in_executor wrapping for heavy sync functions
  4. Add missing "import asyncio"
"""


# =========================================================================
# EDIT 3A — Add asyncio import at top of sportsbook.py
#
#   FIND:    import sqlite3
#   REPLACE: import asyncio\nimport sqlite3
# =========================================================================


# =========================================================================
# EDIT 3B — Add _db_con() helper BEFORE setup_db().
#
#   Add this new function right above def setup_db():
# =========================================================================

# --- ADD THIS (new code) ---
_DB_TIMEOUT = 10

def _db_con():
    """Connection factory: WAL mode + timeout prevents lock fights with casino aiosqlite."""
    con = sqlite3.connect(DB_PATH, timeout=_DB_TIMEOUT)
    con.execute("PRAGMA journal_mode=WAL")
    return con


# =========================================================================
# EDIT 3C — GLOBAL Find-and-Replace across ALL of sportsbook.py:
#
#   In your editor, do Ctrl+H:
#     Find:        sqlite3.connect(DB_PATH)
#     Replace All: _db_con()
#
#   This hits every DB access in the file (~20 locations).
#   WAL + timeout will apply everywhere automatically.
# =========================================================================


# =========================================================================
# EDIT 3D — Wrap _build_game_lines call in run_in_executor.
#
#   FIND (inside the /lines command handler):
#
#       ui_games = _build_game_lines(raw_games)
#
#   REPLACE with:
#
#       loop = asyncio.get_running_loop()
#       ui_games = await loop.run_in_executor(None, _build_game_lines, raw_games)
# =========================================================================


# =========================================================================
# EDIT 3E — Replace _run_autograde with executor-wrapped version.
#
# The function is async but does ALL its DB work synchronously.
# Fix: extract sync DB work into a nested function, run in executor,
# then handle async Discord messaging after.
#
# FIND the entire function starting with:
#     async def _run_autograde(bot) -> None:
# ending just before:
#     class BetSlipModal
#
# REPLACE with the code below.
# (The parlay grading logic is preserved exactly as-is from your v3.2)
# =========================================================================

# --- NEW _run_autograde (complete replacement) ---

async def _run_autograde(bot):
    """
    Core autograde logic. Sync DB work pushed to executor to avoid
    blocking the event loop and fighting casino_db for locks.
    """

    def _grade_sync():
        """All DB reads/writes. Returns list of result dicts for Discord messaging."""
        results = []
        try:
            with _db_con() as con:
                ungraded = con.execute(
                    "SELECT DISTINCT week FROM bets_table "
                    "WHERE status NOT IN ('Won','Lost','Push') AND week <= ?",
                    (dm.CURRENT_WEEK,)
                ).fetchall()

            if not ungraded:
                return results

            for (week,) in ungraded:
                scores = _build_score_lookup(week)
                real_games = len([k for k in scores if k != "__fuzzy__"])
                if real_games == 0:
                    continue

                settled = wins = losses = pushes = 0
                total_paid = 0

                with _db_con() as con:
                    # Straight bets
                    pending = con.execute(
                        "SELECT bet_id, discord_id, matchup, bet_type, wager_amount, odds, pick, line "
                        "FROM bets_table WHERE week=? AND status NOT IN ('Won','Lost','Push')",
                        (week,)
                    ).fetchall()

                    for b in pending:
                        bid, uid, matchup, btype, amt, odds, pick, line = b
                        gd = _fuzzy_match(matchup.lower().strip(), scores)
                        if not gd:
                            continue
                        try:
                            line_val = float(line)
                        except (ValueError, TypeError):
                            line_val = 0.0
                        res = _grade_single_bet(btype, pick, line_val,
                                                gd["home"], gd["away"],
                                                gd["home_score"], gd["away_score"])
                        if res == "Pending":
                            continue
                        if res == "Won":
                            payout = _payout_calc(amt, int(odds))
                            _update_balance(uid, payout, con)
                            total_paid += payout - amt
                            wins += 1
                        elif res == "Push":
                            _update_balance(uid, amt, con)
                            pushes += 1
                        elif res == "Lost":
                            losses += 1
                        con.execute("UPDATE bets_table SET status=? WHERE bet_id=?", (res, bid))
                        settled += 1

                    # Parlays
                    parlays = con.execute(
                        "SELECT parlay_id, discord_id, legs, combined_odds, wager_amount "
                        "FROM parlays_table WHERE week=? AND status='Pending'",
                        (week,)
                    ).fetchall()

                    for pid, uid, legs_json, c_odds, amt in parlays:
                        try:
                            legs = json.loads(legs_json) if isinstance(legs_json, (str, bytes)) else legs_json
                            if not isinstance(legs, list) or not legs:
                                con.execute("UPDATE parlays_table SET status='Lost' WHERE parlay_id=?", (pid,))
                                continue
                        except Exception:
                            continue

                        all_won    = True
                        any_lost   = False
                        unresolved = 0

                        for leg in legs:
                            gd = _fuzzy_match(leg["matchup"].lower().strip(), scores)
                            if not gd:
                                unresolved += 1
                                all_won = False
                                continue
                            res = _grade_single_bet(
                                leg["bet_type"], leg["pick"], float(leg.get("line", 0)),
                                gd["home"], gd["away"], gd["home_score"], gd["away_score"]
                            )
                            if res == "Lost":
                                all_won  = False
                                any_lost = True
                                break
                            if res == "Pending":
                                all_won    = False
                                unresolved += 1
                            elif res != "Won":
                                all_won = False

                        if unresolved > 0 and not any_lost:
                            continue  # retry next cycle

                        if all_won:
                            payout = _payout_calc(amt, c_odds)
                            _update_balance(uid, payout, con)
                            total_paid += payout - amt
                            con.execute("UPDATE parlays_table SET status='Won' WHERE parlay_id=?", (pid,))
                            wins += 1
                        elif any_lost:
                            con.execute("UPDATE parlays_table SET status='Lost' WHERE parlay_id=?", (pid,))
                            losses += 1
                        else:
                            _update_balance(uid, amt, con)
                            con.execute("UPDATE parlays_table SET status='Push' WHERE parlay_id=?", (pid,))
                            pushes += 1

                if settled > 0 or wins + losses + pushes > 0:
                    results.append({
                        "week": week, "settled": settled,
                        "wins": wins, "losses": losses, "pushes": pushes,
                        "total_paid": total_paid,
                    })
        except Exception as e:
            print(f"[AUTO-GRADE] Error: {e}")

        return results

    # Run sync grading in executor
    loop = asyncio.get_running_loop()
    results = await loop.run_in_executor(None, _grade_sync)

    # Send Discord notifications (must be on the event loop)
    for r in results:
        print(
            f"[AUTO-GRADE] Week {r['week']} — Settled {r['settled']} | "
            f"W{r['wins']} L{r['losses']} P{r['pushes']} | Paid ${r['total_paid']:,}"
        )
        channel = discord.utils.get(bot.get_all_channels(), name="sportsbook")
        if channel:
            try:
                embed = discord.Embed(
                    title=f"✅ Week {r['week']} Bets Auto-Graded",
                    color=TSL_GOLD
                )
                embed.add_field(name="Settled",      value=str(r["settled"]),        inline=True)
                embed.add_field(name="✅ Won",       value=str(r["wins"]),           inline=True)
                embed.add_field(name="❌ Lost",      value=str(r["losses"]),         inline=True)
                embed.add_field(name="🔁 Push",     value=str(r["pushes"]),         inline=True)
                embed.add_field(name="💸 Paid Out", value=f"${r['total_paid']:,}",  inline=True)
                embed.set_footer(text="TSL Sportsbook • Auto-graded")
                await channel.send(embed=embed)
            except Exception:
                pass
