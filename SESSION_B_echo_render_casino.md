# GAP Review Session B — Echo, Render & Casino

**Goal:** Deep line-by-line code review only. No code changes. Produce a handoff document for CLAUDEFROG (local desktop Claude) with bugs, risks, and exact fix instructions.

**Output:** When done, create `HANDOFF_echo_render_casino_fixes.md` with the same format as `HANDOFF_data_pipeline_fixes.md` (already on the branch for reference).

---

## Files to Review (exclusive — no other session touches these)

| File | Focus |
|------|-------|
| `echo_cog.py` | Persona system, `infer_context()` channel→voice mapping, @mention handling |
| `echo_loader.py` | `get_persona()`, voice file loading from `echo/*.txt`, fallback behavior |
| `affinity.py` | Affinity scoring logic, how scores are computed and stored |
| `echo/*.txt` | All persona text files — casual, official, analytical voices |
| `atlas_style_tokens.py` | Color constants, font definitions, spacing, layout tokens |
| `atlas_html_engine.py` | Playwright page pool, `render_card()`, `wrap_card()`, browser lifecycle |
| `card_renderer.py` | Trade card rendering — follows pipeline pattern? |
| `casino/` directory | All casino game logic + renderers (blackjack, slots, crash, coinflip, scratch) |

---

## Review Checklist

### echo_cog.py + echo_loader.py
- [ ] `get_persona()` — does it load from files or is it hardcoded?
- [ ] `infer_context()` — what's the channel name → voice mapping? Edge cases?
- [ ] What happens if `echo/*.txt` files are missing?
- [ ] Is the persona always "3rd person as ATLAS" per CLAUDE.md? Any "I"/"me" leaks?
- [ ] Are there any hardcoded persona strings anywhere that should use `get_persona()`?
- [ ] Thread safety — multiple concurrent calls to `get_persona()`?

### affinity.py
- [ ] How are affinity scores computed?
- [ ] Where are they stored? sportsbook.db?
- [ ] Any integer overflow or division-by-zero risks?
- [ ] Is the scoring formula documented?

### atlas_style_tokens.py
- [ ] Any duplicate token names or conflicting values?
- [ ] Are all tokens actually used? (dead tokens = confusion)
- [ ] Color values — any accessibility issues (contrast)?
- [ ] Is this the single source of truth per CLAUDE.md?

### atlas_html_engine.py
- [ ] Page pool — how many pages? Pre-warmed? (should be 4 per CLAUDE.md)
- [ ] `render_card()` — what's the error handling if Playwright crashes?
- [ ] Memory leaks — are pages properly released back to the pool?
- [ ] What happens if the pool is exhausted (all 4 pages busy)?
- [ ] `wrap_card()` — does it enforce 480px width, 2x DPI, `domcontentloaded` wait?
- [ ] Browser lifecycle — when does it start/stop? Graceful shutdown?
- [ ] Concurrent rendering — thread safety of the page pool?

### card_renderer.py
- [ ] Does it use the `wrap_card()` → `render_card()` pipeline?
- [ ] Or does it bypass the engine and call Playwright directly?
- [ ] Any imports from QUARANTINE files?

### casino/ directory
- [ ] All renderers — do they use `atlas_html_engine.py` pipeline?
- [ ] Game logic — any edge cases in blackjack (split aces, insurance), slots (RNG fairness), crash (multiplier overflow)?
- [ ] Economy integration — do bets/payouts go through economy_cog properly?
- [ ] Any direct Pillow/PIL imports? (should be HTML-only per CLAUDE.md)
- [ ] Renderer files: `casino_html_renderer.py`, `highlight_renderer.py`, `session_recap_renderer.py`, `pulse_renderer.py`, `prediction_html_renderer.py`, `ledger_renderer.py`

---

## Discord API Constraints Relevant to This Session

- `view=None` cannot be passed to `followup.send()`
- Select menus capped at 25 options
- Embed descriptions max 4096 chars — check `_truncate_for_embed()` usage
- File attachments for rendered PNGs — size limits?

---

## CLAUDE.md Rules to Verify

- Pipeline: Build HTML body → `wrap_card(body, status)` → `render_card(html)` → PNG bytes
- Width: 480px · DPI: 2x · Wait: `domcontentloaded` · Pool: 4 pre-warmed pages
- `QUARANTINE/atlas_card_renderer.py` and `QUARANTINE/card_renderer.py` must NOT be imported
- `get_persona()` from `echo_loader.py` — never hardcode persona strings
- Voice rules: Always 3rd person as "ATLAS", punchy 2-4 sentences, no bullet lists
