# ATLAS Nightly Audit — 2026-04-11

**Focus:** Core Infrastructure | **Recent commits:** 0 code changes (all 5 recent commits are audit artifacts) | **Files deeply read:** 14 | **Total lines analyzed:** ~4,800

---

## CRITICAL — Fix Before Next Deploy

### [C-01] `awards_cog.py` — Vote Race Condition: `_polls_lock` Acquired Too Late

- **File:** `awards_cog.py` L57–72
- **Risk:** `_polls_lock` is only held during the *write* (`_save_polls`), not across the full read-check-write cycle. Two concurrent button clicks from the same user can both pass `if uid in poll["votes"]` before either writes — allowing double-voting.
- **Evidence:**
  ```python
  uid = str(interaction.user.id)
  if uid in poll["votes"]:          # ← check happens outside lock
      return ...                    # ← another request is here simultaneously
  poll["votes"][uid] = self.values[0]
  await _save_polls()               # ← only the write holds the lock
  ```
  This is a classic TOCTOU (time-of-check / time-of-use) race. Discord's retry behavior on failed interactions makes this especially likely.
- **Fix:** Acquire `_polls_lock` around the full check+write, not just the save:
  ```python
  async with _polls_lock:
      if uid in poll["votes"]:
          return await interaction.response.send_message("⚠️ Already voted.", ephemeral=True)
      poll["votes"][uid] = self.values[0]
      _save_polls_sync()   # already holds lock — call sync version directly
  await interaction.response.send_message("✅ Vote recorded anonymously.", ephemeral=True)
  ```

---

## WARNINGS — Fix This Week

### [W-01] `atlas_home_renderer.py` L50–213 — Outer `except Exception: pass` Silences All DB Errors

- **File:** `atlas_home_renderer.py` L50, L212–213
- **Impact:** The outer `try: ... except Exception: pass` wraps the entire `gather_home_data()` function. Any unhandled error (schema mismatch, connection timeout, missing table) silently returns a card with all stats at zero. The user sees a blank card with no error indication. Admin has no log entry to investigate.
- **Fix:** Replace the outer silent swallow with a logged warning:
  ```python
  except Exception:
      log.warning("gather_home_data failed for user_id=%s", user_id, exc_info=True)
  ```
  The inner per-section `except Exception: pass` blocks are correct (graceful per-table degradation) — only the outer one needs fixing.

### [W-02] `setup_cog.py` L799–800 — Silent Role Enrichment Failure in Auto-Discovery

- **File:** `setup_cog.py` L799–800 (`_auto_discover_db`)
- **Impact:** `except Exception: pass` swallows any DB error during member role status enrichment. The comment says "tsl_members table may not exist yet" — a valid case, but this also swallows schema errors, constraint violations, and lock timeouts. No log entry means role-based status updates can silently stop working after a DB migration.
- **Fix:**
  ```python
  except sqlite3.OperationalError:
      pass  # tsl_members table not yet created — OK at startup
  except Exception:
      log.warning("Role enrichment failed for guild %s", gid, exc_info=True)
  ```

### [W-03] `build_member_db.py` L1258–1275 — N+1 Query in `validate_db_usernames()`

- **File:** `build_member_db.py` L1265–1269
- **Impact:** Runs one `SELECT COUNT(*) FROM games WHERE homeUser = ? OR awayUser = ?` per active member. With 31 members this is 31 individual queries on every startup/sync. If the league grows or this function is called more frequently, it becomes a measurable startup bottleneck.
- **Fix:** Consolidate into a single query:
  ```sql
  SELECT db_username FROM tsl_members
  WHERE db_username IS NOT NULL AND active = 1
    AND db_username NOT IN (
        SELECT DISTINCT homeUser FROM games WHERE homeUser != ''
        UNION
        SELECT DISTINCT awayUser FROM games WHERE awayUser != ''
    )
  ```

### [W-04] `setup_cog.py` L113–161 — `get_channel_id()` Blocks Event Loop on Cache Miss

