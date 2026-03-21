# ATLAS Full Codebase Audit — 2026-03-15

> **Scope:** 67 files, ~40K lines of Python
> **Reviewed by:** 6 parallel code review agents
> **Severity levels:** P0 (Bug/Crash), P1 (Security/Data), P2 (Logic Error), P3 (Code Quality), P4 (Improvement)

---

## Executive Summary

| Severity | Count |
|----------|-------|
| P0 — Bug/Crash | 10 |
| P1 — Security/Data | 13 |
| P2 — Logic Error | 65 |
| P3 — Code Quality | 63 |
| P4 — Improvement | 6 |
| **TOTAL** | **157** |

### Top 5 Most Critical Issues

1. **`data_manager.py:878` (P0)** — H2H record filter uses `status == 3` only, dropping all status-2 completed games. Directly violates CLAUDE.md's `status IN (2,3)` rule.
2. **`data_manager.py:270` (P0)** — Startup makes ~155 sequential HTTP calls to rebuild rings cache, freezing the bot for minutes.
3. **`flow_sportsbook.py:1066–1200` (P0)** — Race condition in bet placement: balance check and debit are non-atomic across BetSlipModal, ParlayWagerModal, and PropBetModal — allows double-spend.
4. **`roster.py:133` (P0)** — Synchronous SQLite calls on the async event loop from Discord UI callbacks, blocking the entire bot.
5. **`card_renderer.py:107` (P0)** — Playwright subprocess resource leak: `close_browser()` calls `__aexit__` on the wrong object, so the Playwright process never terminates.

---

## Findings by Module

### Core Engine

**Files:** `bot.py`, `data_manager.py`, `reasoning.py`, `analysis.py`, `intelligence.py`, `permissions.py`, `constants.py`

| # | File:Line | Sev | Description | Suggested Fix |
|---|-----------|-----|-------------|---------------|
| 1 | `data_manager.py:878` | P0 | `get_h2h_record()` filters to `status == 3` only, dropping status-2 completed games. | Change to `if status not in (2, 3): continue` |
| 2 | `data_manager.py:270–311` | P0 | `_rebuild_rings_cache()` makes ~155 sequential HTTP calls at startup, freezing the bot. | Query `tsl_history.db` championship records directly, or cache to disk across restarts. |
| 3 | `data_manager.py:303` | P1 | Rings cache uses `wins >= 14` as SB proxy. 14-win losers get rings; 12-win champions don't. | Query actual championship data from `tsl_history.db`. |
| 4 | `reasoning.py:448` | P1 | `_call_analyst()` calls synchronous Gemini API directly, blocking the event loop (2–8s). Same at lines 768 and 965. | Wrap in `loop.run_in_executor(None, lambda: ...)` |
| 5 | `reasoning.py:350` | P1 | `build_exec_env()` executes `PREBUILT_METRICS_CODE` with unrestricted builtins. | Document trust boundary explicitly. |
| 6 | `data_manager.py:441–445` | P2 | Off-by-one in weekly scores fetch — fetches one `weekIndex` past current week. | Change `range(0, _l_week + 1)` to `range(0, _l_week)`. |
| 7 | `data_manager.py:516` | P2 | `.get()` on DataFrame returns `None` silently, converting entire column to NaN. | Check `if "seasonIndex" in _l_df_def_full.columns:` first. |
| 8 | `data_manager.py:693` | P2 | `find_trades_by_player()` sorts by string comparison — `"9" > "10"` lexicographically. | Sort with `key=lambda t: (int(t["seasonIndex"] or 0), int(t["weekIndex"] or 0))`. |
| 9 | `bot.py:414–422` | P2 | `_bot_start_time` referenced via `global` before its module-level declaration. | Move declaration to module-level globals section near top. |
| 10 | `bot.py:666` | P2 | `if players and abilities:` — empty list is falsy, silently falls through to redundant API re-fetch. | Use `if players is not None and abilities is not None:`. |
| 11 | `bot.py:439–450` | P2 | `discover_guild_members()` called synchronously on event loop, blocking with SQLite writes. | Wrap in `await loop.run_in_executor(...)`. |
| 12 | `reasoning.py:559` | P2 | Retry temperatures 0.05/0.10 are too close to zero — retries generate near-identical output. | Use `min(0.1 + 0.15 * attempt, 0.7)`. |
| 13 | `reasoning.py:680` | P2 | `_SELECT_PATTERN` regex with `DOTALL` doesn't prevent multi-statement injection alone; relies on undocumented `_BANNED_SQL_KEYWORDS` dependency. | Document the dependency; optionally add semicolon-in-body check. |
| 14 | `reasoning.py:812` | P2 | Dead condition: `not detail.upper().startswith("ONLY")` is always True when `is_safe` is True. | Simplify to `if is_safe and detail != sql:`. |
| 15 | `intelligence.py:485` | P2 | Clutch records fall back to `df_games` (current week only) — meaningless statistics. | Return early error if `df_all_games` is empty. |
| 16 | `intelligence.py:161–168` | P2 | `sqlite3.connect()` called synchronously from cog commands, blocking event loop. | Wrap in `run_in_executor`. |
| 17 | `analysis.py:360` | P2 | `recent_trades()` doesn't sort by date before `.head(n)`. | Add `.sort_values(["seasonIndex", "weekIndex"], ascending=False)`. |
| 18 | `bot.py:272` | P3 | Bare `except Exception: pass` in error handler swallows all followup errors. | Catch only `discord.NotFound`; re-log others. |
| 19 | `data_manager.py:211` | P3 | `_fetch_csv()` uses `print()` instead of `log`. | Change to `log.debug()`. |
| 20 | `analysis.py:285–295` | P3 | Duplicate power rankings formula — weights have diverged from `reasoning.py` copy. | Extract canonical implementation; share between both. |
| 21 | `intelligence.py:303–304` | P3 | Manual `sum()/len()` despite numpy being imported. | Use `np.mean()`. |
| 22 | `intelligence.py:820–852` | P3 | Pagination dict pruned only on registration, not on access — stale entries accumulate. | Call `_prune_stale_pages()` in `get_pagination()` too. |
| 23 | `permissions.py:77–78` | P3 | `is_tsl_owner()` returns `True` in DMs — any user passes TSL Owner checks via DM. | In DM path, restrict to `ADMIN_USER_IDS`. |
| 24 | `constants.py:5` | P3 | `ATLAS_ICON_URL` uses expiring signed Discord CDN URL. | Host on permanent URL. |

