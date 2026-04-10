# Adversarial Review: intelligence.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 866
**Reviewer:** Claude (delegated subagent)
**Total findings:** 23 (3 critical, 9 warnings, 11 observations)

## Summary

`intelligence.py` is an optional analytics module that centralizes draft grading, hot/cold trending, clutch stats, owner profiles, and beef detection. It is imported as `intel` in `bot.py` with an `ImportError` soft-fallback, but its `build_owner_map()` runs inside `_startup_load` and populates module-level caches that other cogs (`oracle_cog` imports as `ig`) consult directly — any failure here silently degrades profile lookups without alerting admins. The module has real correctness hazards: blocking sqlite/Pandas calls inside async entry points, non-thread-safe mutation of `_owner_profiles` + `_paginated_messages`, silent exception swallowing in roster/identity loading, and a draft-class team aggregation that returns top-10 teams but omits the grade column everywhere else.

## Findings

### CRITICAL #1: Blocking sqlite3 I/O inside async function `get_team_draft_class`

**Location:** `intelligence.py:244-268`
**Confidence:** 0.90
**Risk:** `get_team_draft_class_async()` wraps `get_team_draft_class()` in `run_in_executor`, which is correct — but `get_team_draft_class()` is *also* called synchronously from `oracle_cog.py:1505`, `:1713` via `await ig.get_team_draft_class_async(...)` (OK) AND directly in other codepaths where `get_team_draft_class` is a plain sync function. The function itself opens `sqlite3.connect(DB_PATH)` and runs a full `SELECT` on line 261-267. Inside any async caller that accidentally invokes the sync form (or the function is called by a future Discord interaction handler without the wrapper), this blocks the event loop for the duration of the query.
**Vulnerability:** The sync variant is exported publicly and there is no assertion/guard forcing async callers through the wrapper. A future `/boss` or `/oracle` command added by anyone unfamiliar with the split will regress the bot. The same pattern exists for `get_clutch_records` (sync, called from `oracle_cog.py:859, 1036, 1270, 1994, 2303` in async handlers) and `get_hot_cold` (sync, called from `oracle_cog.py:930, 4093, 4501, 4580` in async handlers).
**Impact:** On a large league (32 teams × season worth of games), clutch stats scan every row and perform 6 boolean masks per team in Python — easily 100-500ms. `get_hot_cold` calls `dm.df_offense.copy()` + `dm.df_defense.copy()` on every invocation (full DataFrame copy). Combined with `oracle_cog.py:929`'s for-loop invoking `ig.get_hot_cold(pi["name"])` 20 times sequentially in an async context (no `asyncio.to_thread`), the event loop stalls for seconds, blocking all other Discord interactions and heartbeats.
**Fix:** Either (a) make `get_team_draft_class`, `get_clutch_records`, `get_hot_cold` private (`_`-prefixed) and expose only async wrappers that route through `asyncio.to_thread()`, or (b) have every Oracle call site wrap the invocation in `await asyncio.to_thread(ig.get_hot_cold, pi["name"], last_n=3)`. Cache the DataFrame copy across invocations in the same request so 20 players don't trigger 40 copies.

---

### CRITICAL #2: Concurrent mutation of module-level `_owner_profiles` without locking

**Location:** `intelligence.py:578, 626-656, 659-661, 664-672`
**Confidence:** 0.85
**Risk:** `_owner_profiles: dict[int, dict]` is mutated from `get_or_create_profile()` (`interactions += 1`, insert of new key), `record_roast()` (`roast_count += 1`), and `record_beef()` (list append, dict modify). These functions are called from `oracle_cog.py` async event handlers (e.g., `:816`, `:1187`) and `detect_beef()` at `:720`, which will fire from `on_message` handlers that Discord.py dispatches concurrently via `asyncio.ensure_future`. Python's GIL protects the bucket insert but does NOT protect the compound `interactions += 1` read-modify-write, nor the `next((b for b in profile["beefs"] if b["opponent_id"] == oid), None)` + subsequent append inside `record_beef`.
**Vulnerability:** Two concurrent messages from the same user can race on `get_or_create_profile()` — both see `discord_user_id not in _owner_profiles`, both construct profiles, second overwrites first (losing the beefs list from the first if it was mutated). `record_beef()` without a lock can double-count beefs when two messages arrive simultaneously. More insidiously, any thread iterating `_owner_profiles.items()` while another coroutine upserts will raise `RuntimeError: dictionary changed size during iteration` — although no such iteration exists *yet*, the contract is unsafe for future callers.
**Impact:** Silent state corruption in profile counters; rare dictionary-size race; beef double-counting inflates "Active beefs in chat" stat shown in `/oracle profile`.
**Fix:** Protect `_owner_profiles` and `_paginated_messages` with a `threading.RLock` (or `asyncio.Lock` since callers are async). Simpler: since all callers are inside the event loop, you can rely on cooperative scheduling IF no `await` happens between read and write — but `get_or_create_profile` has no await, so it's atomic in practice. Explicitly document this invariant OR add a lock. The docstring currently says nothing about thread-safety, making the module a landmine for any future sync/threaded caller.

