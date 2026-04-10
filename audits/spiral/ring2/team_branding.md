# Adversarial Review: team_branding.py

**Verdict:** needs-attention
**Ring:** 2
**Reviewed:** 2026-04-09
**LOC:** 231
**Reviewer:** Claude (delegated subagent)
**Total findings:** 14 (1 critical, 6 warnings, 7 observations)

## Summary

`team_branding.py` is a simple JSON-backed lookup helper with a clean constructor and small API surface. It is not critical path for financial logic, but it silently degrades in multiple ways (missing files, unknown teams, KeyError in `logo_path`) that will surface as broken card renders rather than visible errors. The identity-resolution story is weak: nickname matching is naive lowercase equality and will not handle Madden's `teams.csv` variants (e.g., "49ers" vs "49ERS", "Football Team", relocations), which directly contradicts the ATLAS-wide identity resolution mandate.

## Findings

### CRITICAL #1: `logo_path()` raises `KeyError` when a team has no `abbreviation` field
**Location:** `team_branding.py:124-134`
**Confidence:** 0.85
**Risk:** `team["abbreviation"].lower()` does an unchecked dict indexing after `by_nickname()` returns a truthy team dict. If a team entry in `team_branding.json` is missing or has a null `abbreviation` (empty string still works, `None` or absent does not), this raises `KeyError` / `AttributeError` and propagates up into whatever rendering path called it (e.g., Pillow card builder, Playwright render path).
**Vulnerability:** The constructor tolerates missing `abbreviation` at index build time (`team.get("abbreviation", "")` on line 73 coerces to empty string, and empty strings are skipped by the `if abbr:` guard on line 78). But the team dict itself is still stored in `_by_nickname` as long as it had a `nickname`. That means a team with `nickname` but no `abbreviation` is indexable by nickname — and then `logo_path()` crashes on `team["abbreviation"]`.
**Impact:** Crash in a hot rendering path. Since every Pillow/Playwright card render for a team likely calls `logo_path()` via `mm_team_logo_path()`, a single malformed JSON entry takes out every card for that team. No logging, no fallback — the exception bubbles up as an unhandled render failure.
**Fix:** Replace `team["abbreviation"].lower()` with `(team.get("abbreviation") or "").lower()` and return `None` if the abbr is empty. Same treatment for any other dict field accessed via `[]` after a guard that only checks truthiness of the team dict.

