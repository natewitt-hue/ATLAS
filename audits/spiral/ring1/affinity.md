# Adversarial Review: affinity.py

**Verdict:** needs-attention
**Ring:** 1
**Reviewed:** 2026-04-09
**LOC:** 221
**Reviewer:** Claude (delegated subagent)
**Total findings:** 17 (1 critical, 6 warnings, 10 observations)

## Summary

A small, self-contained affinity module that is largely correct on the happy path but has one critical concurrency bug (unsynchronized read-modify-write on `update_affinity` combined with an unbounded per-process in-memory cache) and a handful of warnings around cache coherence, stale-data persistence, and schema/migration safety. The sentiment analysis is a naive keyword heuristic with a broken word-boundary fallback for multi-word phrases like `"thank you"` and `"bot sucks"`. Ship-blocking only on the race condition; the rest are real but survivable given the try/except wrap in `bot.py`.

## Findings

### CRITICAL #1: Race condition on concurrent `update_affinity` calls for same user (lost updates)

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:104-132`
**Confidence:** 0.92
**Risk:** `update_affinity()` performs a classic read-modify-write sequence across three separate awaits: (1) open connection, (2) `SELECT affinity_score, interaction_count`, (3) compute `new_score = _clamp_score(row[0] + delta)`, (4) `UPDATE`. There is no row-level lock, no transaction isolation level pinning, no `BEGIN IMMEDIATE`, no in-process `asyncio.Lock`, and no `UPDATE … SET affinity_score = affinity_score + ?` atomic form. If a single user sends two messages in rapid succession (very common for spammers, or for the exact rude users this system is designed to penalize), two coroutines can both `SELECT` the same baseline score, both compute `baseline + delta`, and both `UPDATE`. The second `UPDATE` silently overwrites the first — one delta is permanently lost. Similarly, `interaction_count` will be undercounted.
**Vulnerability:** Discord's `on_message` handler in `bot.py:631-637` schedules `update_affinity()` inside a try/except wrapper that runs as part of message processing. Two messages arriving within ~50 ms from the same user (multiline paste split into two messages, bot DM bursts, raid scenarios) can easily interleave. `aiosqlite` does not serialize operations across different `connect()` contexts, and each call opens its own connection. The WAL pragma set in `setup_affinity_db()` is not re-applied per connection and does not provide isolation — it only affects durability.
**Impact:** Affinity scores drift incorrectly over time, penalizing negative users less harshly than intended (a spammer of slurs hits 2× `UPDATE` on the same baseline and loses one of the -3 deltas every conflict). `interaction_count` becomes unreliable for audit/analytics. For the asymmetric-punishment design premise ("NEGATIVE_DELTA sticks harder"), the lost-update bias is in the wrong direction — the worst offenders benefit most from the race. Also breaks cache coherence: `_affinity_cache[discord_id]` is written unconditionally at line 131, so the LAST writer's (possibly stale) `new_score` wins in both the cache and the DB.
**Fix:** Collapse the read-modify-write into a single atomic SQL statement:
```python
async with aiosqlite.connect(DB_PATH) as db:
    await db.execute(
        "INSERT INTO user_affinity (discord_id, affinity_score, interaction_count, last_interaction) "
        "VALUES (?, ?, 1, ?) "
        "ON CONFLICT(discord_id) DO UPDATE SET "
        "affinity_score = MAX(?, MIN(?, affinity_score + ?)), "
        "interaction_count = interaction_count + 1, "
        "last_interaction = excluded.last_interaction",
        (discord_id, _clamp_score(float(delta)), now, SCORE_MIN, SCORE_MAX, delta),
    )
    await db.commit()
