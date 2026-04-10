# Adversarial Review: oracle_query_builder.py

**Verdict:** needs-attention
**Ring:** orphan (LIVE — imported by `oracle_agent.py`, `oracle_cog.py`, `test_query_builder`)
**Reviewed:** 2026-04-09
**LOC:** 1708
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (3 critical, 8 warnings, 6 observations)

## Summary

This file is a typed SQL builder consumed by an LLM agent that writes Python against the exported `Query` API and the high-level helpers. The first-line defense (SELECT-only) lives in `codex_utils.run_sql`, which is correct — but `Query` itself trusts every identifier its caller hands it (column, table-qualifier, ORDER BY column, GROUP BY columns, raw `where()` clauses), and the LLM is a caller. Combined with several `f"... LIMIT {limit}"` and `f"... ORDER BY {extreme_type}"` interpolations of unvalidated parameters, the file accumulates a real attack surface for prompt-injected SQL fragments and silent correctness regressions. None of the issues breach the SELECT-only PRAGMA, but several can corrupt result sets, crash queries, or subvert the domain guards the file claims to enforce.

## Findings

### CRITICAL #1: `Query.where()` is a wide-open string-injection seam exposed to the LLM
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:299-303`
**Confidence:** 0.92
**Risk:** `Query.where(clause, *params)` appends `clause` verbatim into the WHERE clause. The `Query` class is exported into the agent sandbox via `build_agent_env` (`oracle_agent.py:532`), so the LLM-generated Python can pass any string as `clause`. Because the SELECT-only guard in `codex_utils.run_sql` only inspects `stripped.upper().startswith("SELECT")` and forbids semicolons, an injected fragment like `where("1=1 UNION SELECT name, sql FROM sqlite_master --")` is not multi-statement and is not blocked.
**Vulnerability:** The docstring says "Add a raw WHERE clause with parameterized values", but only `*params` are parameterized — `clause` itself is concatenated into the SQL string with no validation. The `query_only=ON` PRAGMA in `codex_utils.run_sql` prevents writes but does NOT prevent UNION-based information disclosure or schema reconnaissance.
**Impact:** A prompt-injected user query (e.g., a Discord message that the agent passes through into a generated `q.where("...")` call) can exfiltrate the contents of any table the bot's connection can read — including `tsl_history.db` chat archives if reachable, `flow_economy.db` if attached, or `sqlite_master` itself. This is exactly the "SQL injection via string formatting in NL→SQL Codex pipeline" risk called out in CLAUDE.md.
**Fix:** Either (a) drop the raw `where()` method from the sandbox-exposed surface entirely and force callers through `filter()`, or (b) restrict accepted clause patterns to `^[\w. ]+\s*(=|!=|<|>|<=|>=|IN|LIKE|BETWEEN|IS NULL|IS NOT NULL)\s*\?` and reject anything else with a `ValueError`. At minimum, run `clause` through the same validation that `validate_sql` in `codex_utils.py` runs before appending.

### CRITICAL #2: `Query.aggregate()`, `select()`, `group_by()`, and `sort_by()` all interpolate column identifiers directly into SQL with no allowlist
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:279-282, 305-316, 318-321, 329-352, 376-383, 407-408, 416-421`
**Confidence:** 0.90
**Risk:** Every fluent setter accepts arbitrary strings and stores them, then `build()` interpolates them into SQL via f-strings:
- Line 379: `f"{agg}(CAST({col} AS {cast_type})) AS {col}"`
- Line 383: `f"SELECT {', '.join(select_parts)}\nFROM {self._table}"`
- Line 408: `f"\nGROUP BY {', '.join(self._group_bys)}"`
- Line 418: `f"\nORDER BY {self._order_col} {self._order_dir}"`
- Line 421: `f"\nORDER BY CAST({self._order_col} AS {cast_type}) {self._order_dir}"`