- **File:** `setup_cog.py` L113 (`get_channel_id`), called at `blowout_monitor` L492, `auto_discover` L905
- **Impact:** `get_channel_id()` is synchronous and opens a sqlite3 connection with a 2-second timeout. It's called directly on the event loop thread from `blowout_monitor` (a `tasks.loop`) and from `auto_discover` (post-executor, back on the event loop). A cache miss + DB lock = 2-second event loop stall. Gateway heartbeat is 41.25s; a 2-second stall is survivable but non-trivial. The async wrapper `get_channel_id_async` exists but isn't used in these call sites.
- **Fix:** Replace the direct call in `auto_discover()` with:
  ```python
  ch_id = await get_channel_id_async(cfg_key, guild.id)
  ```
  For `blowout_monitor`, use `get_channel_id_async` too since it already runs in an async task.

### [W-05] `constants.py` L4–9 — Expiring Discord CDN Icon URL

- **File:** `constants.py` L4–9
- **Impact:** `ATLAS_ICON_URL` contains a signed Discord CDN URL with an expiry timestamp (`ex=69add263`). When it expires, all embeds using the footer icon will silently show a broken image. Already noted in the v1.4.2 changelog but unresolved.
- **Fix:** Host on GitHub raw, an S3 bucket, or Imgur. One-line change in `constants.py`.

### [W-06] `awards_cog.py` — No Voting Window Enforcement

- **File:** `awards_cog.py` — no deadline field in poll schema
- **Impact:** Polls have no time-based close. If a commissioner creates a poll and forgets to run `_closepoll_impl`, voting stays open indefinitely. There's no `end_at` field and no background task to auto-close. Operational risk, not a security issue.
- **Fix:** Add an optional `end_at` ISO timestamp to the poll schema; check it in `VoteSelect.callback` before accepting votes.

### [W-07] `bot.py` L639–641 — Message Handler Swallows All AI Errors the Same Way

- **File:** `bot.py` L639–641 (`on_message` handler)
- **Impact:** One broad `except Exception` covers codex_utils failures, atlas_ai timeouts, affinity errors, and lore_rag errors. All produce the same generic reply: "ATLAS is currently undergoing maintenance." The user gets no useful signal and the operator sees only a traceback with no context about which subsystem failed.
- **Fix:** The current logging (`print` + `traceback.print_exc()`) is adequate for a bot this size. Consider wrapping individual subsystem calls in try/except with labeled log messages so triage is faster. Not blocking, but worth the 10-minute investment.

---

## OBSERVATIONS — Track for Later

### [O-01] `data_manager.py` / `build_tsl_db.py` — Sequential API Fetches; Easy Parallelism Win

Both `load_all()` and `sync_tsl_db()` fetch 8+ independent API endpoints sequentially. Each endpoint is ~0.5–2s. A `ThreadPoolExecutor` across the independent fetch calls could cut startup sync time by 60–70%. The biggest wins are the 6 stat-leader endpoints in `load_all()` (pass/rush/rec/sack/int/tackle) — all are fully independent.

### [O-02] `boss_cog.py` L688–694 — Dead "Grade Week" Button Misleads Operator

`BossSBGradeModal.on_submit` unconditionally returns "Manual grading is no longer supported." The button still appears in the Bets & Props panel. Either remove the button or change the label to "AG Status" (which is what the user probably wants). Leaving a button that always returns an error message is a UX trap.

### [O-03] `data_manager.py` L207 — `discord_history.db` vs `TSL_Archive.db` Naming

`_DB_PATH` at L207 resolves to `discord_history.db`. CLAUDE.md documents the Discord archive as `TSL_Archive.db`. These may be separate databases (one local, one Oracle-only), but the naming inconsistency is confusing and worth a comment clarifying the relationship.

### [O-04] `setup_cog.py::_auto_discover_db` — 6 Separate `sqlite3.connect()` Calls

The function opens and closes 6 separate connections to the same DB file. This is safe but wasteful. A single connection passed through the function would be cleaner and reduce open/close overhead. Low priority given the function runs once per startup.