# Then refresh cache by re-reading, or invalidate it:
_affinity_cache.pop(discord_id, None)
```
Additionally, guard the whole function behind a per-user `asyncio.Lock` (keyed in a `dict[int, asyncio.Lock]`) to serialize concurrent writers and keep the cache update consistent with the committed row. Without this, the cache at line 131 can still desync even with the atomic UPSERT.

### WARNING #1: Unbounded in-memory cache (`_affinity_cache`) — slow memory leak

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:49, 74-75, 85, 131, 143`
**Confidence:** 0.88
**Risk:** `_affinity_cache: dict[int, float]` grows monotonically for the process lifetime. Every unique Discord user that ever triggers `get_affinity()` adds an entry. There is no eviction policy, TTL, LRU bound, or size cap. `reset_affinity()` pops a single entry, but nothing else ever removes anything.
**Vulnerability:** A long-running bot process (days/weeks) across a 31-team league with DMs and server-wide chat will accumulate thousands of entries. Every cog calling `get_affinity()` (oracle_cog, bot.py on every message) adds a new key. Under a raid or mass-join scenario, the cache grows unboundedly.
**Impact:** Slow memory growth; pathologically bad if an attacker spams message events from many fresh accounts. Not catastrophic at TSL's scale (tens of users) but still a leak that will eventually show up in a long-uptime deployment (see FROGLAUNCH memory entry — VPS deployment amplifies this).
**Fix:** Use `functools.lru_cache` with `maxsize=512` on an async wrapper, or replace with a bounded `collections.OrderedDict`-based LRU, or add explicit size-bound eviction in `get_affinity()`. At minimum, document the lifetime expectation.