---

### CRITICAL #3: Silent roster import failure during identity cache load breaks team lookup

**Location:** `intelligence.py:108-112`
**Confidence:** 0.90
**Risk:** `_load_identity_cache()` wraps `import roster; for entry in roster.get_all(): KNOWN_MEMBER_TEAMS[entry.discord_id] = entry.team_name` in `except Exception: log.warning("Roster load failed in identity cache")`. The entire `for entry in roster.get_all()` loop can fail midway (e.g., if `entry.discord_id` is None for a stub entry) and leave `KNOWN_MEMBER_TEAMS` partially populated. The fallback in `get_or_create_profile()` at line 637-642 depends on `KNOWN_MEMBER_TEAMS.get(discord_user_id)` as a backup; if roster load failed silently, all new profiles get `team=None` and the user appears as "NOT IN LEAGUE (spectator or unknown)" in `/oracle profile`.
**Vulnerability:** The log is `warning` level, not `error`. It does not raise to the caller, so `build_owner_map()` proceeds as if everything is fine and `oracle_cog` displays wrong team assignments. Since `bot.py:456-461` logs `intel.build_owner_map() failed` only on top-level exception, a partial failure is completely invisible. Worse, the roster module is the authoritative source per CLAUDE.md ("Owner Registry — Discord user ↔ team assignments"), so this is a soft-corruption of the canonical mapping.
**Impact:** After any roster module hiccup (missed migration, empty DB, stale cache), every user appears as having no team. Echo persona context strings (`get_owner_context`) go wrong, `/oracle profile` shows nothing, beef detection silently fails at line 727 because `current_team = get_owner_team(current_username)` also returns None.
**Fix:** (a) Promote the exception to `log.exception(...)` so stack trace is visible. (b) Surface a counter: `log.error(f"Roster load failed in identity cache: {e}", exc_info=True)` and record `_identity_cache_loaded = False` so downstream code can detect degraded state. (c) In `bot.py`, check the flag after `intel.build_owner_map()` and refuse to start or emit an admin alert to `ADMIN_CHANNEL_ID`.

---

### WARNING #1: `_load_identity_cache()` silently swallows ImportError from `build_member_db`

**Location:** `intelligence.py:81-86`
**Confidence:** 0.85
**Risk:** `except Exception: log.warning("Identity cache load failed: build_member_db unavailable")` catches *any* exception — not just `ImportError`. If `build_member_db.get_active_members()` raises `sqlite3.OperationalError` (e.g., schema drift, missing table, DB file locked), the warning misleadingly says "build_member_db unavailable" when the real problem is a corrupted DB.
**Vulnerability:** The broad except obscures the root cause and the warning message blames the wrong thing. Debugging requires grepping for the raw exception.
**Impact:** When things break, ops waste time chasing the wrong problem. Identity cache silently stays empty (`KNOWN_MEMBERS` is `{}`), which means `get_nickname()`, `get_ids_for_nickname()`, and the entire owner fuzzy lookup chain returns None.
**Fix:** Split the catch: `except ImportError: ...` and `except Exception: log.exception("Identity cache load failed: %s", e)`. Never catch broad `Exception` without logging the stack.

---

### WARNING #2: `build_owner_map()` has no return value, no status flag — caller can't detect failure

**Location:** `intelligence.py:591-615`
**Confidence:** 0.85
**Risk:** `build_owner_map()` early-returns at line 598-599 when `dm.df_teams is None or dm.df_teams.empty` with NO log line, NO warning, NO return value. Caller in `bot.py:459` has no way to know owner map is empty. Every lookup afterward returns None, and the bot silently runs in "no owners known" mode.
**Vulnerability:** Startup race condition: if `build_owner_map()` runs before `data_manager.load_all()` completes, `dm.df_teams` is empty (default_factory) and the map silently empties. Per CLAUDE.md: "build_owner_map() is called at startup from bot.py _startup_load. If it fails, intel.X calls in other cogs may NPE." This is exactly that silent-failure vector.
**Impact:** Owner context strings are broken, beef detection is broken, team assignments shown as None. Invisible until a user types `/oracle profile` and sees nothing.
**Fix:** Return `bool` (success), log the "df_teams empty" case at WARNING, and have `bot.py` assert that `dm.df_teams` is loaded BEFORE calling `intel.build_owner_map()`. Bonus: add a `_owner_map_built_at: float` timestamp and expose it for health checks.

