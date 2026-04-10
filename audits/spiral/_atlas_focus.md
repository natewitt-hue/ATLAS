<atlas_specific_attack_surface>
This is the ATLAS Discord bot for The Simulation League — a Madden NFL sim league. Prioritize these failure modes (extracted from CLAUDE.md "hard-won lessons"):

**Flow / Economy / Sportsbook:**
- `flow_wallet.debit()` and `flow_wallet.credit()` MUST be called with a `reference_key` argument on every call. Without it, Discord interaction retries cause double-debits/credits. Format: `f"{SUBSYSTEM}_DEBIT_{uid}_{event_id}_{int(time.time())}"`. Flag any call site that omits `reference_key`.
- Silent `except Exception: pass` in admin-facing views is PROHIBITED. Flag every silent swallow.
- The `"sportsbook_result"` bus topic is subscribed by `flow_live_cog` but no live code publishes to it. Flag any new publisher that doesn't also wire `SportsbookEvent.guild_id`.
- `sportsbook_cards._get_season_start_balance()` must wrap in `try/except sqlite3.OperationalError`. Column may not exist on older DBs.
- Float vs int balance corruption in `flow_economy.db`.

**MaddenStats API:**
- `weekIndex` is 0-based in API but `CURRENT_WEEK` in data_manager is 1-based. Off-by-one trap.
- Completed games filter: `status IN ('2','3')`, NOT `status='3'` alone. Using only '3' silently drops results.
- Full roster data (OVR, devTrait, ability1–6) only from `/export/players`. Stat-leader endpoints cannot substitute.
- Identity resolution: API usernames have underscores/case mismatches. Use `_resolve_owner()` fuzzy lookup. `team_record` SQL: `winner_user`/`loser_user` are API usernames not team nicknames. Always resolve team→username before binding params.
- Draft history: credit players to drafting team, NOT current team.
- `devTrait` mapping: 0=Normal, 1=Star, 2=Superstar, 3=Superstar X-Factor.
- Ability budgets: Star=1B, Superstar=1A+1B, XFactor=1S+1A+1B, C-tier unlimited. Use OR logic on dual-attribute checks.

**Discord API:**
- `view=None` cannot be passed as keyword arg to `followup.send()` — must omit entirely.
- Modals with Gemini calls (>3s) require `defer()` first or hit the 3s timeout.
- No clickable text in embeds — must use cascading select menus.
- Select menus capped at 25 options. `@discord.ui.select` requires `options=[]` even when populated dynamically.
- Two cogs with the same slash command name → second silently fails.
- Ephemeral vs public: drill-downs = ephemeral; hub landing embeds = public.

**Async / Concurrency:**
- Blocking calls inside `async` functions (sqlite3, requests, time.sleep). All blocking I/O must go through `asyncio.to_thread()` or use async libs.
- Race conditions on cog `_startup_done` flag.
- DataFrame mutation across cogs (data_manager DataFrames are shared state).
- TOCTOU on trade approval, ability budget enforcement, balance writes.
- Resource leaks: Playwright pages not returned to the pool, sqlite connections never closed.

**AI / Codex:**
- Hardcoded `ATLAS_PERSONA` instead of `get_persona()` from echo_loader.
- Direct Gemini/Claude SDK calls from cogs instead of `atlas_ai.generate()`.
- `_build_schema()` not dynamically including `dm.CURRENT_SEASON`.
- SQL injection via string formatting in NL→SQL Codex pipeline.
- Prompt injection through user-supplied query text.
- API keys read from anywhere except environment variables.

**Security / Permissions:**
- `is_commissioner()` and `is_tsl_owner()` boundary violations.
- Permission decorators missing on commissioner-only commands.
- Snowflake ID confusion vs DB username.

**Architecture / Lifecycle:**
- Cog load order violations (echo_cog and setup_cog MUST load first per CLAUDE.md).
- Duplicate `load_all()` on reconnect without `_startup_done` guard.
- Imports from `QUARANTINE/` (dead files — never reference).
- Missing `ATLAS_VERSION` bump in `bot.py` for behavior changes.
</atlas_specific_attack_surface>
