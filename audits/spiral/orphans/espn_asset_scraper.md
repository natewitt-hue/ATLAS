# Adversarial Review: espn_asset_scraper.py

**Verdict:** LIVE (CLI script — keep but document and harden)
**Ring:** orphan
**Reviewed:** 2026-04-09
**LOC:** 478
**Reviewer:** Claude (delegated subagent)
**Total findings:** 8 (1 critical, 3 warnings, 4 observations)

## Summary

Standalone CLI script for scraping ESPN team logos and player headshots. Has `if __name__ == "__main__":` entrypoint and `python espn_asset_scraper.py` usage pattern in the docstring — not dead code, just an undocumented operations script. The script has serious robustness issues (no path-traversal protection on team abbreviations used in filesystem paths, blocking I/O sleeping the entire process, no resume capability) and unsafe ESPN API navigation that will silently produce malformed results.

## Findings

### CRITICAL #1: Path traversal via untrusted ESPN team abbreviation
**Location:** `C:/Users/natew/Desktop/discord_bot/espn_asset_scraper.py:240, 269, 271`
**Confidence:** 0.85
**Risk:** `team["abbreviation"]` and `team["espn_id"]` come unsanitized from ESPN's API and are used directly to build filesystem paths via `f"{abbr}.png"` and `headshots/{league}/{team_abbr}/{player_id}.png`. If ESPN ever returns an abbreviation with `..`, `/`, `\`, or `:` (or a player_id with the same), the script will write outside the intended `assets/` directory. ESPN is unlikely to do this maliciously, but soccer leagues already use unusual identifiers and a typo'd or test team in their feed (e.g. NCAA experimental teams) could trip this.
**Vulnerability:** `Path(...) / f"{abbr}.png"` does not validate the segment.
**Impact:** Arbitrary file write outside `assets/`. Worst case overwrites `bot.py` or a token file.
**Fix:** Sanitize: `abbr = re.sub(r"[^a-z0-9_-]", "", team["abbreviation"].lower())` and bail if empty. Same for `pid`.

### WARNING #1: `time.sleep` inside `_parallel_download` executor blocks the dispatch loop
**Location:** `C:/Users/natew/Desktop/discord_bot/espn_asset_scraper.py:282-294`
**Confidence:** 0.85
**Risk:** The script uses `ThreadPoolExecutor` to parallelize downloads, but then calls `time.sleep(DOWNLOAD_DELAY)` inside the `as_completed` consumer loop. This sleeps the main thread between completion notifications, not between dispatches — defeating the rate limit's purpose. If 4 workers complete simultaneously, the next 4 dispatches happen instantly.
**Vulnerability:** Concurrency primitive used incorrectly.
**Impact:** Effective rate limit is ~`MAX_DOWNLOAD_WORKERS * (1/DOWNLOAD_DELAY)` requests/sec per burst, not the intended steady-state. ESPN may rate-limit or ban.
**Fix:** Use a token bucket or sleep inside `download_image()` itself, or serialize via `pool.submit().result()` if true throttling matters.

### WARNING #2: Bare ESPN API navigation crashes on absent leagues
**Location:** `C:/Users/natew/Desktop/discord_bot/espn_asset_scraper.py:124-128`
**Confidence:** 0.85
**Risk:** `data["sports"][0]["leagues"][0].get("teams", [])` does not guard against `data["sports"]` being empty or missing `[0]["leagues"]`. ESPN occasionally returns `{"sports": []}` for unknown league keys or rate-limit pages. This raises `KeyError` or `IndexError` and crashes the entire scrape — losing all teams already accumulated for the run.
**Vulnerability:** No `try/except` around dict navigation.
**Impact:** Hours of work lost on a single bad league response.
**Fix:** Wrap in `try/except (KeyError, IndexError, TypeError)` and `return []` with a warning log.

### WARNING #3: `download_image` swallows all exceptions silently in pool
**Location:** `C:/Users/natew/Desktop/discord_bot/espn_asset_scraper.py:90-102, 282-296`
**Confidence:** 0.7
**Risk:** `download_image` returns `False` on any exception and prints to stderr. The pool then increments `failed` but never raises. A truncated PNG (e.g. zero-byte file) is treated as success. The dest existence check `if dest.exists(): return True` will then permanently cache that 0-byte file across runs.
**Vulnerability:** Resume logic is exists-only, not size/integrity-aware.
**Impact:** Partially-downloaded broken images cached indefinitely. Subsequent runs cannot re-fetch them.
**Fix:** After write, verify `dest.stat().st_size > 0` and `Path(dest).stat().st_size > 100`; delete and retry on failure.

### OBSERVATION #1: Dead-candidate but actually a live CLI
**Location:** `C:/Users/natew/Desktop/discord_bot/espn_asset_scraper.py:477-478`
**Confidence:** 0.95
**Risk:** Same as `backfill_embeddings.py` — has `if __name__ == "__main__":` and a documented `py espn_asset_scraper.py` usage. Static grep correctly finds zero importers but it's a CLI.
**Vulnerability:** N/A.
**Impact:** Risk of incorrect quarantine.
**Fix:** Add to `README.md` or move to `scripts/`.

### OBSERVATION #2: Hardcoded league configurations for college rosters
**Location:** `C:/Users/natew/Desktop/discord_bot/espn_asset_scraper.py:55-58`
**Confidence:** 0.5
**Risk:** NCAAFB has `limit: 150` to "avoid 300+ teams". The selection is arbitrary — not the top-150 by ranking, just whatever ESPN returns first. If a TSL team falls outside the top 150, its logo won't be scraped and the bot will silently lack assets for it.
**Impact:** Missing assets for legitimate teams.
**Fix:** Document the limit semantics, or sort by AP poll first.

### OBSERVATION #3: Concurrent runs would race on cache files
**Location:** `C:/Users/natew/Desktop/discord_bot/espn_asset_scraper.py:90-102`
**Confidence:** 0.4
**Risk:** Two simultaneous invocations of the script would both write to the same `assets/headshots/.../{pid}.png`. SQLite-style locking does not exist for filesystem writes.
**Impact:** Truncated files on collision.
**Fix:** Lock file or atomic-write-then-rename pattern.

### OBSERVATION #4: Hardcoded ESPN base URL with no version pinning
**Location:** `C:/Users/natew/Desktop/discord_bot/espn_asset_scraper.py:60`
**Confidence:** 0.4
**Risk:** `BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"` — if ESPN deprecates v2, the script breaks silently with 404s on every league.
**Impact:** Slow rot.
**Fix:** Document the v2 dependency in the docstring, plan a fallback.

## Cross-cutting Notes

This script and `backfill_embeddings.py` are both "manual ops scripts" that the spiral classifier flagged as orphans. Recommend a `scripts/` directory housing all such manual-run utilities, with a `scripts/README.md` listing each script's purpose, runtime, and required env vars. This would also fix the audit categorization gap where one-off CLIs are confused with dead code.
