# Adversarial Review: codex_intents.py

**Verdict:** needs-attention
**Ring:** orphan (LIVE — imported by codex_cog.py, oracle_cog.py, oracle_query_builder.py, test_oracle_stress.py)
**Reviewed:** 2026-04-09
**LOC:** 1372
**Reviewer:** Claude (delegated subagent)
**Total findings:** 24 (3 critical, 9 warnings, 12 observations)

## Summary

The intent-detection layer is a first-pass regex pipeline that rushes user-supplied names straight into SQL templates with largely parameterized bindings — but several intents inject resolved names as LIKE wildcards or interpolate format strings into SQL bodies, and regex precedence has several ambiguous/unreachable branches that will mis-route queries silently. Off-by-one risks appear in season-math, and the registry ordering causes `h2h_record` to shadow `game_score`, `streak`, `recent_games`, and other team queries in ways that were probably not intended. This file is not "safe because SELECT-only" — an attacker who knows the team-alias set can poison `_resolve_team` lookups, and the `%LIKE%` bindings mean names containing SQL wildcard characters (`_`, `%`) will silently broaden queries.

## Findings

### CRITICAL #1: `get_h2h_sql_and_params` string-concats `dm.WEEK_LABEL_SQL` into SQL template

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:173-188`
**Confidence:** 0.85
**Risk:** Raw string concatenation of a module-scoped SQL fragment (`dm.WEEK_LABEL_SQL`) into a user-facing SQL template. If `data_manager.WEEK_LABEL_SQL` is ever populated from an external source, loaded from config, or modified by another cog at runtime, it becomes an injection vector into every H2H query issued by codex, oracle, and the sportsbook H2H surface.
**Vulnerability:** The parameterization contract is broken. Line 180 does `... || """ + dm.WEEK_LABEL_SQL + """ || ...` — this is the one place in this file where a non-literal string is concatenated into the SQL body without `?` binding. `dm` is imported module-level (line 27) and the file doesn't validate the shape of `WEEK_LABEL_SQL`. More practically: if `dm` is `None` (ImportError path at line 28), this function will raise `AttributeError` at runtime — NOT at import time — and every downstream H2H call (`oracle_cog.H2HModal`, `codex_cog._h2h_impl`, intent-detection) crashes.
**Impact:** (a) Latent crash if `data_manager` import fails and the graceful fallback on line 77 (`_current_season`) tricks callers into thinking `dm=None` is survivable; (b) Long-term injection surface if `WEEK_LABEL_SQL` ever becomes dynamic.
**Fix:** Inline the week-label expression as a literal in the SQL (it's static per the codebase convention), OR guard `dm is not None` at the top of the function and raise a clear error. Never allow dynamic SQL-fragment concatenation even from "trusted" internal modules in a file that also accepts user input.

---

### CRITICAL #2: Resolved names injected as `%name%` LIKE patterns without escaping

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:790, 972-973, 1067, 1263, 1283, 1310, 1322, 1325`
**Confidence:** 0.90
**Risk:** Every `LIKE` predicate takes a user-derived name (team name, player name, owner name) and formats it with `f"%{...}%"` before binding. If the resolved value contains `%` or `_` — which is allowed in API usernames and, critically, in user-supplied text that failed `_resolve_team()` — those characters function as LIKE metacharacters and will broaden the query to an unbounded set or match unintended rows.
**Vulnerability:** Most affected sites:
  - `draft_history` line 790: `params_list = [f"%{team_name}%"]` — team_name comes from `_resolve_team()` which returns canonical, but the LIKE wildcard pattern is still vulnerable if `_TEAM_ALIASES` is ever extended with user content.
  - `trade_history` lines 972-973: `[f"%{team_name}%", f"%{team_name}%"]` — same.
  - `owner_history` line 1067: `[f"%{team_name}%"]` — same.
  - `roster_query` lines 1263, 1283: Team name from resolver.
  - `player_abilities_query` line 1310: team path.
  - `player_abilities_query` line 1322-1325: **`player_name = groups[0].strip()` is user-supplied raw text** passed unescaped into `LIKE '%{player_name}%'` via parameter binding. A query like "what abilities does J%_ have" will match every player whose first+last name contains "J" followed by any single char — silently wrong results.