### Cogs Tier 1 (Oracle, Sentinel, Genesis)

**Files:** `oracle_cog.py`, `sentinel_cog.py`, `genesis_cog.py`

| # | File:Line | Sev | Description | Suggested Fix |
|---|-----------|-----|-------------|---------------|
| 25 | `sentinel_cog.py:622` | P0 | Module-level `genai.Client(api_key=os.getenv(...))` with no null guard — crashes if env var missing. | Lazy-initialize with a `_get_sentinel_gemini()` helper. |
| 26 | `sentinel_cog.py:622` | P2 | Module-level `_gemini` client is dead code — never referenced. `_analyze_screenshot_sync` creates its own client. | Remove after fixing #25; route through shared lazy client. |
| 27 | `oracle_cog.py:3651–3655` | P2 | `/stats hotcold` slash command drops `player_names` drill-down — hub button path works correctly. | Mirror hub logic: pass `player_names` to `PlayerDrillView`. |
| 28 | `genesis_cog.py:1608–1616` | P2 | `breakdown_btn` reads `players_a_raw` (empty for picker-mode trades) — produces zero-value breakdown. | Fall back to `players_a_data`/`players_b_data` like `_update_status` does. |
| 29 | `oracle_cog.py`, `genesis_cog.py:369`, `sentinel_cog.py:646` | P3 | Hardcoded persona text instead of `get_persona()` — violates CLAUDE.md. | Import and use `get_persona(context)`. |
| 30 | `oracle_cog.py:3249,3220,3274` | P3 | Missing `options=[]` on `@discord.ui.select` decorators — will TypeError. | Add placeholder `options=[...]` to each decorator. |
| 31 | `oracle_cog.py:3268–3272` | P3 | `DraftSeasonView` generates 95+ select options — Discord caps at 25. | Truncate: `options[-25:]`. |
| 32 | `genesis_cog.py:373–375` | P3 | `''.join(breakdown_a[:15])` — no separator creates unreadable wall of text for Gemini. | Use `'\n'.join(...)`. |
| 33 | `genesis_cog.py:880–903` | P3 | Dual `TradeActionView` instances (ephemeral + log channel) — can double-approve a trade. | Add status guard at top of `_update_status`, or send log channel message display-only. |
| 34 | `sentinel_cog.py:1063–1068` | P3 | `_request_counter` resets on restart — force request IDs collide within the same day. | Persist counter to file/DB, or use timestamp-based IDs. |
| 35 | `sentinel_cog.py:49,2329` | P3 | Mixing sync `requests` and async `httpx` in same file for same pattern. | Unify on async `httpx`. |
| 36 | `sentinel_cog.py:1299–1307` | P4 | Circular import from `genesis_cog` for parity state — broken fallback writes truncated schema. | Extract shared state into standalone `parity_state.py`. |
| 37 | `oracle_cog.py:3746` | P4 | Hardcoded expiring Discord CDN URL for ATLAS logo. | Use `ATLAS_ICON_URL` from constants. |

### Cogs Tier 2

**Files:** `flow_sportsbook.py`, `boss_cog.py`, `polymarket_cog.py`, `codex_cog.py`, `economy_cog.py`, `commish_cog.py`, `echo_cog.py`, `awards_cog.py`, `real_sportsbook_cog.py`