### [O-05] `build_member_db.py::resolve_db_username` L1370–1379 — Fuzzy Match Writes Permanently

The fuzzy difflib match (cutoff=0.70) auto-caches via `UPDATE tsl_members SET db_username = ?`. If the wrong match is cached, it takes a manual DB edit to fix. A mismatch here means all `/ask me` queries return the wrong player's stats indefinitely. Consider logging the match for human review rather than auto-caching at the 0.70 cutoff.

### [O-06] `atlas_home_renderer.py` — Per-Section `except Exception: pass` Is Correct

Each DB sub-query has its own silent `except Exception: pass`. This is the right pattern — a missing `bets_table` shouldn't break the casino section. Just note that schema drift (e.g., column rename) is invisible without per-section logging. Consider `log.debug` instead of `pass` for easier future debugging.

### [O-07] `bot.py` Docstring Shows v2.0.0 as Latest Changelog Entry

`ATLAS_VERSION` is `7.10.0` but the inline docstring only covers changes up to `v2.0.0`. The docstring is cosmetic, but if someone reads it cold they'll have an incomplete picture of what the bot does. Not worth fixing proactively — just don't rely on it.

### [O-08] `embed_helpers.py` — Used by Only 3 Files (`oracle_cog`, `sentinel_cog`, `flow_sportsbook`)

`build_embed()` in `embed_helpers.py` is imported by only 3 cogs despite being a "shared" builder. Most other cogs construct embeds directly with `discord.Embed(...)`. The inconsistency isn't harmful but means new cogs won't naturally reach for it. Not a bug — just an observation for consistency.

### [O-09] `awards_cog.py` — Announced Results Go to Public Channel, Not Ephemeral

`_closepoll_impl` calls `await interaction.response.send_message(embed=embed)` without `ephemeral=True`. The commissioner's close action will post the results publicly (correct behavior for final results, but worth documenting as intentional).

---

## CROSS-MODULE RISKS

### [X-01] `build_tsl_db.sync_tsl_db()` ATTACH Failure Drops `tsl_members` and `server_config`

- **Caller/Callee:** `build_tsl_db.sync_tsl_db()` → `ATTACH DATABASE old_db` path at L399–418
- **Risk:** If the old `tsl_history.db` has an exclusive write lock when `sync_tsl_db` runs the ATTACH, the preserve-tables step logs a warning and continues — but the new DB is missing `server_config`, `tsl_members`, and `conversation_history`. Every subsequent `get_channel_id()` call returns `None`, all channel routing silently fails, and `build_member_table()` re-seeds from `MEMBERS` (safe but loses runtime team assignments).
- **Mitigation:** This window is small (~seconds during startup) and the bot typically starts without concurrent writers. But during development with multiple sessions open, the risk is real. Consider a pre-ATTACH lock check or a post-swap verification step.

### [X-02] `data_manager._state` References Held Across Atomic Swap

- **Caller/Callee:** Any cog that does `games = dm.df_games` at the start of a command handler
- **Risk:** The `dm.__getattr__` proxy always reads from the current `_state`. But if a handler unpacks `games = dm.df_games` and then awaits, a sync in between replaces `_state`. The local `games` variable holds the old DataFrame. This is the expected Python behavior but could produce stale results in a long-running handler. Not a crash risk — just stale data for one request per sync cycle.

### [X-03] `get_channel_id()` Zero-Config Soft-Fallback Masks `setup_cog` Load Failures

- **Caller/Callee:** All cogs that call `get_channel_id()` → `setup_cog._channel_cache`
- **Risk:** If `setup_cog` fails to load (import error, table creation failure), `_channel_cache` is empty and `get_channel_id()` returns `None` for every key. The `require_channel()` decorator soft-falls back to allowing all channels. No cog fails loudly — they silently post to wrong channels. The only signal is the `print(f"ATLAS Error loading setup_cog: {e}")` at startup. Make sure this startup error is visibly monitored.

---

## POSITIVE PATTERNS WORTH PRESERVING