The agg type gets a 5-value allowlist (line 313) and `_table` gets a frozenset allowlist (line 261), but column names do not. An LLM (or anything that can craft a `Query`) can call `q.select("* FROM sqlite_master --")`, `q.aggregate(**{"x AS bar, (SELECT password FROM secrets) AS leak": "SUM"})`, or `q.sort_by("1 -- AND junk")`. All of these slip past `codex_utils.run_sql`'s SELECT-only check because the result still starts with `SELECT`.
**Vulnerability:** The file relies on convention ("the agent will pass column names from `DomainKnowledge`") with zero runtime enforcement. The `_get_cast_type` helper iterates `DomainKnowledge.STATS` looking for a matching column and silently returns `"INTEGER"` if none is found (line 478) — no failure, no warning, just trust.
**Impact:** Schema disclosure, cross-table reads, view of any column the connection can SELECT. Same blast radius as CRITICAL #1.
**Fix:** Validate every column identifier against a precomputed allowlist (union of `DomainKnowledge.columns()` plus a small set of known structural columns: `fullName`, `teamName`, `seasonIndex`, `weekIndex`, `pos`, etc.). Reject anything containing whitespace, parentheses, commas, semicolons, or comment markers. Apply the same to GROUP BY and ORDER BY column names.

### CRITICAL #3: `recent_games_query` and `game_extremes` interpolate `limit` directly into SQL
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:819, 843-847`
**Confidence:** 0.85
**Risk:** Both functions accept `limit: int = 5` but interpolate via `f"... LIMIT {limit}"` instead of `LIMIT ?`. Python's type hint is not enforced at runtime, and the LLM can call `recent_games_query("user", limit="5; ATTACH DATABASE '/etc/passwd' AS p")` — although `;` is blocked downstream, `recent_games_query("user", limit="5 UNION SELECT * FROM sqlite_master")` is not. `game_extremes` has the same shape on line 819 plus `extreme_type` interpolated through a `dict.get(...)` default — but the default branch `'margin DESC'` only triggers when the key isn't found, so attacker-supplied `extreme_type="blowout"` lands in the dict. However, an attacker who picks an unknown key gets the safe default — that branch is fine. The `limit` interpolation is the real hole.
**Vulnerability:** Type hints are documentation, not enforcement. There is no `isinstance(limit, int)` check, and the surrounding f-string trusts the value.
**Impact:** Same UNION-injection class as #1 and #2. Easier to trigger because `limit` is the most-passed parameter from the LLM.
**Fix:** Replace `f"... LIMIT {limit}"` with parameterized `LIMIT ?` and append `int(limit)` to params. Apply `int()` coercion on entry. The `Query.limit()` builder already does `if n < 1: raise ValueError(...)` but does not enforce `isinstance(n, int)` either — fix that one too.

### WARNING #1: `_sanitize_input` is broken cosmetic security and silently mutates legitimate names
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:31-33, 506, 515, 544, 596, 679, 747-748, 829-830`
**Confidence:** 0.88
**Risk:** The function strips `'`, `"`, `;`, `\`, and `-` from any string. It is then applied to user/team/player names that are immediately bound through `?` parameters — so the sanitization buys zero SQL safety (the `?` already does that) but DOES corrupt legitimate identifiers:
- A team named `"D'Andre Swift"` becomes `"DAndre Swift"`
- An owner named `"Nate-Witt"` becomes `"NateWitt"`
- The Cowboys `"5-11"` record query string becomes `"511"`
- Any name with an apostrophe (`O'Connor`, `D'Angelo`) silently no-matches the database row.

The docstring says "Strip special chars from user-supplied names before inclusion in AI prompts" — but the call sites are NOT prompt assembly, they are SQL parameter binding (`h2h`, `owner_record`, `team_record`, `streak_query`, `roster_query`, `abilities_query`, `recent_games_query`). The sanitizer is in the wrong layer.
**Vulnerability:** Defense-in-depth claim is false. The author appears to have copied the helper from a prompt-assembly path and pasted it into SQL-parameter paths, where it provides no benefit and actively damages correctness.
**Impact:** Silent zero-result queries when a player/team/owner name contains any of the stripped characters. The user sees "no results" instead of the real record. This is hard to debug because there is no warning logged.
**Fix:** Remove `_sanitize_input` from all parameterized-binding call sites. If sanitization is needed for a future prompt path, write a separate `_sanitize_for_prompt` that targets prompt-injection markers (backticks, fences, angle brackets) and use `?`-binding for SQL.

### WARNING #2: `_get_cast_type` returns wrong type for any column not present in `DomainKnowledge.STATS`
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:473-478`
**Confidence:** 0.80
**Risk:** The lookup iterates `DomainKnowledge.STATS.values()` and returns the first matching `cast_type`, defaulting to `"INTEGER"`. Many columns the agent will reasonably ORDER BY are NOT in the registry: `playerBestOvr`, `homeScore`, `awayScore`, `seasonIndex`, `weekIndex`, `winPct`, `netPts`, `tODiff`, `passerRating` (only as a stat lookup, not as an order column), and so on. For any of those, the builder produces `CAST(<col> AS INTEGER)`. For `winPct` (REAL in the DB), `passerRating` (REAL), `passYdsPerAtt` (REAL), this drops the fractional part silently — `0.523` becomes `0`, `121.4` becomes `121`.
**Vulnerability:** First-match-wins iteration (`for sd in DomainKnowledge.STATS.values()`) returns `cast_type` for whichever StatDef happens to live in that column slot, NOT the column the caller asked about. There is no key match — this code is broken; it returns `INTEGER` for the very first stat in the dict if none of them have `column == col` (which is the common case for non-registry columns).
**Impact:** Floats sorted as integers — top-10 lists become wrong, ties multiply, percentages collapse to zero. Hard to detect because the SQL still runs.
**Fix:** Build a `_COLUMN_CAST_TYPES: dict[str, str]` lookup at module load (union of `DomainKnowledge.STATS` cast_types plus a hand-maintained dict for structural columns), and return `_COLUMN_CAST_TYPES.get(col, "INTEGER")` from `_get_cast_type`. Better: also raise on unknown columns when `STRICT_MODE` is enabled, so the agent can self-correct.