| # | File:Line | Sev | Description | Suggested Fix |
|---|-----------|-----|-------------|---------------|
| 38 | `flow_sportsbook.py:1066–1083` | P0 | Race condition: balance check and debit non-atomic in `BetSlipModal`. | Use atomic `flow_wallet.debit()`. |
| 39 | `flow_sportsbook.py:1120–1137` | P0 | Same race condition in `ParlayWagerModal`. | Same fix. |
| 40 | `flow_sportsbook.py:1178–1200` | P0 | Same race condition in `PropBetModal` — 3 separate DB ops with no transaction. | Wrap in `BEGIN IMMEDIATE` or use `flow_wallet.debit()`. |
| 41 | `flow_sportsbook.py:874–999` | P0 | `_grade_sync` uses sync SQLite while async path writes concurrently — `database is locked` risk. | Migrate to `aiosqlite` or serialize all writes. |
| 42 | `flow_sportsbook.py:293–300` | P1 | SQL injection vector: column name f-string interpolated in `_set_line_override`. | Replace with fixed `if/elif` dispatch. |
| 43 | `flow_sportsbook.py:303–308` | P1 | `_clear_line_overrides_for_week(week)` ignores `week` param — deletes ALL overrides. | Scope `DELETE` to the week's game IDs. |
| 44 | `codex_cog.py:777–793` | P1 | SQL injection: `u1`/`u2` f-string interpolated in `_h2h_impl`. | Use parameterized query. |
| 45 | `codex_cog.py:856–862` | P1 | SQL injection pattern: `season` interpolated as f-string. | Use parameterized query. |
| 46 | `real_sportsbook_cog.py:352–401` | P0 | Double-payout risk: concurrent grade runs can pay same bets twice. | Enforce `reference_key` UNIQUE constraint + `BEGIN IMMEDIATE`. |
| 47 | `real_sportsbook_cog.py:415–419` | P2 | Moneyline doesn't handle ties — tied games counted as losses instead of pushes. | Add `if home_score == away_score: return "Push"`. |
| 48 | `real_sportsbook_cog.py:510–545` | P2 | `void_impl` can partially refund — no transaction wrapping. | Wrap entire void in single `BEGIN IMMEDIATE` transaction. |
| 49 | `real_sportsbook_cog.py:332–346` | P3 | Opens new DB connection per game in `_sync_scores` loop. | Hoist connection outside the loop. |
| 50 | `codex_cog.py:337` | P2 | `DB_SCHEMA` captured at import time — `CURRENT_SEASON` may be stale/default. | Call `_build_schema()` lazily per invocation. |
| 51 | `codex_cog.py:170–171` | P3 | Memory trimming triggers at >10 but trims to 5 — inconsistent buffer. | Align trigger and trim targets. |
| 52 | `codex_cog.py:425–428` | P3 | `get_db()`/`run_sql()` use sync sqlite3 with no WAL or timeout. | Add `PRAGMA journal_mode=WAL` and `timeout=5`. |
| 53 | `flow_sportsbook.py:676–678` | P2 | `_combine_parlay_odds` will `ZeroDivisionError` if any leg has odds of 0. | Guard: `if o == 0: o = -110`. |
| 54 | `flow_sportsbook.py:315–316` | P3 | Elo cache never auto-refreshes mid-session — stale odds after DB sync. | Ensure `_invalidate_elo_cache()` fires on every data refresh. |
| 55 | `flow_sportsbook.py:748–752` | P3 | `print()` emits 16 lines per hub load per user. | Replace with `log.debug()`. |
| 56 | `economy_cog.py:97–112` | P2 | `admin_give` passes `con=db` but doesn't verify `flow_wallet` honors it. | Audit and assert `flow_wallet` uses the passed connection. |
| 57 | `economy_cog.py:309–311` | P2 | Stipends use `guilds[0]` — wrong guild if bot is ever in >1 guild. | Use configured guild ID. |
| 58 | `economy_cog.py:674–679` | P2 | Hardcoded `transactions` table name; `COALESCE` masks missing-table errors. | Use constant from `flow_wallet` or surface errors. |
| 59 | `economy_cog.py:294–346` | P3 | Stipend silently re-fires forever for departed members. | Log warning; optionally auto-deactivate. |
| 60 | `commish_cog.py:477` | P2 | `self._get("GenesisCog")` — likely wrong cog name (real names: TradeCenterCog, ParityCog, GenesisHubCog). | Verify and correct. |
| 61 | `commish_cog.py:500–503` | P2 | `self._get("SentinelCog")` — likely wrong name (real: SentinelHubCog). | Verify and correct. |
| 62 | `commish_cog.py:47` | P3 | `bot.tree.add_command` in `__init__` risks `CommandAlreadyRegistered` on reload. | Move to `cog_load` lifecycle hook. |
| 63 | `echo_cog.py:122` | P2 | `from bot import atlas_group` creates circular import. | Pass as parameter or register commands in `bot.py`. |
| 64 | `echo_cog.py:28–29` | P3 | `self._admin_ids` stored but never checked — impl methods unguarded. | Add permission check or remove unused attribute. |
| 65 | `awards_cog.py:77–78` | P2 | `interaction.channel.send()` with no `None` guard — crashes in DMs. | Add `if not interaction.channel:` guard. |
| 66 | `awards_cog.py:58–61` | P3 | `VoteView` not re-registered on restart — old poll buttons silently die. | Use stable `custom_id` + `bot.add_view()` on startup. |
| 67 | `boss_cog.py:192–247` | P3 | Hub views timeout with no `on_timeout` handler — silent death. | Add `on_timeout()` that edits message. |
| 68 | `boss_cog.py:100–112` | P4 | `import roster` inside function body on every call. | Move to module-level with try/except guard. |
| 69 | `polymarket_cog.py:39–54` | P3 | Bare `except Exception` swallows Gemini init errors with no log. | Add `log.warning(...)`. |
| 70 | `polymarket_cog.py:40` | P4 | Module-level Gemini client leaks on extension reload. | Move to class attribute; close in `cog_unload`. |

