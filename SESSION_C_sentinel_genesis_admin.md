# GAP Review Session C — Sentinel, Genesis & Admin

**Goal:** Deep line-by-line code review only. No code changes. Produce a handoff document for CLAUDEFROG (local desktop Claude) with bugs, risks, and exact fix instructions.

**Output:** When done, create `HANDOFF_sentinel_genesis_admin_fixes.md` with the same format as `HANDOFF_data_pipeline_fixes.md` (already on the branch for reference).

---

## Files to Review (exclusive — no other session touches these)

| File | Focus |
|------|-------|
| `sentinel_cog.py` | Rule enforcement, blowout monitor integration, compliance checks, 4th down enforcement, force requests |
| `genesis_cog.py` | Trade center, roster management, devTrait mapping, ability budget enforcement, draft tools, parity system |
| `awards_cog.py` | Awards system, voting mechanics, duplicate vote prevention |
| `economy_cog.py` | Balance operations, payouts, stipends, transaction logging |
| `polymarket_cog.py` | Prediction markets, market creation, bet placement, resolution logic |
| `commish_cog.py` | Unified admin commands, delegation to `_impl` methods in other cogs |

---

## Review Checklist

### sentinel_cog.py
- [ ] Blowout monitor — does it consume `dm.flag_stat_padding()` output correctly?
- [ ] 4th down enforcement — what are the rules? Are they correctly implemented?
- [ ] Force requests — channel routing via `FORCE_REQUEST_CHANNEL` env var
- [ ] Complaint system — any abuse vectors? Rate limiting?
- [ ] Does it properly use `status IN ('2','3')` for game filtering?
- [ ] False positive/negative risks in rule enforcement
- [ ] Error handling — does a sentinel failure crash the bot or fail silently?

### genesis_cog.py (CRITICAL — most gotcha-sensitive file)
- [ ] **devTrait mapping**: 0=Normal, 1=Star, 2=Superstar, 3=Superstar X-Factor — verify exact mapping
- [ ] **Ability budgets**: Star=1B, Superstar=1A+1B, XFactor=1S+1A+1B, C-tier unlimited — verify enforcement
- [ ] **Dual-attribute checks**: Must use OR logic, not AND — verify
- [ ] Trade validation — what checks happen before a trade is approved?
- [ ] Does it use `/export/players` for OVR, devTrait, abilities? (not stat-leader endpoints)
- [ ] Does it use `/export/playerAbilities` for ability assignments?
- [ ] Roster management — any off-by-one errors in roster size checks?
- [ ] Parity system — how does it work? Fair to all teams?
- [ ] Draft tools — do they credit picks to the correct team?
- [ ] Owner resolution — does it use `_resolve_owner()` fuzzy lookup?

### awards_cog.py
- [ ] Voting — can a user vote twice? What prevents it?
- [ ] Award categories — hardcoded or configurable?
- [ ] Results tallying — any tie-breaking logic?
- [ ] Ephemeral responses for votes? (should be private)

### economy_cog.py
- [ ] Balance operations — atomic? What prevents double-spend?
- [ ] Payout math — any floating point precision issues? (should use int cents or Decimal)
- [ ] Stipend logic — when do stipends fire? Duplicate prevention?
- [ ] Transaction logging — auditable?
- [ ] Integration with sportsbook.db — proper connection handling?

### polymarket_cog.py
- [ ] Market creation — admin-only?
- [ ] Bet placement — validates balance before accepting?
- [ ] Resolution logic — what happens on dispute? Can markets be voided?
- [ ] Edge cases: market resolves while bets are being placed, negative balances, zero-sum math
- [ ] Payout calculation — any rounding errors?

### commish_cog.py
- [ ] `_impl` delegation — are all delegations wired to the correct cog methods?
- [ ] Admin permission checks — uses `is_commissioner()` consistently?
- [ ] Does it expose any dangerous operations without confirmation?
- [ ] Command naming — any collisions with other cogs?

---

## MaddenStats API Gotchas CRITICAL for This Session

These are the gotchas most likely to cause bugs in genesis_cog.py:

| Rule | Detail | What to Check |
|------|--------|---------------|
| `devTrait` mapping | 0=Normal, 1=Star, 2=Superstar, 3=X-Factor | Verify genesis_cog uses these exact values |
| Ability budgets | Star=1B, SS=1A+1B, XF=1S+1A+1B, C-tier unlimited | Verify budget enforcement logic |
| Dual-attribute checks | Use OR logic, not AND | Verify any ability eligibility checks |
| Full roster data | OVR, devTrait, ability1-6 only from `/export/players` | Verify genesis doesn't use stat-leader endpoints |
| Ability assignments | Use `/export/playerAbilities` endpoint | Verify source of ability data |
| Owner resolution | Fuzzy lookup via `_resolve_owner()` | Verify username matching |
| Completed games | `status IN ('2','3')` not just `'3'` | Check any game queries in sentinel |

---

## Discord API Constraints Relevant to This Session

- `view=None` cannot be passed to `followup.send()` — check all followup calls
- Select menus capped at 25 options — genesis trade menus, polymarket bet options
- `@discord.ui.select` requires `options=[]` even if populated dynamically
- Modals require `defer()` for any operation >3s (Gemini calls, DB queries)
- Ephemeral vs public: drill-downs ephemeral, hub landing embeds public
- Two cogs with same slash command name → second silently fails (check commish_cog)

---

## CLAUDE.md Rules to Verify

- `get_persona()` from `echo_loader.py` — never hardcode persona strings
- `atlas_ai.generate()` for all AI calls — never call SDKs directly
- `is_commissioner()` checks env `ADMIN_USER_IDS`, "Commissioner" role, or guild admin
- `is_tsl_owner()` checks "TSL Owner" role
- Admin delegation — `commish_cog.py` delegates to `_impl` methods in other cogs
- Channel routing — commands use `require_channel()` decorator