### WARNING #3: `_resolve_best_direction` and `_apply_worst_guard` have first-match-wins bugs across columns shared by multiple StatDefs
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:454-471`
**Confidence:** 0.78
**Risk:** Multiple StatDef entries in `DomainKnowledge.STATS` share the same `column` value:
- `passTDs` is mapped from `"passing touchdowns"`, `"passing tds"`, `"pass tds"`
- `passYds` is mapped from `"passing yards"`, `"pass yards"` and only one of those has `efficiency_alt="passerRating"`
- `defSacks` is shared between `"sacks"` (defense) and `"team sacks"` (team)
- `defTotalYds`, `defPassYds`, `defRushYds` columns appear with `invert_sort=True` only in the team rows but the same column names exist in the individual `defensive_stats` table

The helpers walk `DomainKnowledge.STATS.values()` and return on the first match. Dict iteration order in Python 3.7+ is insertion order, which means the result depends on registry order, not on the actual table being queried. A `Query("offensive_stats").sort_by("passYds", "worst")` finds the first entry with column `passYds` — which happens to have an `efficiency_alt`. So far so good. But `Query("team_stats").sort_by("defSacks", "best")` finds the individual-defense `defSacks` entry first and reports `DESC` (correct for individuals). If the registry order ever flips, this silently inverts.
**Vulnerability:** The "domain knowledge" lookup is keyed by column name, not by `(table, column)`. There is no guard against ambiguity. The "Always require min games for 'worst'" branch on line 347 also adds `COUNT(*) >= 4` to ANY table — but the team-stats table is already aggregated per game and the `defensive_stats` table per game, so this is roughly fine, but for the `standings` table this would silently filter out small samples when there is only one row per team.
**Vulnerability ctd.:** `_apply_worst_guard` mutates `self._aggregates` by deleting the original column and adding the efficiency alt. If the caller had also set `_order_col` to the original column, the order col is reassigned (line 468), but if it was an alias of the original (e.g., user called `q.sort_by("passYds", "worst").select("passYds")`), the SELECT still references `passYds` while the aggregate is now `passerRating` — producing a dangling column reference at runtime.
**Impact:** Wrong sort direction on team-vs-individual stat overlap; broken SELECT clause when `worst` swaps in the efficiency alt while the user's explicit `select()` still names the original column.
**Fix:** Key the lookup by `(table, column)`. Either store StatDefs in `dict[(table, column), StatDef]` or add a `for sd in DomainKnowledge.STATS.values(): if sd.column == col and sd.table == self._table:`. Also, after `_apply_worst_guard` deletes the original column, scan `self._selects` and remove any references to the dropped column.

### WARNING #4: `Query.execute()` is synchronous and will block the event loop if invoked from async code
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:429-439`
**Confidence:** 0.85
**Risk:** `execute()` calls `utils.run_sql(...)` which calls `sqlite3.connect(...).execute(...)` synchronously. The class also exposes `execute_async()` for the correct path. Both methods are added to the LLM sandbox via the `Query` symbol exported in `build_agent_env`. If the agent's generated Python ever calls `q.execute()` from inside a coroutine (which it can — the sandbox `__builtins__` doesn't filter `await`), the blocking sqlite call runs on the bot's main event loop and stalls every other Discord interaction during the query.
**Vulnerability:** Two execution paths with similar names, no warning that `execute()` is blocking. The agent has no incentive to pick `execute_async()` — `execute()` returns `(rows, error)` which is a friendlier shape than `execute_async()`'s "raises on error".
**Impact:** Discord heartbeat timeouts under heavy queries; bot appears frozen for long-running owner-history queries.
**Fix:** Either remove `execute()` from the class entirely and force `execute_async()`, or rename the sync version to `_execute_sync` and never include it in the agent sandbox.