### Identity + DB

**Files:** `build_member_db.py`, `build_tsl_db.py`, `player_picker.py`, `roster.py`, `trade_engine.py`, `ability_engine.py`

| # | File:Line | Sev | Description | Suggested Fix |
|---|-----------|-----|-------------|---------------|
| 71 | `build_member_db.py:1074–1140` | P0 | `build_member_table()` blocks with concurrent sync SQLite writes — no lock. | Add `asyncio.Lock()` or convert to `aiosqlite`. |
| 72 | `build_tsl_db.py:295–344` | P0 | Temp file swap without `PRAGMA synchronous = FULL` — crash risk. | Set pragma before final commit. |
| 73 | `roster.py:133,229,265` | P0 | Sync `sqlite3.connect()` on async event loop from UI callbacks. | Wrap in `run_in_executor`. |
| 74 | `trade_engine.py:122` | P2 | `_meta_cap_bonus()` reads `overallRating` but export uses `playerBestOvr` — always 0. | Use `player.get("overallRating") or player.get("playerBestOvr") or 0`. |
| 75 | `trade_engine.py:334` | P2 | Relative path `"parity_state.json"` — breaks if CWD isn't project root. | Use `os.path.dirname(os.path.abspath(__file__))`. |
| 76 | `ability_engine.py:557` | P2 | `weight` not converted with `_safe_int()` — string vs int comparison `TypeError`. | Use `_safe_int(player.get("weight", 0))`. |
| 77 | `ability_engine.py:966–967` | P2 | `audit_roster()` doesn't filter `"nan"` strings like `reassign_roster()` does. | Add `and ab != "nan"` to equipped filter. |
| 78 | `build_tsl_db.py:239,245` | P2 | `MIN(seasonIndex)` on TEXT column — lexicographic sort gives wrong results. | Use `MIN(CAST(seasonIndex AS INTEGER))`. |
| 79 | `build_tsl_db.py:182–194` | P2 | `owner_tenure` counts unplayed games (no status filter). | Add `AND status IN ('2','3')`. |
| 80 | `build_member_db.py:1266–1291` | P2 | Alias map never adds `db_username → db_username` — exact canonical lookups fail. | Add `alias_map[db_u.lower()] = db_u` unconditionally. |
| 81 | `build_member_db.py:13` | P2 | Docstring references `sync_members` — function is actually `build_member_table()`. | Update docstring. |
| 82 | `ability_engine.py:330` | P2 | `"Inside Stuff"` archetype `"Power/Run Stopper"` — `"Power"` never matches `calculate_true_archetype()` output. | Change to `"Power Rusher/Run Stopper"`. |
| 83 | `roster.py:123–165` | P2 | `_by_team.clear()` before repopulation — concurrent lookups return `None`. | Build new dicts atomically then swap. |
| 84 | `roster.py:361` | P2 | Back button passes `view=` without `embed=` — Discord strips the embed. | Store and pass original embed. |
| 85 | `build_member_db.py:1121–1122` | P2 | Upsert on `discord_username` — members with `discord_id=None` can create duplicates. | Add secondary `ON CONFLICT(discord_id)` branch. |
| 86 | `build_member_db.py:1183–1186` | P2 | `sync_db_usernames_from_teams()` doesn't check if username is already claimed. | Check for existing active member before writing. |
| 87 | `build_member_db.py:1355–1400` | P2 | `discover_guild_members()` increments `updated` unconditionally — always equals `known`. | Only increment when fields actually change. |
| 88 | `player_picker.py:178` | P2 | `id(p)` fallback for select value — invalidated on sync. | Use stable hash of player attributes. |
| 89 | `build_tsl_db.py:200–203` | P2 | Name-based player-draft join fails for abbreviated names — falls back to current team. | Use `rosterId` join if available. |
| 90 | `build_member_db.py:95/821` | P2 | `db_username = "Keem_50kFG"` but CLAUDE.md says `Keem=KEEM`. | Verify against `games` table and correct. |
| 91 | `build_member_db.py:170–173` | P2 | Chok's `db_username: "Chokolate_Thunda"` vs CLAUDE.md `ChokolateThunda`. | Verify against `games` table and correct. |
| 92 | `trade_engine.py:143–146` | P2 | `_bundling_penalty()` never penalizes the first same-position player. | Clarify design intent and adjust if needed. |
| 93 | `player_picker.py:386–390` | P3 | `on_timeout()` disables items but never edits Discord message. | Add `await self.message.edit(view=self)`. |
| 94 | `roster.py:311–330` | P3 | `build_team_options()` has no 25-option cap for conference filter. | Add `[:25]` truncation. |
| 95 | `ability_engine.py:625–648` | P3 | Unknown ability tiers silently evade budget validation. | Add unknown-tier handling/logging. |
| 96 | `ability_engine.py:53–56` | P3 | `__or` suffix documented but never implemented. | Remove docs or implement. |
| 97 | `ability_engine.py:1052–1059` | P3 | `has_changes=True` set even when all violations are unresolved. | Set `has_changes = bool(swaps)`. |
| 98 | `ability_engine.py:428` | P4 | `import math` in middle of file instead of top. | Move to top with other imports. |

