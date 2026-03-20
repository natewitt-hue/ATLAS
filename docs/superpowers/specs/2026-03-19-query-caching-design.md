# Query Caching for Tier 3 NL→SQL Pipeline — Design Spec

> **Priority:** #6 of 7 Oracle V4 improvements (was C2 in V4 handoff)
> **Date:** 2026-03-19
> **Scope:** `codex_cog.py` (new `_QueryCache` class + integration in `/ask`), `bot.py` (cache invalidation on sync + version bump)
> **Approach:** LRU cache with TTL keyed on normalized question + caller identity, caching SQL + rows to skip expensive AI generation + SQL execution on repeated queries

---

## Problem

Every `/ask` Tier 3 query runs the full pipeline: `gemini_sql()` (Sonnet) → `retry_sql()` (up to 3 attempts) → `gemini_answer()` (Haiku). Identical questions re-run everything. At ~31 users, the same popular questions ("Who leads in passing yards?", "Top QBs by rating?") get asked repeatedly within short windows, wasting AI tokens and adding latency.

---

## Design

### What Gets Cached

**SQL + rows only.** The answer is always regenerated via `gemini_answer()` (cheap Haiku call) because it depends on conversation context and affinity tone that change per-user and per-turn.

On cache hit, we skip: `gemini_sql()` (Sonnet, ~2s) + `retry_sql()` (run_sql + up to 2 AI retries). This is 90%+ of the Tier 3 cost and latency.

### Cache Key

```python
key = normalize(question) + "|" + (caller_db or "anon")
```

Normalization: lowercase, strip, collapse whitespace, strip trailing punctuation. "Who has the most wins?" and "who has the most wins" hit the same entry.

`caller_db` is included because ownership queries ("my trades", "my team's record") return different results per user.

### Cache Entry

```python
@dataclass
class _CacheEntry:
    sql: str              # The SQL that was generated
    rows: list[dict]      # Query results
    attempt: int          # Which retry attempt succeeded
    warnings: list[str]   # validate_sql output
    created_at: float     # For TTL check
```

### Configuration

- **TTL:** 5 minutes (data refreshes via sync_tsl_db are infrequent, ~hourly)
- **Max entries:** 200 (at ~5KB per entry = ~1MB max, negligible)
- **Eviction:** On exceeding max_entries, evict oldest 25% by created_at

### Cache Invalidation

- **On `sync_tsl_db()` completion:** Clear entire cache (data changed). Called from `bot.py` after sync finishes.
- **TTL expiry:** Checked on each `get()` call — stale entries return miss.
- **No per-entry invalidation needed** — 5-minute TTL is short enough.

### Integration Point

In the `/ask` command handler, after intent detection routes to Tier 3:

```python
# Before gemini_sql():
cache_key = _query_cache.make_key(annotated_question, caller_db)
cached = _query_cache.get(cache_key)

if cached:
    sql, rows, attempt, warnings = cached.sql, cached.rows, cached.attempt, cached.warnings
    from_cache = True
else:
    sql = await gemini_sql(...)
    rows, sql, error, attempt, warnings = await retry_sql(sql, schema)
    if not error:
        _query_cache.set(cache_key, sql, rows, attempt, warnings)
    from_cache = False

# gemini_answer() always runs (cheap, needs fresh context)
answer = await gemini_answer(question, sql, rows, conv_context)
```

### Footer Indicator

On cache hit, append "⚡ Cached" to the embed footer so users know the result was instant.

### Diagnostics

```python
print(f"[QueryCache] {'HIT' if from_cache else 'MISS'} key={cache_key[:8]}… entries={len(_query_cache)}")
```

---

## Files Modified

| File | Changes |
|------|---------|
| `codex_cog.py` | Add `_QueryCache` class (~40 lines), integrate in `/ask` Tier 3 path, add `clear_query_cache()` export |
| `bot.py` | Call `clear_query_cache()` after `sync_tsl_db()`, bump version (3.10.0 → 3.11.0) |

No new files.

---

## What's NOT Changing

- **Tier 1/2 pipelines** — already fast (deterministic SQL, no AI generation)
- **Oracle modals** (AskTSL, PlayerScout, StrategyRoom) — could benefit from caching but out of scope; they have different SQL generation flows
- **`gemini_sql()`** — unchanged, still generates SQL via Sonnet
- **`retry_sql()`** — unchanged, still 3-attempt cascade
- **`gemini_answer()`** — unchanged, still formats answer via Haiku

---

## Testing

1. Ask "Who leads in passing yards?" twice within 5 minutes → second response should show "⚡ Cached" in footer
2. Run `/wittsync` between identical questions → cache should be cleared, second query runs fresh
3. Same question from two different users (different caller_db) → separate cache entries
4. Ask same question after 5+ minutes → cache miss, fresh pipeline
