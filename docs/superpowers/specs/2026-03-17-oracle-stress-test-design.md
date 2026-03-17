# Oracle Intent Expansion & Stress Test — Design Spec

## Context

The ATLAS Oracle query system uses a 3-tier intent pipeline (Regex → Gemini Classification → NL→SQL) to answer natural language questions about TSL history. Currently 8 intents cover basic queries (H2H, season/alltime records, leaderboards, streaks, team records, draft history, recent games). A 10-question stress test validates these.

**Problem:** Many common question types (playoffs, player stats, trades, scores, roster lookups, standings) fall through to unreliable Tier 3 NL→SQL. Several DB tables (`trades`, `standings`, `players`, `player_abilities`) have zero intent coverage. The `team_stats` table does not exist in production (not fetched by `sync_tsl_db()`). Regex edge cases (contractions, word numbers, "last season") also fail silently.

**Goal:** Expand from 8 → 18 intents covering all queryable tables. Add a stat registry for future extensibility. Expand the stress test from 10 → 40+ cases. Every test must resolve at Tier 1 or Tier 2 — no Tier 3 fallback.

---

## Helper Function Enhancements

All changes in `codex_intents.py`.

### `_normalize_question(text)` — NEW

Called at the top of `_match_regex()` (before iterating the registry) and at the top of `detect_intent()` (before Tier 2 classification). The normalized text is passed through the entire pipeline — regex patterns are written against normalized input.

Transformations:
- Expand contractions: `what's` → `what is`, `who's` → `who is`, `how's` → `how is`
- Strip possessives before intent keywords: `Lions' record` → `Lions record` (only before: record, draft, stats, games, streak, roster, abilities)
- Normalize whitespace

**Insertion point:** First line of `_match_regex(question, ...)` should be `question = _normalize_question(question)`. Similarly, first line of `detect_intent()` after parameter extraction.

### `_extract_season(text)` — ENHANCE

Add support for:
- `last season` / `previous season` → `_current_season() - 1`
- Keep existing `season N` and `this season` / `current season` handlers

### `_extract_limit(text, default)` — ENHANCE

Add word-number support via `_WORD_NUMS` dict:
```
one=1, two=2, three=3, four=4, five=5, six=6, seven=7, eight=8, nine=9, ten=10, fifteen=15, twenty=20
```
Second regex: `r'(?:top|last|recent)\s+(one|two|three|...)'` after the existing digit regex.

### `_resolve_team(text)` — EXTRACT

Extract team alias lookup from `team_record`/`draft_history` into a shared helper. Reuse in all team-based intents: `trade_history`, `game_score`, `team_stats`, `standings_query`, `roster_query`, `player_abilities_query`, `owner_history`.

### `STAT_REGISTRY` — NEW

A centralized dict mapping natural language keywords → `(table, column, aggregation, pos_filter)`:

```python
STAT_REGISTRY = {
    # Passing
    'passing yards': ('offensive_stats', 'passYds', 'SUM', 'QB'),
    'passing tds': ('offensive_stats', 'passTDs', 'SUM', 'QB'),
    'pass tds': ('offensive_stats', 'passTDs', 'SUM', 'QB'),
    'interceptions thrown': ('offensive_stats', 'passInts', 'SUM', 'QB'),
    'passer rating': ('offensive_stats', 'passerRating', 'AVG', 'QB'),
    'completions': ('offensive_stats', 'passComp', 'SUM', 'QB'),
    'completion percentage': ('offensive_stats', 'passCompPct', 'AVG', 'QB'),
    # Rushing
    'rushing yards': ('offensive_stats', 'rushYds', 'SUM', None),
    'rushing tds': ('offensive_stats', 'rushTDs', 'SUM', None),
    'rush yards': ('offensive_stats', 'rushYds', 'SUM', None),
    'rush tds': ('offensive_stats', 'rushTDs', 'SUM', None),
    'fumbles': ('offensive_stats', 'rushFum', 'SUM', None),
    # Receiving
    'receiving yards': ('offensive_stats', 'recYds', 'SUM', None),
    'receiving tds': ('offensive_stats', 'recTDs', 'SUM', None),
    'receptions': ('offensive_stats', 'recCatches', 'SUM', None),
    'catches': ('offensive_stats', 'recCatches', 'SUM', None),
    'drops': ('offensive_stats', 'recDrops', 'SUM', None),
    'yards after catch': ('offensive_stats', 'recYdsAfterCatch', 'SUM', None),
    # Defense
    'tackles': ('defensive_stats', 'defTotalTackles', 'SUM', None),
    'sacks': ('defensive_stats', 'defSacks', 'SUM', None),
    'interceptions': ('defensive_stats', 'defInts', 'SUM', None),
    'forced fumbles': ('defensive_stats', 'defForcedFum', 'SUM', None),
    'fumble recoveries': ('defensive_stats', 'defFumRec', 'SUM', None),
    'defensive tds': ('defensive_stats', 'defTDs', 'SUM', None),
    'deflections': ('defensive_stats', 'defDeflections', 'SUM', None),
    'pass deflections': ('defensive_stats', 'defDeflections', 'SUM', None),
}
```