### Casino Subsystem

**Files:** `casino/casino.py`, `casino/casino_db.py`, `casino/games/*.py`, `casino/renderer/*.py`

| # | File:Line | Sev | Description | Suggested Fix |
|---|-----------|-----|-------------|---------------|
| 99 | `casino/casino_db.py:248–283` | P1 | `process_wager` delegates to `flow_wallet.credit(con=db)` — if wallet ignores `con`, credit is outside write lock. | Verify/assert wallet uses passed connection. |
| 100 | `casino/casino_db.py:352–409` | P1 | TOCTOU on daily scratch: check before `BEGIN IMMEDIATE` — double-credit possible. | Re-read `last_claim` inside the transaction. |
| 101 | `casino/games/blackjack.py:524–559` | P1 | Session creation TOCTOU: two concurrent starts can both pass `uid in active_sessions` check. | Set `active_sessions[uid] = "PENDING"` sentinel before first `await`. |
| 102 | `casino/games/crash.py:239–260` | P1 | Double-cashout: `cashed_out` flag set after `await` — two rapid clicks both credit. | Set `player.cashed_out = True` before any `await`. |
| 103 | `casino/games/crash.py:393–456` | P2 | New round creation race: two users can both create rounds — orphaned background task. | Set sentinel before first `await`. |
| 104 | `casino/casino.py:389–403` | P2 | `_casino_clear_session_impl` doesn't stop the `BlackjackView` — buttons remain live after refund. | Call `session.view.stop()` before popping. |
| 105 | `casino/games/slots.py:153` | P2 | 2-match (net loss) logged as `outcome="win"` — inflates win stats. | Use `"push"` when `0 < payout < wager`. |
| 106 | `casino/games/coinflip.py:139–157` | P2 | Accept button TOCTOU: `resolved` checked before `await deduct_wager` — double-debit. | Set `self.resolved = True` immediately after check. |
| 107 | `casino/games/coinflip.py:162–174` | P2 | PvP: displayed coin side is independent of winner — visually misleading. | Derive winner from coin flip result. |
| 108 | `casino/games/blackjack.py:147` | P2 | `wager_override or self.wager` — explicit zero treated as "not provided". | Use `wager_override if wager_override is not None else self.wager`. |
| 109 | `casino/casino_db.py:583–634` | P2 | `resolve_challenge` assumes symmetric wagers — breaks if asymmetric ever introduced. | Accept separate wager params or assert equality. |
| 110 | `casino/renderer/prediction_card_renderer.py:340–352` | P3 | `Image.open()` per market card never closed — buffer leak. | Use `with Image.open(...) as card_img:`. |
| 111 | `casino/renderer/ledger_renderer.py:231` | P3 | `datetime.now()` without timezone — inconsistent with UTC used elsewhere. | Use `datetime.now(timezone.utc)`. |
| 112 | `casino/casino_db.py:416–445` | P3 | "Provably fair" seed uses predictable Mersenne Twister. | Use `secrets.token_urlsafe(16)`. |
| 113 | `casino/games/crash.py:235` | P3 | `CrashView` with `timeout=None` — leaks if `_run_round` raises. | Wrap in `try/finally: view.stop()`. |
| 114 | `casino/games/slots.py:135–149` | P3 | PIL render called synchronously on event loop between `asyncio.sleep`. | Wrap in `run_in_executor`. |
| 115 | `casino/games/blackjack.py:33–34` | P3 | `if TYPE_CHECKING: pass` — dead code. | Remove. |
| 116 | `casino/renderer/card_renderer.py:113` | P3 | `_build_shoe()` called just for length — allocates 312-card list then discards. | Use constant `_FULL_SHOE_SIZE = 312`. |
| 117 | `casino/casino.py:163–164` | P3 | `CasinoHubView` 120s timeout with no `on_timeout` handler. | Add handler to disable buttons. |
| 118 | `casino/renderer/card_renderer.py:259–264` | P4 | Felt gradient rendered pixel-by-pixel in Python loop. | Use numpy or pre-render/cache. |

### UI + Rendering + Cortex

**Files:** `atlas_card_renderer.py`, `card_renderer.py`, `flow_cards.py`, `sportsbook_cards.py`, `hub_view.py`, `pagination_view.py`, `ui_state.py`, `setup_cog.py`, `cortex/`, `echo_loader.py`, `affinity.py`, `flow_wallet.py`, `google_docs_writer.py`, `odds_api_client.py`, `lore_rag.py`