### WARNING #5: `_owner_games_cte` parameter binding count is hand-maintained and undocumented elsewhere
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:944-1012`
**Confidence:** 0.75
**Risk:** The CTE binds `params: list = [user] * 8` because there are 6 `CASE WHEN g.homeUser/winner_user = ?` references plus 2 `WHERE` references. The docstring (line 958) says "5x homeUser CASE: ... 1x winner_user CASE: ... 2x in WHERE clause" — that totals 8, but counting the actual CTE on lines 986-995, I count: 5x `homeUser = ?` (lines 986, 987, 988, 989, 992), 1x `winner_user = ?` (line 995), 2x WHERE (line 999) = 8 total. The math checks out, but if anyone adds a CASE branch without updating `[user] * 8`, every downstream `pythagorean_wins`, `home_away_record`, etc. will silently bind the wrong column to the wrong placeholder. There is no test pinning the count.
**Vulnerability:** Magic-number coupling between the CTE template and the param-list multiplier. There is no `assert sql.count("?") == len(params)` validation.
**Impact:** Silent data corruption — wrong owner's games attributed to the wrong user — if the CTE is ever edited.
**Fix:** Generate the CTE programmatically: `params: list = []; user_placeholders = []; for _ in range(6): user_placeholders.append("?"); params.append(user)` ... or at minimum add `assert cte.count("?") == len(params)` at the end of `_owner_games_cte`.

### WARNING #6: `improvement_leaders` JOIN is missing the `teamName` join key, will mismatch traded players
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:879-914`
**Confidence:** 0.82
**Risk:** The s2 subquery groups by `fullName` only (`GROUP BY fullName` on line 908), but the s1 subquery groups by `(fullName, teamName)`. The JOIN is `s1.fullName = s2.fullName`. If a player was traded between season1 and season2, s1 has them on team A, s2 has them as a single row aggregating across all teams in season2, and the join fires on the name match. Two players with the same fullName (which exists in Madden — duplicate "John Smith") will cross-join.
**Vulnerability:** Asymmetric GROUP BY between the two subqueries. The s1 subquery is keyed by team but s2 isn't, so the join semantics are nonsensical.
**Impact:** Wrong improvement totals. Cartesian explosion for any duplicate-name player.
**Fix:** Either drop teamName from s1's GROUP BY (and let the data join cleanly across teams) or add it to s2's GROUP BY and the JOIN condition.

### WARNING #7: `compare_seasons` uses `LIKE ?` with `%user_or_team%` and binds without escaping wildcards
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:865-876`
**Confidence:** 0.70
**Risk:** The bind value is `f"%{user_or_team}%"`. SQLite LIKE treats `%` and `_` as wildcards. A user/team string of `_andy_` or `Smith%` will match unintended rows. There is no `ESCAPE` clause and `_sanitize_input` is not even called here, so the raw value is interpolated into the LIKE pattern. Also, since the user has no way to write a literal underscore in a name, the LIKE permits substring matches that the caller probably did not intend (e.g., "Smith" matches "Smithson", "Goldsmith").
**Vulnerability:** No wildcard escaping, no `ESCAPE` clause. The same pattern exists in `abilities_query` line 755.
**Impact:** Wrong row matches; potentially expanded rowsets when names contain underscores (which API usernames frequently do per CLAUDE.md gotchas).
**Fix:** Either drop the LIKE in favor of exact match `(teamName = ? OR fullName = ?)`, or escape `%` and `_` and add `ESCAPE '\'`.