**Impact:** (1) Attacker (or curious user) can use `%` and `_` in the natural language query to broaden player/team lookups and exfiltrate data that wouldn't be visible through normal intent flow. (2) Teams whose names genuinely contain "_" (some API team names) will match unrelated teams. (3) Silent wrong-data returns for legitimate users who happen to type percent signs.
**Fix:** Sanitize LIKE patterns before binding: `escaped = name.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')` then bind with `ESCAPE '\'` clause in the SQL: `... LIKE ? ESCAPE '\\'`. Apply at every `f"%{...}%"` site. For player-name lookups, consider splitting to first/last and using exact-match on the already-split columns instead of a concatenated LIKE.

---

### CRITICAL #3: `h2h_record` regex pattern #4 shadows `recent_games`, `game_score`, `streak`, and most team intents

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:273-282, 1339-1346`
**Confidence:** 0.80
**Risk:** The intent registry is iterated in registration order (line 1339), and `h2h_record` is the FIRST registered intent. Its pattern #4 — `r'\b(\S+)\s+(?:vs\.?|versus)\s+(\S+)(?:\s+record|\s+h2h|\s+head)?\b'` — matches ANY `X vs Y` substring. `_build_h2h` only returns None if BOTH captured tokens resolve to a team (line 309-310), but `_resolve_team` uses **exact key lookup**, so `Lions vs Packers` → team1='lions', team2='packers' both hit → fallthrough to `game_score` (correct). HOWEVER: `Lions vs JT` / `Killa vs Packers` will pass the stop-word check, not be teams, and get classified as H2H with one owner name and one team string as the "owner" — passing nonsense to `get_h2h_sql_and_params`. The H2H query will then resolve "Lions" via fuzzy resolver and possibly match an owner whose name contains "Lion".
**Vulnerability:** The precedence guard "is either side a team?" only rejects when BOTH are teams. Mixed queries (team vs user) silently route to the wrong intent. Also, pattern #4's `\S+` captures include punctuation — "Witt's vs Killa's" would capture `Witt's` and `Killa's`, and `_resolve_name` would fail, causing the raw strings to be bound into the SQL with no match (silent empty result).
**Impact:** (1) Users asking "Lions vs JT" get a meaningless H2H record between a fuzzy-resolved "Lions" and JT, or an empty result, when they clearly meant "show me game scores between the Lions and JT's team". (2) `recent_games`, `streak`, and even `player_stats` intents that should handle some of these queries never get tried because `h2h_record` matches first.
**Fix:** Reorder the registry so `game_score` and team-based intents match before `h2h_record`, OR tighten the h2h pattern to require explicit owner markers (`'s`, `my`, `i`). Also, extend the bail-out at line 309-310 to fail if EITHER side resolves to a team (not both), falling through to `game_score`.

---

### WARNING #1: `_extract_season` matches `s5` inside ordinary words

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:80-96`
**Confidence:** 0.80
**Risk:** Pattern `r'(?:season|s)\s*(\d+)'` will match `s 5`, `s5` — but with `\s*` it also matches substrings like `has 5 wins` (whitespace-zero between the "s" of "has" and the digit). Actually: `\s*` allows zero whitespace, and the regex uses `re.IGNORECASE` but no word-boundary. Testing: `most TDs 2025` → doesn't match. `has 5 wins` → matches `s 5` (the "s" at the end of "has" followed by space and digit) → season=5. This is a textbook off-by-one/over-match.
**Vulnerability:** No `\b` word boundary before the non-capturing group. `s` alone is a common English letter boundary.
**Impact:** Queries like "how many TDs does he have after 3 games" or "has 7 wins" get silently season-filtered to S3 or S7, producing incomplete results without user feedback.
**Fix:** Change pattern to `r'\b(?:season|s)\s*(\d+)\b'` — add word boundaries on both sides and require at least minimal separation.

---

### WARNING #2: `_extract_season` for "last season" returns 0 or negative in early seasons

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:94-95`
**Confidence:** 0.90
**Risk:** `return _current_season() - 1`. If `dm` failed to import, `_current_season()` returns `6` (line 77 default), so "last season" = 5 — OK. But if `dm.CURRENT_SEASON` is 1 (new league), "last season" = 0, which produces `WHERE seasonIndex = '0'` — empty result, silently. No bounds check.
**Impact:** In early-season or fresh-league states, "last season" queries return empty results silently.
**Fix:** `return max(1, _current_season() - 1)` OR return None and let the caller explain "there is no previous season yet."

---