### WARNING #2: Cache coherence broken across multiple bot processes / restarts

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:74-88, 131, 143`
**Confidence:** 0.9
**Risk:** `_affinity_cache` is per-process. If two processes ever share `flow_economy.db` (e.g. operator runs a second debug instance, or a blue/green restart overlap), each has its own stale view. `reset_affinity()` (called from `god_cog.py:66`) only invalidates the cache in the process it ran in; the other process keeps serving the stale score until restart. Even within one process, if an admin directly edits `flow_economy.db` via sqlite3 CLI, the cache has no invalidation hook.
**Vulnerability:** The design assumes a single authoritative process. No invalidation broadcast, no cache versioning, no re-read TTL. The cache never expires — once read, `get_affinity()` returns the cached value forever (line 74-75 short-circuits), which means `reset_affinity()` in one process cannot affect another.
**Impact:** `/god affinity <user> reset` appears to succeed but the actual persona still treats the user as HOSTILE because the other-process cache is stale. Silent wrong behavior in admin workflows — which are exactly the workflows CLAUDE.md flags as "silent except bad".
**Fix:** Either (a) add a TTL to cache entries (store `(value, fetched_at)` tuples and refresh after e.g. 60s), (b) add an invalidation mechanism (publish to the event bus), or (c) document clearly that affinity is single-process only and add a startup warning if multiple processes are detected.

### WARNING #3: `setup_affinity_db()` has no schema migration path — silent schema drift

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:54-67`
**Confidence:** 0.85
**Risk:** `CREATE TABLE IF NOT EXISTS` handles the first-run case but does nothing for schema evolution. If a future version adds a column (e.g. `tier_history TEXT`, `custom_persona TEXT`, or a new `decay_timestamp`), the table will not be upgraded. Calls that reference the new column will raise `sqlite3.OperationalError: no such column`.
**Vulnerability:** CLAUDE.md explicitly flags this pattern in the Flow Economy gotchas: *"`sportsbook_cards._get_season_start_balance()` — Must wrap in `try/except sqlite3.OperationalError` … Column may not exist on older DBs."* Affinity already hit the same class of issue (the module docstring on line 7 still claims it uses `sportsbook.db` — see OBSERVATION #1 — meaning the DB target was already migrated once without schema versioning).
**Impact:** Any future schema change will silently break affinity for operators who upgrade without running a manual migration. Because `bot.py:304-309` wraps setup in try/except, the failure is invisible in the bot startup logs beyond a single print line.
**Fix:** Add a `schema_version` table or `PRAGMA user_version`, and run idempotent `ALTER TABLE` statements on version bumps. Wrap each migration step in its own try/except with `sqlite3.OperationalError` specifically.

### WARNING #4: Multi-word keywords broken by `\b` word-boundary regex

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:188-205, 214-215`
**Confidence:** 0.9
**Risk:** `_kw_match()` builds `rf"\b{re.escape(keyword)}\b"`. For multi-word keywords like `"thank you"`, `"good job"`, `"well done"`, `"bot sucks"`, `"shut up"`, `"brain dead"`, the `\b` anchors still work for the outer words, but the keyword contains a space, and `\b` requires a word character on one side and non-word on the other. `re.escape("thank you")` produces `thank\ you`; a `\b` around it matches `\bthank\ you\b`. This actually MOSTLY works because `\b` matches at a word/non-word transition, but it fails on punctuation-adjacent forms: `"thank you!"` matches fine, but `"thank,you"` (unlikely) doesn't. More importantly, the comment on line 204 gives a misleading example ("avoid partial matches e.g. 'w' in 'awesome'") — but `"w"` is in `_POSITIVE_KEYWORDS` on line 192, so a message like `"w"` (a common slang term meaning "win") matches, AND a message like `"w!"` matches, but `"w,"` … actually matches too because `,` is non-word. So the `\b` check is working for most realistic cases.
**Vulnerability:** The real bug is that `"good job"` appears in `_POSITIVE_KEYWORDS` but `"job"` alone does not. A user typing `"great job"` gets credit for `"great"` (positive) but not `"good job"` (because that exact phrase isn't present). Conversely, `"mid"` is negative — so a user saying `"the mid-season review"` gets a false negative because `\bmid\b` matches `mid-season` (hyphens are non-word chars). Similarly, `"ass"` is negative, so `"ass-kicker"` or `"bass player"` are false triggers — wait, `"bass"` has `"ass"` as a substring but `\b` prevents the match (correct). BUT `"ass-kicker"` matches `\bass\b` because `-` is non-word. That's a false negative penalty.
**Impact:** Users get surprise affinity drops for innocuous phrases like `"ass-kicker"`, `"kick-ass game"`, `"mid-season trade"`, `"cringe-worthy"` (wait, `"cringe"` is the keyword so that one's intentional). `"clown-around"` and `"lame-duck"` trigger false negatives. The asymmetric punishment (`-3` vs `+2`) amplifies the cost of false negatives. Real users will gain unexplained HOSTILE tiers.
**Fix:** Either (a) require context-aware matching (phrase match with surrounding spaces rather than `\b`), (b) remove ambiguous single-word slang (`"mid"`, `"ass"`, `"w"`), or (c) replace the whole heuristic with `atlas_ai.generate()` call budgeted to a small model — and cache per-message to avoid repeated calls. The CLAUDE.md rule says all AI calls must route through `atlas_ai.generate()`, so any future sentiment upgrade MUST go there.

### WARNING #5: Sentiment is not internationalized and ignores sarcasm / negation

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:208-221`
**Confidence:** 0.8
**Risk:** `"not helpful"` scores +1 (positive hit on `"helpful"`, no negative hit). `"not terrible"` scores -1 (negative hit on `"terrible"`, no positive hit). `"stupid good"` hits BOTH and nets to neutral (tie → neutral). `"this sucks… jk it's amazing"` nets to neutral. Sarcasm is fully invisible. A user repeatedly saying `"ATLAS is not helpful"` accidentally GAINS affinity because the keyword match is `"helpful"` → positive.
**Vulnerability:** No negation handling (`not`, `never`, `no`, `isn't`). No multi-word context window. Ties are counted as neutral even when the message is clearly rude (`"you're both great and stupid"`).
**Impact:** Affinity scores trend incorrect for any user with a non-trivial writing style. Users who give detailed critical feedback are penalized; users who give empty thanks-spam are rewarded. Over months, the HOSTILE/FRIEND tiers will not reflect real sentiment.
**Fix:** This is fundamentally a ceiling on the keyword approach. Either accept it as "best effort" and document the limitation prominently in the affinity tier instruction, or upgrade to an LLM-based classifier via `atlas_ai.generate()` with a constrained JSON output (`{sentiment: "positive|neutral|negative", confidence: 0-1}`) and cache results.

### WARNING #6: `get_affinity()` silently swallows ALL exceptions and returns 0.0

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:87-88`
**Confidence:** 0.85
**Risk:** `except Exception: return 0.0` is a bare catch. Disk full, permissions error, corrupted database, aiosqlite future change, module import issue — everything maps to "score 0" silently. There's no `log.exception(...)`. Every failure is invisible. Worse, because the cache at line 85 is only populated on success, a persistent failure will re-attempt the DB connection on every single `get_affinity()` call with zero observability.
**Vulnerability:** Per the ATLAS focus block: *"Silent `except Exception: pass` in admin-facing views is PROHIBITED."* `get_affinity()` isn't strictly admin-facing, but it is in the hot path of every user message (called from `bot.py:591` and `oracle_cog.py:3446`) and its silent failure mode disguises a broken affinity subsystem as "everyone is neutral" — which is the exact "silent wrong behavior" mode CLAUDE.md warns against.
**Impact:** If the DB file gets corrupted or the `flow_economy.db` path changes, the entire affinity subsystem degrades silently. No logs, no error metric, no sentinel alert. The HOSTILE tier vanishes and rude users lose their punishment — the exact opposite of the design intent.
**Fix:** Replace with:
```python
except sqlite3.OperationalError as e:
    log.warning("affinity: DB operational error for %d: %s", discord_id, e)
    return 0.0
except Exception:
    log.exception("affinity: unexpected error for discord_id=%d", discord_id)
    return 0.0
```
And add a module-level `log = logging.getLogger("atlas.affinity")`.

### OBSERVATION #1: Module docstring is stale — claims `sportsbook.db` but uses `flow_economy.db`

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:7, 23`
**Confidence:** 1.0
**Risk:** Line 7 docstring says `"Uses sportsbook.db alongside the casino/economy tables"` but line 23 actually uses `os.getenv("FLOW_DB_PATH", ..., "flow_economy.db")`. Per CLAUDE.md, `sportsbook.db` is the "legacy (orphaned)" DB; the active one is `flow_economy.db`.
**Impact:** Misleads future maintainers. Confuses auditors (me, just now). Indicates the module has already migrated once without updating its own docs.
**Fix:** Update docstring to say `"Uses flow_economy.db alongside the casino/wallet tables (FLOW_DB_PATH override supported)."`

### OBSERVATION #2: No `log` module imported — zero observability

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:15-21`
**Confidence:** 0.95
**Risk:** The module has no `logging.getLogger` instance. It cannot emit any structured logs. Every other ATLAS module in Ring 1 uses `log = logging.getLogger("atlas.xxx")` (e.g. `god_cog.py:25`). Affinity is fully silent.
**Impact:** Debugging affinity issues in production requires operator to add print statements or wrap every call. Cannot filter by log level. Cannot alert on error rate.
**Fix:** Add `import logging` and `log = logging.getLogger("atlas.affinity")` at module level. Use it everywhere exceptions are caught.

### OBSERVATION #3: `_clamp_score()` takes `float` but `SCORE_MIN`/`SCORE_MAX` are `int`

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:26-30, 39-41`
**Confidence:** 0.7
**Risk:** Minor type smell: `POSITIVE_DELTA = 2` (int), `SCORE_MIN = -100` (int), `SCORE_MAX = 100` (int), but `affinity_score REAL DEFAULT 0.0` (float). `_clamp_score()` returns `float` per its annotation, but for an int input `max(-100, min(100, 2))` returns int, not float. The runtime behavior is fine (implicit conversion at DB write), but the type annotation lies.
**Impact:** Type checker (if run) flags it. No runtime bug.
**Fix:** Either make the constants `-100.0` / `100.0` / `2.0`, or annotate as `int | float`. Cosmetic.

### OBSERVATION #4: `_clamp_score()` defined BEFORE the tier thresholds — reads oddly

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:39-47`
**Confidence:** 0.5
**Risk:** The file groups constants: deltas (26-36), then `_clamp_score` function (39-41), then tier thresholds (44-46), then cache (49). Conventional order is all constants together, then functions. Minor readability nit.
**Impact:** None functional. Style only.
**Fix:** Move `_clamp_score()` below all constants.

### OBSERVATION #5: `neutral` sentiment short-circuits `get_affinity()` but doesn't update `interaction_count` or `last_interaction`

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:99-100`
**Confidence:** 0.85
**Risk:** The comment says "Skip the DB round-trip for neutral interactions". This is an optimization, but it means a user sending 10,000 neutral messages never updates `interaction_count` or `last_interaction`. The analytics value of those columns is diminished — you can't answer "when did this user last interact" or "how many total interactions has this user had" from the DB.
**Impact:** Analytics are missing neutral-only users entirely. The `notes` field is never written at all in this code path or any other. If `god_cog.py` or an admin dashboard ever queries `interaction_count`, it will undercount by the neutral ratio (probably 80%+ of all messages).
**Fix:** Either (a) document the intent explicitly ("interaction_count only tracks sentiment-bearing messages"), (b) rename the column to `sentiment_interaction_count`, or (c) always write `last_interaction` even on neutral (one `UPDATE` per neutral is cheap given the aiosqlite pool).

### OBSERVATION #6: No decay function — HOSTILE is permanent

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:91-132`
**Confidence:** 0.9
**Risk:** Once a user's score hits `-50` (HOSTILE) or even `-100` (clamped floor), they can only recover at `+2` per positive message. From -100 to -9 (to exit DISLIKE) requires 46 positive messages. From -100 to +30 (FRIEND) requires 65 positive messages. No time-based decay: a user who was rude two years ago is still HOSTILE today. Conversely, a user who was friendly once and hasn't talked since is still FRIEND forever.
**Impact:** Affinity scores become fossilized; the system can't model "they've changed" or "they haven't been active in 6 months so revert to neutral". Real Discord communities have rhythm and drift; this doesn't.
**Fix:** Add a decay function in `setup_affinity_db()` scheduled task: `UPDATE user_affinity SET affinity_score = affinity_score * 0.95 WHERE last_interaction < datetime('now', '-7 days')`. Or, on `get_affinity()`, apply a time-based decay if `last_interaction` is > N days ago. Either way, decay is mentioned nowhere in the file.

### OBSERVATION #7: `reset_affinity()` does not reset `interaction_count`, `last_interaction`, or `notes`

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:135-143`
**Confidence:** 0.9
**Risk:** The function name promises "reset" but it only zeroes `affinity_score`. `interaction_count` and `last_interaction` survive, as does the `notes` column. An admin thinking they've "wiped the slate clean" for a user via `/god affinity <user> reset` may be surprised to find historical state persists.
**Impact:** Admin surprise. In extreme cases, if the system later adds tiered thresholds based on `interaction_count` (e.g. "new user = extra patient"), the reset won't restore the new-user status.
**Fix:** Either (a) rename to `reset_affinity_score()` to make the scope explicit, or (b) also reset `interaction_count=0`, `notes=''`, `last_interaction=NULL`. At minimum, document the limited scope in the docstring.

### OBSERVATION #8: `POSITIVE_KEYWORDS` and `NEGATIVE_KEYWORDS` are module-level sets with single-char entries

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:188-200`
**Confidence:** 0.9
**Risk:** The positive list contains single-char `"w"` (slang for "win"). `\b` matching means: any message containing a lone `"w"` surrounded by non-word chars hits positive. A user typing `"w. that was close"` or `"w, ok"` or a literal `"w"` message all trigger +2. But so does `"W tier"` (gaming tier slang) which was probably intentional. However, `"thx w/ you"` has `\bw\b` — actually `w/` has a slash which is non-word, so `\bw\b` matches. False positive.
**Impact:** Easy to game the system: a user hostile to ATLAS could sprinkle `w` in every message to offset their negatives. The asymmetric weights (`+2 vs -3`) limit the exploit, but it's still a known-to-attacker attack vector if the HOSTILE tier is visible anywhere.
**Fix:** Remove `"w"` and `"dub"` and `"ass"` from the keyword sets — single-char and ambiguous-substring keywords are too noisy. Or promote them to multi-char forms: `"big w"`, `"clutch w"`, `"w rizz"`, etc.

### OBSERVATION #9: `get_affinity_instruction()` returns raw prompt text with no sanitization

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:148-172`
**Confidence:** 0.7
**Risk:** The function returns a fixed string based on score bracket. The string contains no user-controlled data, so direct prompt injection via this module is not possible. But the strings are hardcoded English and use the unified persona format. If `echo_loader.py`'s persona ever changes format (e.g. to JSON or YAML), these `[USER AFFINITY: ...]` bracket tags would need to be updated to match. The coupling is implicit and undocumented.
**Impact:** Persona drift over time. The instruction format here hardcodes a `[TAG: VALUE]` pattern that `echo_loader`'s unified persona may not honor; there's no test verifying the downstream LLM actually changes tone based on these strings.
**Fix:** Add a comment linking the format to `echo_loader.py`'s expected format. Consider moving the tier instruction strings into `echo_loader.py` as a central registry so persona authors can edit them in one place.

### OBSERVATION #10: No module-level docstring examples show the `update_affinity` flow

**Location:** `C:/Users/natew/Desktop/discord_bot/affinity.py:1-13`
**Confidence:** 0.6
**Risk:** The top docstring shows `get_affinity` and `get_affinity_instruction` but not `update_affinity` or the `analyze_sentiment` → `update_affinity` pipeline, which is the most error-prone call site (see WARNING #6's caller pattern in `bot.py:631-637`). Future maintainers will miss the sentiment → update flow.
**Impact:** Minor onboarding friction.
**Fix:** Add a docstring example:
```python
sentiment = affinity.analyze_sentiment(message)
new_score = await affinity.update_affinity(user_id, sentiment)
```

## Cross-cutting Notes

Three themes from this file likely echo in the rest of Ring 1 Batch C (Oracle / AI / Memory):

1. **Silent `except Exception: pass` in try/except-wrapped optional modules.** `bot.py:593-594` and `bot.py:636-637` wrap affinity calls in `try: ... except: pass`, making every internal failure invisible. Any sibling optional module (`lore_rag`, etc.) likely does the same — the critical issue is that the caller's silent swallow combined with the callee's silent swallow produces a cascade where failures in affinity are TWICE invisible. This pattern should be audited across all optional-module callers and each should log the exception at least once.

2. **In-memory caches without invalidation hooks in modules with admin reset commands.** `god_cog.py` calls `affinity.reset_affinity()`, but the reset only affects the calling process's cache. Any other Ring 1 module with an admin reset + in-memory cache (e.g. echo persona cache, affinity-like scoring in oracle/codex) will have the same cross-process staleness. Consider a lightweight pub/sub invalidation bus.

3. **Schema evolution without `PRAGMA user_version` or migration table.** Affinity's `CREATE TABLE IF NOT EXISTS` pattern is duplicated across the codebase. Any future column add silently diverges between fresh and upgraded installs. The same audit should check all `setup_*_db()` functions in Ring 1 (`flow_wallet`, `sportsbook_core`, `wager_registry`) for migration safety.