1. **`data_manager.load_all()` atomic swap** (`L677`): Single `_state = LeagueState(...)` reassignment. GIL guarantees atomicity — no explicit lock needed for the swap itself. Correct and clean.

2. **`build_tsl_db.sync_tsl_db()` write-to-temp-then-replace** (`L347–463`): Writes to `tsl_history.db.tmp`, then atomically replaces. The database is never in a half-built state from the reader's perspective. 3-retry loop for Windows file locking with fallback to `shutil.copy2` is solid defensive coding.

3. **`boss_cog.py` defense-in-depth admin gating**: `default_permissions=discord.Permissions(administrator=True)` at the Group level (L2534–2535) + explicit `await is_commissioner(interaction)` check in every handler AND every modal `on_submit`. Three independent layers — a misconfigured Discord permission won't bypass the code-level check.

4. **`build_member_db.py` COALESCE upsert** (`L1147–1158`): `team = COALESCE(tsl_members.team, excluded.team)` ensures runtime `/commish assign` team assignments survive bot restarts. The IMMEDIATE transaction + `_build_lock` threading lock prevents concurrent build calls from corrupting the seed data.

5. **`setup_cog.py` channel routing soft-fallback**: `require_channel()` returns `True` when no channels are configured (`if not allowed_ids: return True`). New servers work out-of-the-box without a `/setup` run. Graceful degradation over hard failures.

---

## TEST GAPS

| Test Case | Type | Validates | Priority |
|-----------|------|-----------|----------|
| Concurrent vote submissions (same user) | Unit | `awards_cog.py` TOCTOU race condition fix | **HIGH** |
| sync_tsl_db() while another connection holds exclusive lock | Integration | ATTACH failure path — does `tsl_members` survive? | **HIGH** |
| `gather_home_data()` with missing `bets_table` column | Integration | `atlas_home_renderer.py` per-section graceful degradation | MEDIUM |
| `get_channel_id()` called on cache miss during `blowout_monitor` | Unit | Event loop blocking under 2s SQLite timeout | MEDIUM |
| `validate_db_usernames()` with 100+ members | Performance | N+1 query impact at scale | LOW |
| `resolve_db_username()` with two similar usernames (0.70 cutoff) | Unit | False positive fuzzy match gets cached | MEDIUM |

---

## METRICS

| Metric | Value |
|--------|-------|
| Code commits (last 24h) | 0 (all audit docs) |
| Files deeply read | 14 |
| Total lines analyzed | ~4,800 |
| Critical issues | 1 |
| Warnings | 7 |
| Observations | 9 |
| Cross-module risks | 3 |
| Positive patterns noted | 5 |

**Overall health:** Good. The core infrastructure is well-structured with solid atomic swap patterns, defense-in-depth auth, and graceful channel routing degradation. The one critical issue (vote race condition) is a real bug but limited to the awards system — a low-frequency, low-stakes feature. No deploy-blocking issues in the data pipeline or permission system.

**Next audit focus:** Sunday — Cross-Cutting & Integration (full bot scan)

---

## CLAUDE.md UPDATES

### Changes Made

**1. Architecture Reference section — stale cog load order replaced:**
- Section at bottom of CLAUDE.md had an old reference to `commish_cog` as the last cog and was missing `flow_store`, `flow_live_cog`, `real_sportsbook_cog`, `boss_cog`, `god_cog`, `atlas_home_cog` (everything after `economy_cog`).
- Updated to match the actual `_EXTENSIONS` list in `bot.py`.

**2. Module Map — missing entries added:**
- `awards_cog.py` was in the Cog Load Order (#8) but absent from the Module Map table. Added with description "Awards & voting."
- `embed_helpers.py` is imported by `oracle_cog`, `sentinel_cog`, and `flow_sportsbook` but not documented. Added under Core as a shared utility.
- `oracle_memory.py` is imported in `bot.py` (`from oracle_memory import OracleMemory`) and used for conversation memory in the `on_message` handler but not in the Module Map. Added under Conversation Memory.