Both `leaderboard` and `player_stats` intents consume this registry. New stats = one-line additions.

**Lookup strategy:** Use longest-match-first when resolving user text against registry keys. Sort keys by length descending before matching to prevent "interceptions" from matching before "interceptions thrown". This avoids the defensive stat being returned when the user meant the offensive stat (and vice versa).

**Note on `extendedName`:** All player stat queries should use `extendedName` (e.g., "Jalen Hurts") for display and filtering, NOT `fullName` (e.g., "J.Hurts"). The existing `leaderboard` intent uses `fullName` in its SQL — this should also be updated to `extendedName` for consistency.

---

## New Intents (10 new, 18 total)

### Intent 9: `playoff_results`

**Patterns:**
- `who won the super bowl [in season N]`
- `super bowl results/winners/history`
- `playoff results/games/scores [season N]`
- `championship game scores/results`

**SQL — Super Bowl query** (`stageIndex >= 200`):
```sql
SELECT seasonIndex, homeTeamName, awayTeamName, homeScore, awayScore,
       homeUser, awayUser, winner_team, winner_user
FROM games WHERE status IN ('2','3') AND CAST(stageIndex AS INTEGER) >= 200
  [AND seasonIndex = ?]
ORDER BY CAST(seasonIndex AS INTEGER) DESC
```

**SQL — General playoffs** (`stageIndex >= 2`):
```sql
SELECT seasonIndex, weekIndex, stageIndex, homeTeamName, awayTeamName,
       homeScore, awayScore, winner_team, winner_user
FROM games WHERE status IN ('2','3') AND CAST(stageIndex AS INTEGER) >= 2
  [AND seasonIndex = ?]
ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC
LIMIT ?
```

**Builder logic:** "super bowl"/"championship"/"title" → stageIndex >= 200. "playoff" → stageIndex >= 2.

### Intent 10: `player_stats`

**Patterns:**
- `who has/leads the most {stat_keyword} [all-time|this season]`
- `{player_name}'s career/season stats`
- `{player_name}'s stat line`
- `top N in {stat_keyword}`

**SQL (aggregate player stats):**
```sql
SELECT extendedName AS player_name, teamName,
       {agg}(CAST({column} AS {type})) AS stat_value
FROM {table}
WHERE stageIndex = '1'
  [AND pos = ?]
  [AND seasonIndex = ?]
  [AND extendedName LIKE ?]
GROUP BY extendedName
ORDER BY stat_value DESC
LIMIT ?
```

**Important:** Use `extendedName` (e.g., "Jalen Hurts") NOT `fullName` (e.g., "J.Hurts") for player name display and filtering. The `extendedName` column stores the full readable name.

**Builder logic:** Look up stat keyword in `STAT_REGISTRY` using longest-match-first to avoid "interceptions" matching "interceptions thrown" (offensive) instead of "interceptions" (defensive). If individual player name detected, add `extendedName LIKE` filter. For `AVG` aggregations (passer rating), use `REAL` type. For `SUM`, use `INTEGER`.

### Intent 11: `trade_history`

**Patterns:**
- `what trades did {team} make`
- `{team} trades [this/last season]`
- `trades this/last season`
- `recent trades`
- `trade history`