### WARNING #3: `_current_season()` hardcoded fallback of `6` drifts from reality

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:76-77`
**Confidence:** 0.95
**Risk:** `return dm.CURRENT_SEASON if dm else 6`. Per CLAUDE.md the league is on season 95+. The fallback is a hardcoded stale value from early development. If `data_manager` import fails for any reason (circular import, startup ordering issue), every intent that defaults to the current season will return data from S6, silently.
**Vulnerability:** The `except ImportError: dm = None` guard on line 28 deliberately allows this module to load without `dm`, so the fallback path IS reachable. It's a silent fallback to wrong data rather than a failure.
**Impact:** Silent wrong-season data when `dm` is unavailable.
**Fix:** If `dm is None`, raise `RuntimeError("data_manager required for season resolution")` rather than returning a stale literal. Alternatively, raise at import time — don't half-load.

---

### WARNING #4: `_build_season_record` drops "last season" semantics when capture group is also present

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:326-337`
**Confidence:** 0.75
**Risk:** Patterns include `r"\b(?:my|(\S+?)(?:'s)?)\s+record\s+(?:this|in|for|last|previous)?\s*(?:season|s)\s*(\d+)?"`. For "my last season record" the pattern matches, groups = [None, None] (no explicit number), and `_extract_season(question)` falls through to matching "last season" → `_current_season() - 1`. BUT for "my record last season", the pattern also matches, but only if the optional "last" qualifier is placed before "season" — the second capture group `(\d+)?` is None, so `groups` is empty after filtering, owner defaults to caller_db. `_extract_season(question)` should then match `last season` → S-1. OK so far. But if the user says "my record last", the pattern fails entirely and falls through to `alltime_record` — actually probably correct. The risk is the overlap isn't tested: the test in `_build_season_record` only uses the extracted digit from the pattern as a filter on the owner list, never on season.
**Vulnerability:** `_extract_season` is called separately and may disagree with what the pattern matched.
**Impact:** Subtle off-by-one where pattern intent and season extraction disagree.
**Fix:** If the pattern captured a digit in group 2, use that; otherwise call `_extract_season`. Currently the group-2 digit is ignored entirely.

---