| # | File:Line | Sev | Description | Suggested Fix |
|---|-----------|-----|-------------|---------------|
| 119 | `card_renderer.py:107–114` | P0 | Playwright resource leak: `close_browser()` calls `__aexit__` on wrong object — subprocess never terminates. | Store `pw` separately from the context manager. |
| 120 | `cortex_main.py:62` | P0 | `_clear_cache()` uses relative path `".cortex_cache"` — clears wrong directory if CWD differs. | Use `os.path.join(os.path.dirname(__file__), ".cortex_cache")`. |
| 121 | `card_renderer.py:285,808` | P1 | HTML loads external fonts — Playwright blocks on `networkidle` if CDN is slow/down. | Download fonts locally; embed as base64 data URIs. |
| 122 | `cortex_engine.py:176–186` | P1 | SQL built with f-string interpolation of `ai_clause` — safe today but fragile pattern. | Use fixed clause or document that input must never be user-controlled. |
| 123 | `atlas_card_renderer.py:467–469` | P2 | SPARKLINE silently skipped in `_calculate_height()` but still enters `_draw_section()` — hidden coupling with HERO. | Document coupling or route through `_draw_section`. |
| 124 | `atlas_card_renderer.py:944–951` | P2 | `card_to_discord_file()` returns `BytesIO` but annotation says `-> bytes`. | Fix annotation or return `buf.read()`. |
| 125 | `card_renderer.py:820–822` | P2 | `box` may be unbound if `bounding_box()` raises inside `if card:` block. | Initialize `box = None` before the block. |
| 126 | `card_renderer.py:803–824` | P3 | Playwright page not closed on exception path — page leak. | Use `try/finally: await page.close()`. |
| 127 | `flow_cards.py:43–89` | P2 | 7 synchronous sqlite3 calls — blocks event loop if not wrapped in executor. | Ensure call sites use `run_in_executor`, or convert to `aiosqlite`. |
| 128 | `flow_cards.py:150–159` | P3 | `_get_leaderboard_rank()` fetches all rows for linear scan. | Use SQL subquery for rank. |
| 129 | `flow_cards.py:191` | P2 | ROI calculated against hardcoded `STARTING_BALANCE = 1000` not actual start balance. | Use user's `season_start_balance`. |
| 130 | `sportsbook_cards.py:147–156` | P2 | Division by zero if `odds == 0`. | Guard: `if abs(odds) == 0: payout += wager`. |
| 131 | `sportsbook_cards.py:44–97` | P3 | DB query functions copy-pasted from `flow_cards.py`. | Extract shared module. |
| 132 | `sportsbook_cards.py:161–173` | P3 | Same leaderboard rank inefficiency as flow_cards. | Same SQL subquery fix. |
| 133 | `hub_view.py:138–160` | P2 | `@ui.button` and `@functools.wraps` decorator order inverted. | Fix order or document intent. |
| 134 | `hub_view.py:148–160` | P3 | No double-click protection — second `defer()` raises `InteractionResponded`. | Check `interaction.response.is_done()` first. |
| 135 | `pagination_view.py:140–143` | P3 | `on_timeout()` disables items but never edits message. | Store message ref and call `message.edit(view=self)`. |
| 136 | `pagination_view.py:97–138` | P2 | No static `custom_id` — can't be restored on restart. | Add comment warning; ensure not registered with UIStateManager. |
| 137 | `ui_state.py:155–215` | P2 | `restore_all_views()` fetch-then-modify across separate connections. | Batch-delete stale records in single query. |
| 138 | `ui_state.py:34` | P3 | UI state stored in `flow_economy.db` — lost if economy DB cleared. | Use `tsl_history.db` or parameterize path. |
| 139 | `setup_cog.py:95–119` | P2 | `get_channel_id()` swallows all exceptions — `None` return hides real errors. | Add `log.error(...)`. |
| 140 | `setup_cog.py:299` | P3 | `await` on potentially sync `casino_db.set_setting` — `TypeError` swallowed. | Verify if coroutine; fix accordingly. |
| 141 | `setup_cog.py:224–227` | P3 | Channel name collision not detected during provisioning. | Add warning log on collision. |
| 142 | `cortex_main.py:32–34` | P2 | Bare imports fail when imported as package from parent directory. | Add `sys.path.insert(0, os.path.dirname(__file__))`. |
| 143 | `cortex_analyst.py:29` | P3 | Hardcoded `gemini-2.5-flash` — CLAUDE.md says project uses 2.0 Flash. | Align or document intentional difference. |
| 144 | `cortex_analyst.py:37–40` | P3 | `timeout: 120_000` — verify if SDK expects ms or seconds. | Check SDK docs; adjust. |
| 145 | `cortex_analyst.py:106–114` | P2 | `_safe_extract_json()` returns truncated JSON as valid — corrupted signals propagate. | Check for `_parse_failed` in `run_all_passes()`. |
| 146 | `cortex_writer.py:40–41` | P3 | `@retry` with no condition — retries on `KeyboardInterrupt`, `SystemExit`, permanent errors. | Add `retry=retry_if_exception_type(...)`. |
| 147 | `cortex_writer.py:288` | P2 | `int(spk.get('grade_level', 0))` — crashes on non-numeric strings like `"10th"`. | Use safe int converter. |
| 148 | `affinity.py:103–133` | P2 | `BEGIN IMMEDIATE` failure → `rollback()` with no active transaction. | Wrap `BEGIN` in its own try/except. |
| 149 | `affinity.py:196,213` | P2 | Keyword `"w"` matches any message containing letter "w" — false positive on nearly everything. | Use word-boundary regex matching. |
| 150 | `flow_wallet.py:109–122` | P3 | `get_balance()` acquires write lock for pure read — contends with writes. | Use plain SELECT without `BEGIN IMMEDIATE`. |
| 151 | `flow_wallet.py:393–443` | P2 | `update_balance_sync()` check-then-update not atomic — TOCTOU race. | Add explicit `BEGIN IMMEDIATE`. |
| 152 | `google_docs_writer.py:91–96` | P3 | Section parser misclassifies `*markdown*` lines as meta. | Tighten regex: `^\*[^*]+\*$`. |
| 153 | `odds_api_client.py:162–168` | P2 | 4 sequential `fetch_odds` calls for independent sports. | Use `asyncio.gather(...)` for parallel fetching. |
| 154 | `lore_rag.py:177–213` | P2 | FAISS index update not thread-safe + blocking on event loop. | Add `threading.Lock`; ensure `run_in_executor` at all call sites. |
| 155 | `atlas_card_renderer.py:218–240` | P3 | Pixel-by-pixel noise fallback — 450K+ writes per card. | Use numpy or ensure `noise_texture.png` exists. |
| 156 | `atlas_card_renderer.py:249–255` | P3 | 200 ellipses per glow × 2 calls per render. | Pre-render or use GaussianBlur. |
| 157 | `atlas_card_renderer.py:686–692` | P3 | `alpha_composite` then `paste()` without mask — alpha inconsistency. | Use `Image.alpha_composite` consistently. |