### WARNING #1: Default `league` parameter uses mutable `None` with lazy resolution — silent league bleed risk
**Location:** `team_branding.py:85-107, 111-134, 138-151`
**Confidence:** 0.7
**Risk:** Every lookup method accepts `league: str = None` and resolves with `league = league or self.default_league`. If the caller passes an empty string `""` (a common result of `.strip()` on Discord command input), it falls through to the default silently instead of raising. This creates a class of latent bugs where NFL team names are matched against NBA league (or vice versa) with no error.
**Vulnerability:** `"" or self.default_league` evaluates to `self.default_league` because empty string is falsy. Same for any sentinel like `"None"` in string form (wouldn't fall through, but would fail lookup instead of erroring).
**Impact:** Cross-league contamination. In a multi-sport deployment (NFL + NBA headshots in one JSON), an empty-string league accidentally falls back to NFL. Card renders would show wrong logos / colors with no error trail.
**Fix:** Use an explicit sentinel: `league: Optional[str] = None` (already done) and `if league is None: league = self.default_league`. This preserves the intended default-on-missing semantics without swallowing empty strings.

### WARNING #2: Constructor prints to stdout instead of using structured logging
**Location:** `team_branding.py:50-52`
**Confidence:** 0.9
**Risk:** `print(f"⚠ Team branding file not found: {bp}")` writes to stdout and silently continues with empty data. In a Discord bot running as a systemd service (FROGLAUNCH plan), stdout is consumed by the journal but never reaches `log.error()` or the bot's admin channel. If the asset file is missing in production, every subsequent call returns empty/None and cards render as stubs — and the commissioner has no signal.
**Vulnerability:** `print()` bypasses the logging framework entirely. Missing headshots file on line 59 has no warning at all — totally silent.
**Impact:** Silent production degradation. Every team lookup returns `None`, every color call returns `("#000000", "#ffffff")`, every logo path is `None`. Cards degrade to default/blank. No alerts, no way to know from inside Discord that the asset file is missing.
**Fix:** Replace `print(...)` with `log.error(...)` from the Python `logging` module. Missing headshots file should also emit a warning. Consider raising in production mode if the asset files are load-bearing.

### WARNING #3: JSON loading is synchronous blocking I/O in module-level usable class
**Location:** `team_branding.py:46-60`
**Confidence:** 0.6
**Risk:** `json.load(f)` on possibly large `team_branding.json` and `player_headshots.json` (which per the docstring contains per-player headshots for all NFL teams — potentially thousands of entries) is blocking I/O. If `TeamBranding(...)` is instantiated inside an `async def` context (e.g., cog `setup_hook`, or a slash command handler), it blocks the event loop for the duration of the load.
**Vulnerability:** Nothing in the class signals "instantiate me at startup only". The docstring example shows `branding = TeamBranding(...)` with no async guard. A careless caller in a cog lazy-instantiates per-command, blocking the event loop on every invocation.
**Impact:** Event loop stalls. In a Discord bot, blocking the loop for >500ms causes gateway heartbeats to time out and can trigger reconnects, which in turn re-triggers `load_all()` without `_startup_done` protection.
**Fix:** Document that `TeamBranding` must be constructed at startup (in `on_ready` or `setup_hook` once), not per-command. Or provide an async classmethod `TeamBranding.load_async()` that runs `json.load` under `asyncio.to_thread`.

### WARNING #4: `player_headshot()` uses substring match on user-supplied name — false positives
**Location:** `team_branding.py:159-166`
**Confidence:** 0.75
**Risk:** `if name_lower in p.get("name", "").lower():` does a substring match, not equality. Looking up "Smith" matches "Smith", "Smithson", "Goldsmith", and "Brandon Smith", returning whichever comes first in roster order (dict iteration order). Looking up "Lamar" matches "Lamar Jackson" and "Lamar Miller" indiscriminately.
**Vulnerability:** Substring match on unverified input. There's no ranking, no exact-match priority, no disambiguation. Whoever sorted first in the JSON wins.
**Impact:** Wrong player headshots on cards. Trade cards, player profile cards, draft history cards all render the wrong face for any name that is a substring of another player's name. High-profile collision: "Tyreek Hill" is a substring of "Tyreek Hill Jr." if that ever happens; "Brown" matches every Brown on the roster.
**Fix:** Do an exact match first, then fall back to substring only if no exact hit. Or use `player_name.lower() == p.get("name", "").lower()` for strict matching and provide a separate `player_headshot_fuzzy()` for explicit fuzzy lookups. Also add ranking: prefer names where the match starts at index 0 (first-name match).

### WARNING #5: Identity resolution is naive — does not match Madden `teams.csv` variants
**Location:** `team_branding.py:183-188`
**Confidence:** 0.7
**Risk:** `mm_nickname_to_branding()` is documented as "the primary integration point for ATLAS" and simply delegates to `by_nickname()`, which is exact lowercase matching. Per ATLAS focus rules, "API usernames have underscores/case mismatches. Use `_resolve_owner()` fuzzy lookup." The same class of bugs applies to team nicknames: Madden exports may contain relocations (e.g., "Commanders" vs "Football Team" vs "Redskins"), abbreviations, or whitespace-padded variants.
**Vulnerability:** No alias table, no fuzzy matching, no fallback to abbreviation. A team renamed mid-season or a CSV export with subtle whitespace/encoding differences silently returns `None`.
**Impact:** Cards for renamed / legacy teams render with default black/white colors and no logo. Historical records for defunct franchises (95+ Super Bowl seasons per CLAUDE.md) never resolve.
**Fix:** Add an alias map (similar to `build_member_db.get_alias_map()`) that normalizes Madden nicknames to canonical ESPN nicknames. Fall back: `by_nickname → by_abbreviation → alias_map → None`. Log a warning on the alias_map miss path so the commissioner sees which teams fall through.

### WARNING #6: Roster lookup is O(n) per call with no cache — scales poorly across card render bursts
**Location:** `team_branding.py:159-173`
**Confidence:** 0.55
**Risk:** `player_headshot()` and `player_by_espn_id()` both iterate the full team roster on every call. For a Blackjack/Highlights render that calls player lookup per player card (e.g., a parlay card with 8 legs), that's 8 × full roster scan.
**Vulnerability:** No index built for player lookups, unlike team lookups which are O(1). `_players_raw` is loaded but never reindexed.
**Impact:** Low single-call latency, but amplified under burst load. Not a correctness bug, but in the hot render path where 4 Playwright pages are pre-warmed, a slow roster iteration compounds with HTML rendering delay.
**Fix:** Build `_by_player_name[league][abbr]` and `_by_player_espn_id[league][abbr]` dicts at construction time, same pattern as `_by_nickname`. Or at minimum, lazily build and cache on first access.

### OBSERVATION #1: `default_league` defaults to "NFL" but doesn't validate it exists in the loaded data
**Location:** `team_branding.py:42-43`
**Confidence:** 0.6
**Risk:** If someone passes a custom `branding_path` with only NBA data, the default_league "NFL" silently returns empty for every call. No startup warning.
**Vulnerability:** Constructor accepts any string and the first lookup miss is the symptom.
**Impact:** Mysteriously empty results with no hint why.
**Fix:** After building indexes, `if self.default_league not in self._by_nickname: log.warning(...)`.

### OBSERVATION #2: Return type annotation mismatch on `colors_rgb` and `mm_team_colors_pillow`
**Location:** `team_branding.py:148-151, 190-192`
**Confidence:** 0.85
**Risk:** Annotated as `tuple[tuple, tuple]` but the inner tuples are specifically `tuple[int, int, int]`. Loose typing silently accepts any tuple, including wrong-shaped ones from a buggy `_hex_to_rgb`.
**Vulnerability:** Type checkers (mypy, pyright) can't catch downstream misuse.
**Impact:** None at runtime — this is a type hygiene issue, not a bug. But it's on a public API consumed by Pillow renderers.
**Fix:** `def colors_rgb(...) -> tuple[tuple[int, int, int], tuple[int, int, int]]:`.

### OBSERVATION #3: `_hex_to_rgb` silently returns (0,0,0) on malformed input
**Location:** `team_branding.py:202-207`
**Confidence:** 0.85
**Risk:** Any hex string that isn't exactly 6 chars after stripping `#` silently becomes black. "#fff" (3-char shorthand, valid CSS) → black. "#ffffffff" (8-char with alpha) → black. Invalid characters → `ValueError` raised from `int(h[0:2], 16)` — inconsistent with the "silent black" fallback.
**Vulnerability:** Half-baked validation: rejects length mismatch silently, but propagates `ValueError` on non-hex characters.
**Impact:** A color like "#fff" in team JSON produces a silent black primary, hiding a data bug. A non-hex value crashes the caller.
**Fix:** Expand 3-char hex to 6-char before validating. Wrap `int(...)` in try/except and return `(0, 0, 0)` on any parse failure. Log at WARNING level when the fallback fires so data issues are visible.

### OBSERVATION #4: `all_teams()` returns the raw internal list by reference — mutation risk
**Location:** `team_branding.py:100-103`
**Confidence:** 0.7
**Risk:** `return self._raw.get(league, [])` returns the same list object stored in `self._raw`. A caller that does `teams = branding.all_teams(); teams.append(custom_team)` mutates the source of truth for all other callers in the process.
**Vulnerability:** DataFrame-style shared state bug for a plain Python list. Per ATLAS focus, "DataFrame mutation across cogs (data_manager DataFrames are shared state)" is a known pattern. Same risk here.
**Impact:** Subtle action-at-a-distance. A renderer that appends a scratchpad team corrupts all future calls.
**Fix:** `return list(self._raw.get(league, []))` to return a shallow copy. Or return a tuple for immutability.

### OBSERVATION #5: `team_roster()`, `player_headshot()` etc. ignore `default_league` — inconsistent API
**Location:** `team_branding.py:155-179`
**Confidence:** 0.8
**Risk:** Team lookups (`by_nickname`, `logo_url`, etc.) all use `league = league or self.default_league` pattern. Player lookups require `league` as a positional first arg with no default. This is inconsistent and the caller has to know which is which.
**Vulnerability:** API surface drift. Easy to forget which methods need an explicit league.
**Impact:** Minor — misuse raises `TypeError` at call site, so it's a loud failure not a silent one.
**Fix:** Make `league` optional with default across the board, or mandatory everywhere. Pick one convention.

### OBSERVATION #6: `__main__` test block prints to stdout only — no assertions
**Location:** `team_branding.py:212-231`
**Confidence:** 0.9
**Risk:** The "Quick Test" block is a smoke test that visually verifies lookups. It has no `assert` statements, doesn't return a non-zero exit code on failure, and can't be wired into CI. "NOT FOUND" output is indistinguishable from successful output on a glance.
**Vulnerability:** Not a bug, but a missed opportunity. This looks like test code but acts like a demo.
**Impact:** None — this is dev scaffolding.
**Fix:** Move to `tests/test_team_branding.py` as proper pytest cases. Keep the demo here only if it also exits non-zero on any `None` return.

### OBSERVATION #7: Emoji in docstring / print statement may not render on all consoles
**Location:** `team_branding.py:51`
**Confidence:** 0.5
**Risk:** The `⚠` U+26A0 character in the fallback `print` may not render on Windows cmd.exe or journalctl with a non-UTF8 locale. On a misconfigured terminal it becomes `⚠` or similar mojibake, obscuring the actual path in the warning.
**Vulnerability:** Encoding assumption.
**Impact:** Cosmetic. The warning still appears but is uglier.
**Fix:** Replace with ASCII `[WARN]` or move to the logging framework (see WARNING #2) which handles encoding properly.

## Cross-cutting Notes

- **Asset file discovery pattern:** The implicit assumption is that `assets/team_branding.json` and `assets/player_headshots.json` exist relative to CWD. Other ATLAS files likely share this pattern for `assets/logos/NFL/*.png` and `assets/headshots/NFL/*/*.png`. A single missing-asset check utility could cover all of them.
- **Identity resolution gap:** This file reinforces that ATLAS has two identity-resolution domains (Madden players via `_resolve_owner`, ESPN teams via `by_nickname`) and neither is robust against rename/alias. A unified alias table (similar to `tsl_members`) for teams would close a persistent gap.
- **Silent degradation pattern:** Both missing files and unknown teams return `None`/empty without logging. Across the codebase this pattern is prohibited by CLAUDE.md rules for admin views, and is problematic here in the render path. Any renderer calling `logo_path()` should log when it gets `None` back so data quality issues surface in the admin channel.
- **No Discord CDN signed URLs spotted:** Per the ATLAS focus concern about expiring Discord CDN URLs (ref: `constants.py`): this file loads URLs from a local JSON of ESPN URLs, not Discord CDN. ESPN logo URLs are generally stable, so the expiring-URL risk is absent here. That's the one thing this file does right on the asset-URL front.