**SQL:**
```sql
SELECT team1Name, team2Name, seasonIndex, team1Sent, team2Sent
FROM trades WHERE status IN ('approved', 'accepted')
  [AND (team1Name LIKE ? OR team2Name LIKE ?)]
  [AND seasonIndex = ?]
ORDER BY CAST(seasonIndex AS INTEGER) DESC
LIMIT ?
```

**Note:** Both `'approved'` and `'accepted'` represent completed trades in the DB.

### Intent 12: `team_stats`

**Patterns:**
- `which/what team has the best/worst/most/least {offense|defense|turnovers|points}`
- `{team} offense/defense stats`
- `which team scores the most points`

**Important:** The `team_stats` table does NOT exist in production (not fetched by `sync_tsl_db()`). Use the `standings` table instead, which contains `offTotalYds`, `defTotalYds`, `ptsFor`, `ptsAgainst`, `tODiff`. Note: `standings` is current-season only, so historical team stats queries are not supported.

**SQL:**
```sql
SELECT teamName,
       CAST(offTotalYds AS INTEGER) AS off_yds,
       CAST(defTotalYds AS INTEGER) AS def_yds,
       CAST(ptsFor AS INTEGER) AS pts_for,
       CAST(ptsAgainst AS INTEGER) AS pts_against,
       CAST(tODiff AS INTEGER) AS to_diff
FROM standings
WHERE 1=1
  [AND teamName LIKE ?]
ORDER BY {sort_col} {sort_dir}
LIMIT ?
```

**Sort mapping:** "best offense" → `off_yds DESC`. "best defense" → `def_yds ASC` (lower = better). "most points" → `pts_for DESC`.

### Intent 13: `game_score`

**Collision guard:** The word "score" distinguishes this from `h2h_record`. The `h2h_record` builder must return `None` (fall through) if the question contains "score" — add "score" to H2H's stop words list so it doesn't steal game_score queries like "Lions vs Packers score".

**Patterns:**
- `score of {team1} vs {team2} [last week|season N]`
- `score of the {team} game`
- `{team1} vs {team2} score/result`

**SQL:**
```sql
SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName,
       homeScore, awayScore, homeUser, awayUser, winner_team
FROM games WHERE status IN ('2','3') AND stageIndex = '1'
  AND ((homeTeamName LIKE ? AND awayTeamName LIKE ?)
    OR (homeTeamName LIKE ? AND awayTeamName LIKE ?))
  [AND seasonIndex = ?]
ORDER BY CAST(seasonIndex AS INTEGER) DESC, CAST(weekIndex AS INTEGER) DESC
LIMIT ?
```

Single-team variant (no opponent): drop the opponent filter, return most recent games for that team.

### Intent 14: `owner_history`

**Patterns:**
- `what teams has {owner} owned/run/managed`
- `{owner}'s team/ownership history`
- `who owned the {team} in season N`

**SQL — Owner's teams:**
```sql
SELECT teamName, seasonIndex, games_played
FROM owner_tenure WHERE userName = ?
ORDER BY CAST(seasonIndex AS INTEGER)
```

**SQL — Team's owners:**
```sql
SELECT userName, teamName, seasonIndex, games_played
FROM owner_tenure WHERE teamName LIKE ?
  [AND seasonIndex = ?]
ORDER BY CAST(seasonIndex AS INTEGER)
```

### Intent 15: `records_extremes`

**Patterns:**
- `biggest blowout [ever|this season]`
- `closest game [ever|this season]`
- `highest/lowest scoring game [ever|this season]`
- `most lopsided/one-sided game`

**SQL:**
```sql
SELECT seasonIndex, weekIndex, homeTeamName, awayTeamName,
       homeScore, awayScore, homeUser, awayUser,
       ABS(CAST(homeScore AS INTEGER) - CAST(awayScore AS INTEGER)) AS margin,
       (CAST(homeScore AS INTEGER) + CAST(awayScore AS INTEGER)) AS total_pts
FROM games WHERE status IN ('2','3') AND stageIndex = '1'
  [AND seasonIndex = ?]
ORDER BY {sort_expression}
LIMIT ?
```

**Sort mapping:** "biggest blowout" → `margin DESC`. "closest" → `margin ASC`. "highest scoring" → `total_pts DESC`. "lowest scoring" → `total_pts ASC`.