---

## Cross-Cutting Issues

These patterns appear across multiple files and account for the majority of findings:

### 1. Blocking Synchronous Calls on the Async Event Loop (18 findings)
**Files:** `reasoning.py`, `intelligence.py`, `roster.py`, `build_member_db.py`, `flow_cards.py`, `sportsbook_cards.py`, `codex_cog.py`, `flow_sportsbook.py`, `casino/games/slots.py`, `lore_rag.py`

Synchronous `sqlite3.connect()`, `requests.get()`, and `gemini_client.generate_content()` calls are made directly from async context without `run_in_executor`. This blocks the entire asyncio event loop for the duration of each call (100ms–8s), freezing all Discord interactions.

### 2. TOCTOU Race Conditions in Economy Operations (12 findings)
**Files:** `flow_sportsbook.py`, `real_sportsbook_cog.py`, `casino/casino_db.py`, `casino/games/blackjack.py`, `casino/games/crash.py`, `casino/games/coinflip.py`, `economy_cog.py`, `flow_wallet.py`

Balance checks and debits are separate non-atomic operations. Concurrent Discord interactions can both pass the balance guard before either debit lands, enabling double-spend. The casino games have the same pattern with in-memory state flags (`resolved`, `cashed_out`, `active_sessions`) checked before `await` boundaries.

### 3. Hardcoded Persona Text Instead of `get_persona()` (7 findings)
**Files:** `oracle_cog.py`, `genesis_cog.py`, `sentinel_cog.py`

CLAUDE.md explicitly requires `get_persona()` from `echo_loader`, but all three major cogs hardcode persona strings in Gemini prompts. This means Echo persona changes have no effect on these cogs.

### 4. `status IN (2,3)` Violations (3 findings)
**Files:** `data_manager.py`, `build_tsl_db.py`

CLAUDE.md's most critical data rule is violated in H2H records and owner tenure calculations, silently dropping real completed games.

### 5. View Timeout Without User Feedback (8 findings)
**Files:** `boss_cog.py`, `casino/casino.py`, `pagination_view.py`, `player_picker.py`

Views expire silently — buttons stop working with no visual indication. Users click dead buttons and get "Unknown Interaction" errors.

### 6. SQL Injection Patterns (4 findings)
**Files:** `codex_cog.py`, `flow_sportsbook.py`, `cortex_engine.py`

F-string interpolation in SQL queries. Currently mitigated by input validation or hardcoded values, but fragile if validation is ever relaxed.

### 7. Duplicate Code Between Card Renderers (3 findings)
**Files:** `flow_cards.py`, `sportsbook_cards.py`

Entire DB query functions copy-pasted verbatim. Bug fixes must be applied to both files.

---

## Fix Plan

### P0 — Must Fix (10 items — crashes, data loss, resource leaks)