---

### WARNING #3: Cross-reference nickname→team uses substring match, matches wrong user

**Location:** `intelligence.py:608-615`
**Confidence:** 0.85
**Risk:** The cross-reference loop does `if nick_lower in uname: _username_to_team[nick_lower] = team`. Substring matching across arbitrary nicknames → API usernames is a foot-gun: nickname `"Al"` matches `"AlexJ"`, `"falcon_al"`, `"alpha_squad"` and picks the first hit via `break`. A user named `"nat"` is matched into `nate_something`'s team. Per CLAUDE.md Identity Resolution: "API usernames have underscores/case mismatches. Use `_resolve_owner()` fuzzy lookup." This is NOT the documented fuzzy resolver.
**Vulnerability:** The match is order-dependent (first hit wins from `list(_username_to_team.items())`), so the resolution changes based on `df_teams` row order. Same nickname can resolve to different teams run-to-run depending on API ordering.
**Impact:** Wrong owner → wrong team attribution in Echo persona, wrong H2H records in beef mode, wrong clutch stats when drilling into "my team".
**Fix:** Use `build_member_db.get_alias_map()` (CLAUDE.md says this maps 88+ variants to canonical DB usernames) instead of reinventing substring matching. Or import `_resolve_owner()` from wherever it lives and use it. At minimum, require exact equality or prefix match with length check (e.g., `len(nick_lower) >= 4 and uname.startswith(nick_lower)`) to reduce false positives.

---

### WARNING #4: `_resolve_col()` ignores column priority, picks first match not best match

**Location:** `intelligence.py:504-508, 510-514`
**Confidence:** 0.75
**Risk:** `_resolve_col(games, "homeScore", "homeTeamScore")` picks the first candidate that exists. If a DataFrame schema migration ever includes BOTH columns (e.g., legacy `homeScore` and new `homeTeamScore`), the function silently picks the legacy one. There is no preference ordering documented, no fallback detection, no warning logged when multiple candidates exist.
**Vulnerability:** Schema drift between `df_games` (current week from `/games/schedule`) and `df_all_games` (full season from exports) almost certainly uses different column names. The function picks one and treats both sources identically.
**Impact:** On one side of a schema drift, clutch stats compute from one column set; on the other side, from another. Results diverge silently.
**Fix:** Require explicit column specs per DataFrame source, or explicitly check for column set equality and log a warning when mismatched. Cite which column was actually picked in debug logs.

---

### WARNING #5: `get_hot_cold` name collision fallback uses case-insensitive substring match

**Location:** `intelligence.py:422-427`
**Confidence:** 0.80
**Risk:** When `player_df.empty`, fallback splits `player_name` on `.` or space and uses `df["fullName"].str.contains(last, case=False, na=False)`. For a common last name like "Smith", this hits every Smith in the league and returns the *first* by weekIndex. The reported stats then belong to a totally different player than the user asked about.
**Vulnerability:** No uniqueness check, no "multiple matches" error. The function returns hot/cold stats for the wrong player and the user has no indication the match failed.
**Impact:** `/oracle player "T.Smith"` shows stats for a random Smith. Decision-making (e.g., "Is this player HOT?") is misdirected.
**Fix:** When fallback matches >1 distinct player, either error out ("Multiple players matched 'Smith': [list]") or weight by snippet similarity (Levenshtein). Never silently pick the first.

---

### WARNING #6: `get_hot_cold` trend calculation undercounts when `season_avg` is zero

**Location:** `intelligence.py:442-447`
**Confidence:** 0.85
**Risk:** `deltas[col]` only gets populated when `sa > 0`. The trend_score loop at line 454-460 iterates `for col, delta in deltas.items()` — so columns with zero season average are simply ignored, even if the last N games had huge values. A rookie's first 3 games of the season will show zero trend because `season_avg == last_n_avg` and `deltas` is empty or all-zero.
**Vulnerability:** The logic is "percentage change from season average" which is undefined when season_avg is 0. But the fallback of "exclude the column" means a player going from 0 TDs career → 3 TDs in the last 3 games shows neutral, not HOT.
**Impact:** Breakout rookies never trigger the 🔥 HOT label. Stat correction for retired/IR players returning also never flags as hot.
**Fix:** When `sa == 0` and `la > 0`, record `deltas[col] = 100.0` (capped) or compute absolute delta and normalize via a reference distribution. At minimum, score these as positive trend.