### WARNING #5: `_build_recent_games` — "my" detection relies on `groups[0] is None`, breaks with pattern order

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:589-615`
**Confidence:** 0.75
**Risk:** `used_my = bool(re.search(r'\bmy\b', question, re.IGNORECASE)) and groups[0] is None`. The `groups[0] is None` check assumes the FIRST capture group is always the owner name slot. But patterns are structured differently — pattern 1 has `(?:my|(\S+?)(?:'s)?)` at group 1, count at group 2, opponent at group 3. Patterns 2-5 have only the owner slot and optional count/opponent. If the regex engine backtracks or an alternate pattern fires, `groups[0]` may not be the owner slot.
**Vulnerability:** More concretely: for pattern 4 `r"\b(?:my|(\S+?)(?:'s)?)\s+(?:last|most\s+recent)\s+game\b"`, group 0 is the owner. For pattern 5 `r"\b(?:my|(\S+?)(?:'s)?)\s+games?\s+(?:vs\.?|against|versus)\s+(\S+)"`, group 0 is owner and group 1 is opponent. The detection relies on this positional contract holding across all 5 patterns.
**Impact:** In patterns where `my` is not in group 0, `used_my` will be wrong and `count` may be treated as an owner name → fuzzy resolver attempts to resolve the digit string → None → raw digit becomes the owner → silent empty result.
**Fix:** Use `match.re.pattern` to identify which pattern matched, or explicitly test which group captured "my" via a named group. Current positional logic is fragile.

---

### WARNING #6: `_build_h2h` resolves owner via fuzzy resolver, then falls back to raw name

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:313-314`
**Confidence:** 0.80
**Risk:** `owner1 = _resolve_name(owner1, resolved_names) or owner1`. If neither the alias map nor the fuzzy resolver finds a match, the raw regex capture is bound into the SQL `winner_user = ?`. Per CLAUDE.md: "API usernames have underscores/case mismatches. Use `_resolve_owner()` fuzzy lookup." — but when fuzzy lookup fails, the raw text from the user message gets passed directly.
**Vulnerability:** User types "tunagod123" → not in alias map, fuzzy returns None → `owner1 = "tunagod123"` → SQL looks for `winner_user = 'tunagod123'` → silently returns zero games. User has no idea the resolver failed — they just see "0-0 all-time". Same for the other ~10 intents that use this pattern.
**Impact:** Silent empty-result failures for any name the resolver can't match. No error, no fallback to Tier 2/3.
**Fix:** If both `_resolve_name` and the alias lookup fail, return `None` (fall through to Tier 3 AI) rather than binding the raw capture.

---

### WARNING #7: Stop-word filter inside `_build_streak` is nested inside function, rebuilt on every call

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:663-665`
**Confidence:** 0.60
**Risk:** The set `_STREAK_STOP` is defined inside the function body, so it's re-created on every regex match. Same for `_STOP_WORDS` in `_build_h2h` at line 302. This is on the hot path for oracle queries.
**Vulnerability:** Minor performance; also a smell — these are constants that belong at module scope next to `_WORD_NUMS`.
**Impact:** Negligible per-call cost, but multiplied across oracle hot paths it adds up, and it's inconsistent with the module's other constant definitions.
**Fix:** Hoist both sets to module scope.

---

### WARNING #8: `_build_team_record` uses `winner_team`/`loser_team` columns that may not exist

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:749-751`
**Confidence:** 0.70
**Risk:** The SQL references `winner_team` and `loser_team`. These are derived columns — other intents (`h2h_record`, `season_record`, `alltime_record`, `recent_games`) use `winner_user`/`loser_user` exclusively. The team columns are only referenced by `team_record`, `game_score` (line 830, 841), and `playoff_results` (line 875-884).
**Vulnerability:** If `tsl_history.db` on older deployments doesn't have the `winner_team`/`loser_team` view/column (e.g. the migration that added them hasn't run), this intent silently crashes. No try/except at the call site.
**Impact:** Team record queries raise `sqlite3.OperationalError: no such column` uncaught → user-facing traceback.
**Fix:** Add a schema check at module load or wrap the team-column queries in try/except and fall through to computing from `homeTeamName`/`awayTeamName` + scores.

---

### WARNING #9: `_build_roster_query` iterates `_TEAM_ALIASES` substring match, vulnerable to "no" = Saints

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:1249-1253`
**Confidence:** 0.90
**Risk:** The docstring at line 138 explicitly says "Uses exact key match only — no substring matching to avoid false positives like 'was' → Commanders or 'no' → Saints." But this code path does `if alias in text_lower` — substring match — which is exactly the false-positive case the helper was written to avoid.
**Vulnerability:** "best QB now" → `alias='no'` is in `text_lower` → team_name='Saints' → filters roster to Saints QBs silently. Same for "best QB in the east" → `alias='ten'` (Titans) is not in "east" but `alias='sea'` could match "season". There are many short 2-3 char aliases.
**Impact:** Silent wrong-team filtering on roster queries.
**Fix:** Use `_resolve_team()` on extracted team-name candidates instead of substring iteration, or gate the substring match to aliases ≥ 4 chars and require word boundaries.

---

### OBSERVATION #1: Dead pattern — `alltime_record` group-extraction can't see owner name from "my record" pattern

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:375-393`
**Confidence:** 0.70
**Vulnerability:** Pattern 4 `r'\b(?:my)\s+record\b(?!\s+(?:this|last|in|season|vs|against|versus))'` has ZERO capture groups. `match.groups()` returns `()`. The `for g in groups:` loop is skipped, and `owner = None`, so `owner = caller_db`. This works, but pattern 4 is the ONLY one that works for "my" cases on this intent — patterns 1-3 all have `(?:my|(\S+?)(?:'s)?)` which means "my" is the non-capturing branch, and groups are empty anyway. The fall-through to `caller_db` is the only reason `alltime_record` works for "my". This is correct but brittle — easy to regress.
**Impact:** None today; fragile.
**Fix:** Add a comment documenting the non-capturing "my" branch behavior.

---

### OBSERVATION #2: `_WORD_NUMS` defined AFTER its first reference (line 91)

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:91, 99-103`
**Confidence:** 0.95
**Risk:** `_extract_season` at line 91 references `_WORD_NUMS`, but `_WORD_NUMS` is defined at line 99 (after the function). This works because Python resolves module globals at call time, not definition time. But it's confusing and a style issue — standard convention is to define module-level constants before functions that use them.
**Impact:** None runtime; code-organization smell.
**Fix:** Move `_WORD_NUMS` above `_extract_season`.

---

### OBSERVATION #3: `_TEAM_ALIASES` likewise defined after `_resolve_team`

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:134-141, 695-728`
**Confidence:** 0.95
**Risk:** `_resolve_team` at line 134 references `_TEAM_ALIASES`, but `_TEAM_ALIASES` is defined at line 695. Same deferred-lookup pattern. Works because `_resolve_team` is only called from within build functions invoked at regex-match time (after module load), but the reader has to scroll ~560 lines to find the definition. The comment at 136 even says "defined below" — acknowledging the smell.
**Impact:** Maintainability; documentation scattered.
**Fix:** Move `_TEAM_ALIASES` near the top with the other helpers, OR move `_resolve_team` to just below `_TEAM_ALIASES`.

---

### OBSERVATION #4: `_lookup_stat` and `STAT_REGISTRY` duplicate logic in `_build_leaderboard`

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:202-253, 521-541`
**Confidence:** 0.80
**Risk:** `STAT_REGISTRY` provides a canonical stat_key → (table, column, agg, pos) mapping, but `_build_leaderboard` defines its own inline `stat_map` with DIFFERENT keys ('pass', 'rush', 'receiv', 'tackl', 'sack', 'intercept') and DIFFERENT tuple shapes (adds `worst_col` for efficiency metrics). This means a new stat added to `STAT_REGISTRY` won't be picked up by the leaderboard intent, and vice versa.
**Impact:** Two sources of truth diverge over time.
**Fix:** Extend `STAT_REGISTRY` with a `worst_col` slot and use it from the leaderboard builder too. Delete the inline `stat_map`.

---

### OBSERVATION #5: `_STAT_KEYS_SORTED` computed at module load, but STAT_REGISTRY is mutable

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:243`
**Confidence:** 0.70
**Risk:** `_STAT_KEYS_SORTED = sorted(STAT_REGISTRY.keys(), ...)` is computed once at import. If `STAT_REGISTRY` is extended at runtime (e.g. `oracle_query_builder.py` imports it and adds entries — line 99 of that file references `codex_intents.STAT_REGISTRY`), `_lookup_stat` will miss the new keys.
**Impact:** Cross-module contract drift.
**Fix:** Make `_STAT_KEYS_SORTED` a property or compute lazily on first `_lookup_stat` call.

---

### OBSERVATION #6: `_INTENT_REGISTRY` type annotation uses lowercase `callable`

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:259`
**Confidence:** 0.95
**Risk:** `list[tuple[str, list[re.Pattern], callable]]` — `callable` is the built-in function, not the `Callable` type. This is an invalid type annotation and any static type checker (mypy, pyright) will flag it. Discord.py 2.3 + Python 3.14 should be running modern type-check tools.
**Impact:** Type-check noise; masks real contract errors.
**Fix:** Use `typing.Callable` or `collections.abc.Callable`.

---

### OBSERVATION #7: `check_self_reference_collision` is never called from the intent detection flow

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:145-161, 1352-1372`
**Confidence:** 0.90
**Risk:** The function is a public API (imported by `codex_cog.py` line 69), but `detect_intent` itself doesn't invoke it. If codex_cog calls `detect_intent` and forgets to also call `check_self_reference_collision`, the "Witt record vs Witt" case produces a nonsense H2H result. The API contract is disconnected.
**Impact:** Caller-side error prone; easy to forget.
**Fix:** Either call `check_self_reference_collision` inside `detect_intent` and return an error IntentResult, or clearly document in the module docstring that callers MUST call it separately.

---

### OBSERVATION #8: `_build_draft_history` pattern #1 is extremely broad and overlaps `game_score`, `team_record`

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:767-772`
**Confidence:** 0.80
**Risk:** Pattern `r'\b(?:who\s+did\s+)?(?:the\s+)?(\w+(?:\s+\w+)?)\s+draft\b'` fires on ANY query containing "X draft" where X is 1-2 words. "Lions draft" correctly matches. "NFL draft" — "NFL" fails `_resolve_team` → returns None → falls through. But "best draft class" → captures "best draft class", resolves "best" to None, returns None. OK so far. But "top of the draft" → fails. The broader concern is that any two-word phrase before "draft" ending in a team alias leaks into this intent before other intents can claim it.
**Impact:** Minor at current intent count; grows as new intents are added.
**Fix:** Require explicit anchor words like "picks" or "class" to qualify for draft_history; bare "draft" should fall to Tier 3.

---

### OBSERVATION #9: `_build_player_stats` pattern #1 `(\w[\w\s]*)` is ungreedy and over-captures

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:902-908`
**Confidence:** 0.70
**Risk:** `r'\b(?:who\s+(?:has|leads?|is\s+leading)\s+(?:the\s+)?(?:most|highest|league\s+in))\s+(\w[\w\s]*)'` — the capture group `(\w[\w\s]*)` is greedy and will swallow everything to end of string. "who has the most passing yards this season" → captures "passing yards this season" → passed to `_lookup_stat` which finds "passing yards" via longest-match, but the meta.stat is set to the key, not the captured string. Works today but fragile.
**Impact:** Brittle pattern; relies on `_lookup_stat` internals.
**Fix:** Tighten the capture to `(\w+(?:\s+\w+)?)` or trim with `.strip()` before lookup.

---

### OBSERVATION #10: `_normalize_question` strips possessives only before a fixed keyword list

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:120-131`
**Confidence:** 0.70
**Risk:** The regex lists specific keywords (`record|draft|stats?|games?|streak|roster|abilities|x-factor|trades?|wins?|losses?|history|offense|defense|players?|picks?|team|schedule`). Missing: `standings`, `lineup`, `contract`, `cap`, `salary`, `waiver`, `depth chart`, `ability`, `dev`. So "Witt's standings" won't normalize and the standings intent regex won't match.
**Impact:** Limited recall for non-keyword queries.
**Fix:** Use a more general possessive-strip: `r"(\w+)'s?\s+"` applied before any regex matching, not gated on a keyword.

---

### OBSERVATION #11: `_resolve_team` normalizes via `.strip().lower()` but `_TEAM_ALIASES` has mixed-case keys

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:134-141, 695-728`
**Confidence:** 0.95
**Risk:** `_TEAM_ALIASES` keys are all lowercase (verified lines 696-727). Good. But the function docstring at 137-139 says "Uses exact key match only" — meaning any team name with spaces or punctuation won't match. Verified: "green bay" is in the dict lowercase, but `_build_roster_query` at line 1272 uses `_resolve_team(groups[0])` where groups[0] comes from `(\w+(?:\s+\w+)?)` — so "green bay" would match as 2 words. But "los angeles" isn't a key; "la rams" is. "New York Giants" → lowercase "new york giants" — not in dict. Only "nyg" and "ny giants" are keys.
**Impact:** Recall gaps for common multi-word team references.
**Fix:** Add multi-word variants for all LA, NY, Tampa Bay, Kansas City, New England, New Orleans, San Francisco, Green Bay teams. Or compute aliases from a canonical list.

---

### OBSERVATION #12: No docstring on public `detect_intent`; dead "Tier 2" comments

**Location:** `C:/Users/natew/Desktop/discord_bot/codex_intents.py:1352-1372`
**Confidence:** 0.90
**Risk:** The module header (lines 1-16) advertises a "Three-tier query pipeline" with Tier 2 = "AI structured classification", but `detect_intent` at line 1352 has a terse docstring saying "Two-tier intent detection (v3)" and the comment "(old Tier 2 Gemini classification deprecated)". The module header is stale.
**Impact:** Documentation drift confuses readers and future agents.
**Fix:** Update the module docstring to reflect v3 two-tier reality. Remove Tier 2 references throughout.

---

## Cross-cutting Notes

1. **Silent empty-result failures** are the dominant failure mode of this file. Almost every intent defaults to `caller_db` when the capture fails, binds raw text when the resolver fails, and never returns "I don't understand" — callers get empty SQL results and AIs downstream must interpret them. The whole module should prefer returning `None` (fall-through to Tier 3) over "best-guess with raw text" for identity resolution failures.

2. **Regex precedence is order-dependent** and fragile. The registration order in `_INTENT_REGISTRY` directly controls which intent wins, and several intents (h2h, game_score, recent_games, team_record) have overlapping patterns that only work because of specific registration ordering. A new contributor adding an intent could silently shadow existing ones. Consider adding an explicit priority field to `_register()` or writing pytest regression tests that pin the matched intent for a corpus of example queries.

3. **LIKE-pattern injection** is systemic. Every `LIKE '%...%'` parameter binding should be escaped consistently. A single helper `_like_escape(s)` applied at every call site would close this class of bug.

4. **STAT_REGISTRY and leaderboard's inline stat_map divergence** will bite as stats are added. The two sources of truth problem should be resolved before the next stat-type expansion (e.g. special teams, red-zone, third-down).

5. **The `dm` import guard is a footgun** — it allows the module to load with `dm=None` but nothing downstream is safe against that. Either make `dm` mandatory (raise at import) or make every `dm.X` access defensive.