### Intent 16: `standings_query`

**Patterns:**
- `{division} standings` (e.g., "NFC East standings")
- `playoff picture/race`
- `who leads the {conference|division}`
- `standings this season`
- `current standings`

**SQL:**
```sql
SELECT teamName, totalWins, totalLosses, winPct, ptsFor, ptsAgainst,
       seed, rank, divisionName, conferenceName, divWins, divLosses, confWins, confLosses
FROM standings
WHERE 1=1
  [AND divisionName LIKE ?]
  [AND conferenceName LIKE ?]
ORDER BY CAST(rank AS INTEGER)
LIMIT ?
```

**Note:** Column names are `divisionName` and `conferenceName` (NOT `divName`/`confName`). Values: "NFC East", "AFC North", etc. `winPct` is stored as a decimal string (e.g., "0.917"). `standings` is a current-season snapshot, so no season filter needed.

### Intent 17: `roster_query`

**Patterns:**
- `{team} roster`
- `best/highest rated {position} [in the league|on {team}]`
- `free agents [at {position}]`
- `who is the best {position}`
- `show me {team}'s {position}s`

**SQL — Team roster:**
```sql
SELECT firstName, lastName, pos, CAST(playerBestOvr AS INTEGER) AS ovr,
       dev, age, contractYearsLeft
FROM players WHERE teamName LIKE ?
  [AND pos = ?]
ORDER BY ovr DESC
LIMIT ?
```

**SQL — League-wide positional query:**
```sql
SELECT firstName, lastName, pos, teamName,
       CAST(playerBestOvr AS INTEGER) AS ovr, dev
FROM players WHERE pos = ?
  [AND isFA = '1']
ORDER BY ovr DESC
LIMIT ?
```

### Intent 18: `player_abilities_query`

**Patterns:**
- `who has x-factor/superstar on {team}`
- `{player}'s abilities/x-factor`
- `{team} x-factors/superstars`
- `what abilities does {player} have`

**SQL — Team abilities:**
```sql
SELECT firstName, lastName, teamName, title, description
FROM player_abilities WHERE teamName LIKE ?
ORDER BY firstName
```

**SQL — Player abilities:**
```sql
SELECT firstName, lastName, teamName, title, description
FROM player_abilities WHERE firstName || ' ' || lastName LIKE ?
```

---

## Existing Intent Fixes

### Tier 2 Wiring for leaderboard/team_record/draft_history

Currently `_build_from_classification()` returns `IntentResult(intent="unknown", tier=3)` for these 3 intents. Add proper SQL builders that mirror their Tier 1 counterparts.

### "How many games" routing

Add to `alltime_record` patterns:
- `how many games/wins/losses has {owner} played/won/lost`

Add to `season_record` patterns (with season qualifier):
- `how many wins/losses/games do I have this season`

### Registration Order

Priority-based ordering to prevent pattern collisions:

```
1.  h2h_record         (explicit "vs" — highest priority)
2.  recent_games        ("last N games" before season_record)
3.  game_score          ("score of X vs Y" before team_record)
4.  season_record
5.  alltime_record
6.  leaderboard         ("top N" ranked lists)
7.  player_stats        (individual player + stat queries)
8.  streak
9.  team_record
10. draft_history
11. trade_history
12. team_stats
13. owner_history
14. playoff_results
15. records_extremes
16. standings_query
17. roster_query
18. player_abilities_query
```

---

## Tier 2 Classification Prompt Update

Update `_CLASSIFICATION_PROMPT` to include all 18 intents with parameter schemas. Add `_build_from_classification()` handlers for each.

New intents added to the classification prompt:
```
9.  playoff_results — {"type": "superbowl"|"playoff", "season": int|null}
10. player_stats — {"player_name": str|null, "stat_category": str, "season": int|null, "limit": int}
11. trade_history — {"team": str|null, "season": int|null}
12. team_stats — {"team": str|null, "stat_category": str, "season": int|null}
13. game_score — {"team1": str, "team2": str|null, "season": int|null}
14. owner_history — {"owner": str|null, "team": str|null, "season": int|null}
15. records_extremes — {"type": "blowout"|"closest"|"highest"|"lowest", "season": int|null, "limit": int}
16. standings_query — {"division": str|null, "conference": str|null}
17. roster_query — {"team": str|null, "position": str|null, "free_agents": bool}
18. player_abilities_query — {"team": str|null, "player_name": str|null}
```