| # | Finding | Action |
|---|---------|--------|
| 1 | `data_manager.py:878` H2H status filter | Change `status != 3` to `status not in (2, 3)` |
| 2 | `data_manager.py:270` 155 HTTP calls at startup | Query `tsl_history.db` for championship data; cache to disk |
| 3 | `flow_sportsbook.py:1066–1200` bet placement race (3 modals) | Replace check+debit with atomic `flow_wallet.debit()` |
| 4 | `flow_sportsbook.py:874` sync/async DB contention | Migrate grading to `aiosqlite` or serialize all writes |
| 5 | `real_sportsbook_cog.py:352` double-payout risk | Add `UNIQUE` constraint on `reference_key` + `BEGIN IMMEDIATE` |
| 6 | `roster.py:133,229,265` sync SQLite on event loop | Wrap in `run_in_executor` |
| 7 | `sentinel_cog.py:622` module-level Gemini crash | Lazy-init with null guard |
| 8 | `card_renderer.py:107` Playwright resource leak | Store `pw` instance separately from context manager |
| 9 | `build_tsl_db.py:295` non-atomic temp file swap | Add `PRAGMA synchronous = FULL` before commit |
| 10 | `build_member_db.py:1074` concurrent sync writes | Add `asyncio.Lock()` guard |

### P1 — Should Fix (13 items — security, data integrity)

| # | Finding | Action |
|---|---------|--------|
| 1 | `reasoning.py:448,768,965` blocking Gemini calls | Wrap all 3 in `run_in_executor` |
| 2 | `data_manager.py:303` rings proxy `wins >= 14` | Query actual championship records from DB |
| 3 | `flow_sportsbook.py:293` SQL injection in `_set_line_override` | Replace f-string with if/elif dispatch |
| 4 | `flow_sportsbook.py:303` week param ignored in clear | Scope DELETE to week's game IDs |
| 5 | `codex_cog.py:777` SQL injection in `_h2h_impl` | Parameterized query |
| 6 | `codex_cog.py:856` SQL injection in `_season_recap_impl` | Parameterized query |
| 7 | `card_renderer.py:285,808` external font dependency | Download fonts; embed as base64 |
| 8 | `cortex_engine.py:176` f-string SQL pattern | Document or fix |
| 9 | `casino/casino_db.py:248` `flow_wallet.credit(con=db)` not verified | Audit and assert wallet uses passed connection |
| 10 | `casino/casino_db.py:352` daily scratch TOCTOU | Re-read claim inside transaction |
| 11 | `casino/games/blackjack.py:524` session creation TOCTOU | Set sentinel before first `await` |
| 12 | `casino/games/crash.py:239` double-cashout TOCTOU | Set `cashed_out = True` before `await` |
| 13 | `reasoning.py:350` unrestricted exec | Document trust boundary |

### P2 — Logic Fixes (65 items)

**Highest-impact P2 fixes (do these first):**

1. `trade_engine.py:122` — Player values silently 0 for all export-sourced players (wrong OVR field)
2. `affinity.py:196` — Keyword `"w"` false-positive on nearly every message
3. `codex_cog.py:337` — DB_SCHEMA stale at import time
4. `commish_cog.py:477,500` — Wrong cog names → all commish delegations silently fail
5. `build_tsl_db.py:239` — TEXT column MIN gives wrong draft attribution
6. `build_tsl_db.py:182` — Owner tenure counts unplayed games
7. `ability_engine.py:557` — Weight TypeError crashes physics floor checks
8. `build_member_db.py:1266` — Exact DB username lookups fail
9. `flow_cards.py:191` — ROI calculated against wrong starting balance
10. `sportsbook_cards.py:147` — Division by zero on odds=0

**Remaining P2 fixes:** See findings #6–17, #26–28, #47–48, #50, #53, #56–58, #60–61, #63, #65, #74–92, #103–109, #123–125, #127, #129–130, #133, #136–137, #139, #142, #145, #147–149, #151, #153–154.

### P3/P4 — Quality & Improvements (69 items)

**Grouped by theme:**

| Theme | Count | Key Actions |
|-------|-------|-------------|
| View timeout handlers | 8 | Add `on_timeout()` to all hub/casino/pagination views |
| `get_persona()` migration | 7 | Replace hardcoded persona in oracle, genesis, sentinel |
| `print()` → `log` | 3 | `data_manager.py`, `flow_sportsbook.py` |
| Select menu `options=[]` | 3 | `oracle_cog.py` decorators |
| Duplicate card query code | 3 | Extract `flow_db_queries.py` |
| Expired CDN URLs | 2 | `constants.py`, `oracle_cog.py` — host permanently |
| PIL performance | 4 | Noise, glow, felt, slots — use numpy/caching |
| Dead code cleanup | 3 | `sentinel_cog.py:622`, `blackjack.py:33`, `echo_cog.py:28` |
| Retry/error handling | 4 | `cortex_writer.py`, `polymarket_cog.py`, `bot.py:272` |
| Architecture | 4 | Extract `parity_state.py`, parameterize `ui_state` DB path |
| Import hygiene | 2 | `ability_engine.py:428`, `boss_cog.py:100` |
| Misc quality | 26 | See individual findings |

---

## Verification Notes

- All findings cite specific file:line references verified against the current codebase
- P0/P1 findings cross-referenced against CLAUDE.md rules and Discord API constraints
- 67 files reviewed across 6 parallel agents; no files skipped
- Prior audit reports (`AUDIT_REPORT.md`, `code_review.md`) are superseded by this document
