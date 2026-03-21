# ATLAS Nightly Session — 2026-03-08

## Project State

ATLAS is at v1.5.0 with 10 cog modules, an Elo-based sportsbook, Polymarket prediction markets, a Codex historical AI, and the Echo voice persona system. The codebase is ~20 Python files totaling ~1.3 MB of source. Overall code quality is solid — error handling is thorough, async patterns are correct, and the exec sandbox in reasoning.py is well-locked-down.

## Changes Made

### 1. `echo_cog.py` — Fixed import placement
- **Problem:** `import os` was at line 154 (bottom of file) instead of at the top with other imports. It worked because Python resolves module-level imports before any function call, but it violated standard conventions and was confusing to read.
- **Fix:** Moved `import os` to the top-of-file import block. Removed the comment that called it an "import guard" — it was just a misplaced import, not a guard.

### 2. `echo_loader.py` — Fixed broken `property()` descriptors
- **Problem:** Lines 208-210 used `property(lambda self: ...)` at module level. `property()` only works as a descriptor inside a class — at module level, these were inert `property` objects that couldn't be called or used as strings. Any cog trying to use `PERSONA_CASUAL` etc. would get a `property` object instead of a persona string.
- **Fix:** Replaced with simple functions (`def PERSONA_CASUAL() -> str`) that call `get_persona()`. These serve as backward-compatible convenience accessors. New code should use `get_persona()` directly.

### 3. `bot.py` — Refactored `setup_hook()` cog loading
- **Problem:** `setup_hook()` had 10 nearly identical try/except blocks (60+ lines of repetitive code). Each block did the same thing: `await bot.load_extension(name)` wrapped in `try/except Exception`. Adding a new cog meant copy-pasting the block.
- **Fix:** Replaced with a single `_EXTENSIONS` list and a 4-line loop. Comments document load-order requirements (echo_cog first, setup_cog second). The list makes it trivial to add/remove/reorder cogs.
- **Side effect:** Two cogs (`awards_cog`, `polymarket_cog`) had their load-print statements in the old bot.py blocks instead of in their own `setup()` functions. Added `print()` calls to both cogs' `setup()` functions so they self-announce like all other cogs.

### 4. `polymarket_cog.py` — Added self-announcing load message
- Replaced `log.info("PolymarketCog loaded.")` with `print("ATLAS: Flow · Polymarket Prediction Markets loaded.")` in `setup()` to match the convention used by all other cogs (print to stdout, not logging).

### 5. `awards_cog.py` — Added self-announcing load message
- Added `print("ATLAS: Awards Engine loaded.")` to `setup()` since it previously relied on bot.py for the print.

## Issues Found (Not Fixed — For Review)

### `kalshi_cog.py` — Stub economy table (line 157)
The `get_balance()` and `update_balance()` functions query a non-existent `economy` table. The polymarket_cog correctly imports from `casino.casino_db` instead. If kalshi_cog is still active, its balance helpers should be wired to `casino_db` the same way polymarket_cog does. If kalshi_cog is deprecated (replaced by polymarket_cog), consider removing it from the project to avoid confusion.

### `README.md` — Outdated
The README still references the old "WittGPT" branding and lists files that no longer exist (`embeds.py`, `sportsbook.py`, `export_receiver.py`, `rules.py`). The architecture diagram, file structure, and command list don't reflect the current v1.5.0 ATLAS codebase with its cog-based architecture. A fresh README would help onboarding and documentation.

### `genesis_cog.py` — TODO at line 2183
"Implement Green Bar Rule validation against system-neutral offer" is still unimplemented. This is documented in the scaffold README as a Phase 5 dependency.

### `codex_cog.py` — Redundant `ATLAS_PERSONA` constant
Lines 51-56 define `ATLAS_PERSONA` which is only used as a fallback if `echo_loader` can't be imported. The fallback in `echo_loader._FALLBACKS["analytical"]` already serves the same purpose. Consider removing the redundant constant to keep a single source of truth.

## Suggested Next Steps

1. **Update README.md** to reflect ATLAS v1.5.0 architecture, current cog list, and correct command reference.
2. **Decide on kalshi_cog.py**: wire to casino_db or remove if deprecated.
3. **Clean up leftover worktrees** in `.claude/worktrees/` (quirky-chebyshev, charming-heisenberg).
4. **Bump version** to 1.5.1 after these fixes are verified in a live test run.