---

### WARNING #7: `detect_beef()` resolves `current_team` via fallible `get_owner_team` and doesn't handle None

**Location:** `intelligence.py:727, 743`
**Confidence:** 0.80
**Risk:** `current_team = get_owner_team(current_username)` returns None if the caller isn't in the username cache. Line 743 computes `h2h = dm.get_h2h_record(current_team, opponent_team) if current_team else {}` — correct for None — but then `build_beef_context()` at line 758 does `a_team = beef.get("challenger_team", "Unknown")` which yields `None` (not `"Unknown"`) because `.get(key, default)` returns the stored value if present, and `None` *is* present in the dict. The `"Unknown"` default never fires.
**Vulnerability:** Subsequent `f"{beef['challenger']} ({a_team}) is coming at ..."` renders as "nate (None) is coming at..." in the output. Ditto line 770's `dm.df_standings["teamName"].str.lower() == team.lower()` which raises `AttributeError: 'NoneType' object has no attribute 'lower'` when team is None.
**Impact:** Beef mode crashes when either user isn't yet mapped to a team. Triggers an unhandled exception in `on_message` handler chain.
**Fix:** Guard `if team is None: continue` inside the loop at line 768. Change `.get("challenger_team", "Unknown")` to `.get("challenger_team") or "Unknown"` to coerce None → "Unknown".

---

### WARNING #8: Draft class `team_grades.head(10)` silently drops most teams from leaderboard

**Location:** `intelligence.py:240`
**Confidence:** 0.70
**Risk:** `cls.groupby("teamName").agg(...).head(10).to_dict("records")` returns only the top-10 teams by gradeScore. TSL has 31-32 teams. If a user asks "what was Season 5 like for the Cardinals", and the Cardinals' draft graded poorly, their row is NOT returned to the caller. The caller has no way to know they exist but were excluded.
**Vulnerability:** The caller in `oracle_cog.py` presumably renders `team_grades` as a UI element without knowing it's truncated.
**Impact:** `/oracle draft-class <season>` displays "Top 10 drafts by grade" when the UI label says "All draft grades". Users think their team didn't draft anyone.
**Fix:** Return the full list and let the UI paginate (the `PaginatedResult` class exists for this). Or document the truncation in the return payload: `"team_grades_truncated": True, "team_grades": top10`.

---

### WARNING #9: `PaginatedResult` next/prev mutate shared state across concurrent Discord users

**Location:** `intelligence.py:808-862`
**Confidence:** 0.80
**Risk:** `_paginated_messages: dict[int, tuple[PaginatedResult, float]]` keyed by `message_id`. `register_pagination()` stores a `PaginatedResult` that tracks `self.current: int`. Any Discord user who sees the ephemeral/public message and reacts can call `get_pagination(message_id)` to advance `self.current`. Two users reacting simultaneously race on `self.current += 1` (no lock). One user can "steal" the page state of another user viewing the same pagination.
**Vulnerability:** No per-user page state. Per CLAUDE.md: "Ephemeral vs public: drill-downs = ephemeral; hub landing embeds = public." If a public paginated message is shared, this becomes chaos.
**Impact:** Two users reading the same public leaderboard independently flip pages on each other. Also, `_prune_stale_pages()` deletes entries older than 30min but doesn't lock — iteration during mutation from another caller can raise RuntimeError.
**Fix:** Per-user page state: key by `(message_id, user_id)` or store each user's `current` in a sub-dict. Wrap mutations in a lock. Consider discord.py's built-in `discord.ui.View` pagination, which handles per-interaction state correctly.

---

### OBSERVATION #1: Silent `except Exception: pass` in `get_or_create_profile` roster import

**Location:** `intelligence.py:632-636`
**Confidence:** 0.80
**Why:** Second silent swallow in the file (first is at line 108-112, third is at line 84-86). `except Exception: pass` hides real errors in `roster.get_team_name()`. Per CLAUDE.md Flow Economy Gotchas: "Silent except Exception: pass in admin-facing views is prohibited." While this isn't admin-facing, it's still a hidden failure path. Should at minimum `log.debug(...)` or `log.exception(...)`.
**Fix:** `except Exception: log.exception("roster.get_team_name failed for discord_id=%s", discord_user_id)`.