### WARNING #8: `draft_picks_query` increments `round_num + 1` with no clamp or type check
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:736`
**Confidence:** 0.72
**Risk:** `q.where("draftRound = ?", str(round_num + 1))`. The type hint says `int | None`, but if the agent passes a string ("3"), `round_num + 1` raises `TypeError` from inside the SQL builder, which propagates up as a 500 to the user. There's no validation, and the off-by-one (`round_num + 1`) is a documentation gap — callers expect 0-indexed rounds but the DB stores them 1-indexed. This will be a forever-trap for the agent unless documented in the docstring.
**Vulnerability:** Implicit indexing convention with no validation, no docstring, no test.
**Impact:** Crash on string input; silent off-by-one if the agent passes a 1-indexed round.
**Fix:** `q.where("draftRound = ?", str(int(round_num) + 1))` and document the convention in the docstring.

### OBSERVATION #1: `_VALID_TABLES` allowlist excludes tables the file's own helpers reference
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:236-240`
**Confidence:** 0.80
**Risk:** The frozenset omits `player_abilities`. Wait — actually it's there. The set includes `players, player_abilities, owner_tenure, player_draft_map`. But it omits `og`, `og_raw` (these are CTE names, not tables, OK). Also omits `team_stats`, which IS in the set. So `_VALID_TABLES` looks complete for the current callers. But it does NOT include `tsl_members`, which CLAUDE.md identifies as the identity registry. If the agent ever needs to query the identity registry, it cannot do so through `Query`. The fallback would be to call `_get_codex_intents()._resolve_team()` (line 1650), which is private cross-module access — a separate smell.
**Impact:** None today; future expansion path is closed.
**Fix:** Add `tsl_members` to `_VALID_TABLES` if/when the agent needs identity queries through the builder.

### OBSERVATION #2: No identity-resolution helpers from `tsl_members` are exposed
**Location:** entire file
**Confidence:** 0.75
**Risk:** CLAUDE.md says `tsl_members.get_alias_map()`, `get_known_users()`, `get_db_username_for_discord_id()` are the single source of truth for Discord ID → DB username mapping. None of these are imported or exposed in the QueryBuilder. The `resolve_user` helper on line 1640 delegates to `codex_utils.fuzzy_resolve_user`, not to `tsl_members`. So the agent cannot canonicalize a Discord snowflake to a DB username through this module — it has to do it externally and trust the result is already resolved.
**Vulnerability:** Identity resolution is a critical correctness layer per CLAUDE.md and it's not visible from the SQL builder. Any caller that forgets to pre-resolve hits the "API usernames have underscores/case mismatches" trap.
**Fix:** Import `build_member_db.get_db_username_for_discord_id` lazily and expose `resolve_db_user(discord_id)` in the sandbox. Document loudly in the file header that the user param to `h2h`, `owner_*`, etc. must be a DB username, not a Discord ID or display name.

### OBSERVATION #3: `Query.filter()` has no validation of unknown keys; falls through to `f"{key} = ?"` (line 497)
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:284-297, 480-497`
**Confidence:** 0.85
**Risk:** `_expand_filter` recognizes 6 keys (season, stage, team, user, pos, status) and falls through to `return f"{key} = ?", [val]` for anything else. The `key` comes from `**kwargs` in `filter()`, so the LLM can call `q.filter(**{"1=1 OR teamName": "anything"})` — Python won't allow non-identifier kwargs from `**` literal but ALLOWS them via `q.filter(**{"badkey": "v"})` if the dict-construction route is used. The fallthrough then produces `... WHERE badkey = ?` which is at worst a parse error (rejected by SQLite), but at best leaks column-name guessing through error messages (since `run_sql` returns the raw exception text on line 56 of `codex_utils.py`).
**Impact:** Information disclosure through error messages; parse errors instead of friendly validation failures.
**Fix:** Replace the fallthrough with `raise ValueError(f"Unknown filter key: {key!r}")`.

### OBSERVATION #4: `Query._wheres` raw clauses are interpolated in arbitrary order vs `_filters`, can produce duplicate predicates
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:386-403`
**Confidence:** 0.55
**Risk:** `where_clauses` is built first from `_filters`, then `_pos_filter`, then `_wheres` are extended. If a caller does `q.filter(season=6).where("seasonIndex = ?", 6)`, the resulting SQL has `seasonIndex = ? AND seasonIndex = ?` which still works but is wasteful and confusing. More dangerously, `q.where("seasonIndex >= 5")` (no params) is allowed and interpolated raw — the agent can bypass the `?` discipline whenever it wants.
**Impact:** Convention erosion; the moment the agent learns it can pass literal-laced strings, parameterization stops being enforced at all.
**Fix:** Either deprecate `where()` (force callers through `filter()`) or require at least one `?` in the clause (`assert clause.count("?") >= 1`).