---

## Test Suite (40 Cases)

### Original 10 (unchanged)
| # | Question | Expected Intent |
|---|----------|-----------------|
| 1 | "what is my record vs diddy" | h2h_record |
| 2 | "JT vs Tuna" | h2h_record |
| 3 | "how are the Saints doing" | team_record |
| 4 | "my season record" | season_record |
| 5 | "Witt's all-time record" | alltime_record |
| 6 | "top 5 passers this season" | leaderboard |
| 7 | "who did New Orleans draft" | draft_history |
| 8 | "Chokolate_Thunda's record vs MeLLoW_FiRe" | h2h_record |
| 9 | "my last 5 games vs Killa" | recent_games |
| 10 | "what is Shottaz record this season" | season_record |

### New Intent Coverage (20 tests)
| # | Question | Expected Intent |
|---|----------|-----------------|
| 11 | "who won the Super Bowl in season 3" | playoff_results |
| 12 | "playoff results this season" | playoff_results |
| 13 | "who has the most passing TDs all-time" | player_stats |
| 14 | "top rushing yards this season" | player_stats |
| 15 | "who leads the league in sacks" | player_stats |
| 16 | "what trades did the Lions make" | trade_history |
| 17 | "trades this season" | trade_history |
| 18 | "which team has the best offense" | team_stats |
| 19 | "which team scores the most points" | team_stats |
| 20 | "what was the score of Lions vs Packers" | game_score |
| 21 | "score of the Chiefs game" | game_score |
| 22 | "what teams has Witt owned" | owner_history |
| 23 | "who owned the Bears in season 2" | owner_history |
| 24 | "biggest blowout ever" | records_extremes |
| 25 | "closest game this season" | records_extremes |
| 26 | "highest scoring game" | records_extremes |
| 27 | "NFC East standings" | standings_query |
| 28 | "who is the best QB in the league" | roster_query |
| 29 | "Lions roster" | roster_query |
| 30 | "who has x-factor on the Packers" | player_abilities_query |

### Edge Case / Regex Fix Tests (10 tests)
| # | Question | Expected Intent | Tests |
|---|----------|-----------------|-------|
| 31 | "my record last season" | season_record | "last season" parsing |
| 32 | "top five passers" | leaderboard | word number "five" |
| 33 | "what's my record this season" | season_record | contraction "what's" |
| 34 | "the Lions' record this season" | team_record | team possessive |
| 35 | "how many games have I won" | alltime_record | "how many" routing |
| 36 | "how many wins do I have this season" | season_record | "how many" + season |
| 37 | "who has most wins" | leaderboard | owner leaderboard |
| 38 | "Cowboys record season 4" | team_record | team + specific season |
| 39 | "who leads the league in interceptions" | player_stats | defensive stat |
| 40 | "free agents at QB" | roster_query | free agent filter |

### Test Harness Validation

Each test validates:
1. **Correct intent** — `result.intent == expected_intent`
2. **Tier ≤ 2** — `result.tier <= 2`
3. **Rows returned** — `len(rows) > 0` (or 0 for empty-but-valid queries)
4. **No SQL error** — `error is None`

Summary output: `"RESULTS: X/40 passed (intent match: Y, tier check: Z, rows: W)"`

---

## Files Modified

| File | Changes |
|------|---------|
| `codex_intents.py` | Add 10 new intents, STAT_REGISTRY, helper enhancements, Tier 2 wiring for all 18 intents |
| `test_oracle_stress.py` | Expand from 10 → 40 test cases, add intent/tier validation |
| `bot.py` | Bump ATLAS_VERSION |

---

## Verification

1. Run `python test_oracle_stress.py` — all 40 must pass
2. For each test: verify correct intent name, tier ≤ 2, rows returned, no SQL error
3. Run with `gemini_client=None` to test Tier 1 regex coverage (primary)
4. Optionally run with Gemini client to validate Tier 2 classification as backup