---

### OBSERVATION #2: `teamName` column swap in `get_draft_class` breaks mental model

**Location:** `intelligence.py:183-186`
**Confidence:** 0.80
**Why:** The DB returns `drafting_team` but the DataFrame column is renamed to `teamName` via the `columns=[...]` arg. Readers familiar with `df_teams` (where `teamName` means *current* team) will misread the aggregation at line 212 `cls.groupby("teamName")` as "group by current team" when it's actually "group by drafting team". This is the exact bug v3→v4 was supposed to fix. Keep the column name consistent with semantics: `drafting_team`.
**Fix:** Rename the column to `drafting_team` throughout, update `_pick_cols` accordingly.

---

### OBSERVATION #3: `_YEAR_BASE` and `YEAR_TO_SEASON` hardcoded year range ends at 2035

**Location:** `intelligence.py:44-46`
**Confidence:** 0.95
**Why:** `YEAR_TO_SEASON = {yr: yr - _YEAR_BASE for yr in range(2025, 2035)}` hardcodes the upper bound. In year 2035, `SEASON_TO_YEAR.get(11)` returns None and the fallback at line 198 `SEASON_TO_YEAR.get(season, season + 2024)` uses a magic number `2024` that *happens* to equal `_YEAR_BASE`. If `_YEAR_BASE` is ever changed, line 198 won't be updated and values diverge.
**Fix:** Use `_YEAR_BASE` in the fallback: `SEASON_TO_YEAR.get(season, season + _YEAR_BASE)`. Extend range to 2050 or compute dynamically from `dm.CURRENT_SEASON`.

---

### OBSERVATION #4: `get_draft_class` / `get_team_draft_class` duplicated grading logic

**Location:** `intelligence.py:183-241, 286-329`
**Confidence:** 0.85
**Why:** Two functions both load draft rows, compute `devScore * 0.6 + ovr_norm * 0.4`, and apply `_letter_grade`. The math is copy-pasted in Python (`get_team_draft_class`) vs. vectorized Pandas (`get_draft_class`). Risk of drift — a fix in one doesn't propagate to the other.
**Fix:** Extract a shared `_grade_player(row)` helper used by both.

---

### OBSERVATION #5: `get_clutch_records` "clutch_winpct" division and the fallback when `close` is empty

**Location:** `intelligence.py:547-554, 563`
**Confidence:** 0.70
**Why:** When `close` (the close-games DataFrame filtered on margin) is empty because the season has no ≤7pt games yet, all `cw`/`cl` calculations are zero-element sums and return `0`. `clutch_winpct` division is guarded by `if (cw + cl) > 0 else 0`, so it's safe. But at line 571-572, `df.iloc[0]["team"]` for most_clutch and `df.sort_values("clutch_winpct").iloc[0]["team"]` for least_clutch return the *same* team (row 0) when all teams tie at 0 — both "most clutch" and "least clutch" point at the alphabetically first team, misleading users.
**Fix:** Return `None` / `"N/A"` when `close.empty` rather than picking an arbitrary team.

---

### OBSERVATION #6: `get_draft_class` `busts` logic uses `|` not an OR of two real conditions

**Location:** `intelligence.py:207-210`
**Confidence:** 0.75
**Why:** `early[(early["dev"] == "Normal") | (early["playerBestOvr"] < 75)]` flags "Normal dev OR sub-75 OVR" as a bust. This means a 74 OVR Star dev trait player is tagged as a bust, which is wrong — a Star at 74 is fine for a late-round pick. The OR should be AND, or use a per-round threshold.
**Fix:** `early[(early["dev"] == "Normal") & (early["playerBestOvr"] < 75)]`. Consider round-aware thresholds.

---

### OBSERVATION #7: `compare_draft_classes` sequentially `await`s N seasons instead of `gather`

**Location:** `intelligence.py:332-348`
**Confidence:** 0.90
**Why:** The `for season in range(2, dm.CURRENT_SEASON + 1): dc = await get_draft_class(season)` loop serializes the per-season DB reads. On a season-95 league (CLAUDE.md says 95+ seasons), this is 94 sequential `run_in_executor` trips to the sqlite connection pool. Should be `asyncio.gather(*(get_draft_class(s) for s in ...))` for parallel execution.
**Fix:** Use `asyncio.gather()` with a semaphore to cap concurrent DB queries.

---