### OBSERVATION #5: `summarize()` and `compare_datasets()` have O(n²) and string-coercion behaviors that will surprise the agent
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:1654-1708`
**Confidence:** 0.60
**Risk:** `summarize()` iterates dataset[0]'s columns and tries `float(v)` on every string in every row. For 10k-row datasets, this is fine, but for the agent doing per-week queries across multiple seasons it can be slow. `compare_datasets()` builds two dicts, then iterates the union of keys, accessing both dicts again for every key — fine for small N, but the `for field_name in r1` loop only iterates r1's fields (not r2's), so any field present only in dataset2 is silently dropped from the comparison.
**Impact:** Asymmetric dataset comparisons drop columns when the schemas don't match exactly.
**Fix:** Iterate `set(r1) | set(r2)` instead of just `r1`.

### OBSERVATION #6: `current_season()` falls back to hardcoded `6` when data_manager is missing
**Location:** `C:/Users/natew/Desktop/discord_bot/oracle_query_builder.py:1628-1631`
**Confidence:** 0.85
**Risk:** Per CLAUDE.md, the bot has 95+ Super Bowl seasons. Returning `6` as a fallback means any test or import-time call (when data_manager hasn't loaded) will silently produce season-6 SQL, which will return zero rows for the live season. The `current_week()` fallback returns `1`. Both should either raise loudly or call out to a more authoritative source (e.g., `MAX(seasonIndex) FROM standings`). The fallback is also inconsistent with `oracle_agent.py:431`, which uses `1` as the fallback. Two modules have two different magic fallbacks for the same value.
**Impact:** Silent wrong-season queries; the agent has no way to detect the fallback fired.
**Fix:** Raise `RuntimeError("data_manager not loaded; cannot determine current season")` and let the caller handle it. Pin the fallback values in one place if they must exist.

## Cross-cutting Notes

1. **The whole file's safety story rests on `codex_utils.run_sql`'s SELECT-only PRAGMA**, not on `Query`'s own discipline. That guard is solid against writes (verified at `codex_utils.py:39-56`), but it does NOT prevent UNION-based reads, schema reconnaissance via `sqlite_master`, or cross-table information disclosure. CRITICAL #1, #2, and #3 all exploit this gap. If the SELECT-only guard were ever removed or weakened, the entire `Query` API becomes a remote SQL execution surface.

2. **`_sanitize_input` is the wrong tool in the wrong layer.** It's a prompt-sanitizer applied to SQL parameter binds. Either move it to a prompt-assembly module and remove the call sites here, or replace it with a parameterization audit pass.

3. **Column-keyed lookups in `DomainKnowledge`** (`_get_cast_type`, `_resolve_best_direction`, `_apply_worst_guard`) all use `for sd in DomainKnowledge.STATS.values(): if sd.column == col`. Multiple StatDefs share the same column. Key by `(table, column)` to fix this entire class of bug at once.

4. **The agent sandbox in `oracle_agent.py:472-549` exposes the entire `Query` class plus 30+ helper functions** with no restriction on which methods can be called. If `Query` is to remain in the sandbox, it needs hardening (CRITICAL #1 and #2). Alternatively, expose only the high-level domain functions (which take typed params) and remove `Query` itself from the sandbox — this trades flexibility for safety.

5. **No tests are referenced** by the file (no `if __name__ == "__main__":` block, no doctest), and there is a `test_query_builder` import-only consumer mentioned in the task header but no test file exists in `audits/` to verify the assumptions in this audit. Test coverage for the parameterization invariants would catch CRITICAL #3 immediately.