### OBSERVATION #8: `_load_full_players` creates `fullName` without `strip()`

**Location:** `intelligence.py:137-140`
**Confidence:** 0.70
**Why:** `df["fullName"] = fn + " " + ln` leaves trailing whitespace when lastName is empty ("Madden" + " " + "") = "Madden ". Downstream exact-match lookups via `df["fullName"] == player_name` will miss, forcing fallback to substring search (WARNING #5).
**Fix:** `df["fullName"] = (fn + " " + ln).str.strip()`.

---

### OBSERVATION #9: `get_hot_cold` docstring claims "structured dict" but signature lacks type hint

**Location:** `intelligence.py:387-487`
**Confidence:** 0.60
**Why:** `def get_hot_cold(player_name: str, last_n: int = 3) -> dict:` returns a dict with 11 keys on success path and 2 keys on error path. No `TypedDict`, no schema. Callers parse fields by guessing. Also returns different shapes based on `group == "offense"` vs `"defense"`.
**Fix:** Use `TypedDict` or a dataclass for clarity, or at minimum document the key set explicitly.

---

### OBSERVATION #10: `get_clutch_records` uses `df_all_games` fallback to `df_games` without merging

**Location:** `intelligence.py:498`
**Confidence:** 0.70
**Why:** `src = dm.df_all_games if not dm.df_all_games.empty else dm.df_games`. If `df_all_games` is partially populated (e.g., through week 8 but current week is week 10), `df_games` has the week 9-10 data that `df_all_games` lacks. The logic picks exactly one source — so mid-season clutch stats miss the current week's data.
**Fix:** Union both sources and dedupe by gameId (or trust `df_all_games` includes everything and explicitly refresh it).

---

### OBSERVATION #11: No `__all__` export list, no public-API contract

**Location:** `intelligence.py:1-34`
**Confidence:** 0.60
**Why:** Module exports via wildcard import convention. Internal helpers (`_load_identity_cache`, `_load_raw_offense`, `_load_raw_defense`, `_prune_stale_pages`, `_owner_profiles`) are accessible via `intel.X` from any caller. There's no `__all__` list, so private/public split is convention-only. Future refactors can break callers without warning.
**Fix:** Add `__all__ = [...]` listing only the intended public API surface.

---

## Cross-cutting Notes

1. **Blocking I/O in async contexts** — `intelligence.py` routinely exposes sync functions (`get_clutch_records`, `get_hot_cold`, `get_team_draft_class`, `build_leaderboard_data`) that are called from `oracle_cog.py` async handlers without `asyncio.to_thread()`. This is a pattern that likely affects `analysis.py`, `codex_cog.py`, and other Ring 1 analytics modules — every "analytics helper" module should have async wrappers or all callers must route through `asyncio.to_thread`. Consider a blanket policy enforced via a lint rule or audit pass.

2. **Silent except swallowing** — Three silent/warning-only catches in this file (lines 84, 108-112, 632-636) is symptomatic of the broader analytics stack tolerating missing data by erasing errors. CLAUDE.md explicitly prohibits this in admin-facing views; the same principle should apply to any code that populates caches other modules depend on. Recommend a grep sweep: `except Exception: *pass` across Ring 1 Oracle/Codex/AI modules.

3. **Non-thread-safe module-level caches** — `_owner_profiles`, `_paginated_messages`, `KNOWN_MEMBERS`, `_nickname_to_ids`, `KNOWN_MEMBER_TEAMS`, `_username_to_team`, `_team_to_username` are all module-level dicts mutated without locks. While the GIL + single-threaded asyncio saves most call patterns, any future move to worker threads or `concurrent.futures.ThreadPoolExecutor` (which `run_in_executor` already uses!) could race. The sync functions running in the default executor thread pool + the async functions on the main loop can race today on the owner profile dicts. A dedicated lock or a move to `contextvars` / a single mutex owner class is warranted.

4. **Substring fuzzy matching instead of the canonical alias map** — The cross-reference loop at line 608-615 reinvents identity resolution in ways CLAUDE.md explicitly warns against ("Use `_resolve_owner()` fuzzy lookup"). There's probably similar duplication in `oracle_cog`, `genesis_cog`, `codex_cog`. Consolidate all owner resolution through `build_member_db.get_alias_map()` or a single `identity.py` module.

5. **Draft class grading math drift** — Two copies of the grading formula (vectorized Pandas in `get_draft_class`, row-by-row Python in `get_team_draft_class`) is a landmine. Any ranking tweak must be duplicated or the league vs team views diverge.
