# ATLAS Sportsbook UX — Design Handoff

**Date:** 2026-03-24
**Type:** Handoff — prior session did initial UX review; new session should validate, deepen bugs, and design improvements
**Branch context:** `main` (post PR #23 — `_parse_commence` tipoff fix merged)

---

## Mission for New Session

1. **Validate** the findings below by reading the actual code (don't trust this doc blindly — verify line numbers)
2. **Find additional bugs** — especially around parlay grading, real-sport edge cases, and workspace state leakage
3. **Design concrete UX improvements** — the goal is a cleaner, more discoverable betting experience, especially for parlays
4. **Produce a prioritized redesign spec** grounded in what's actually in the codebase

---

## Codebase Context

| File | Lines | Purpose |
|------|-------|---------|
| `flow_sportsbook.py` | ~3,800 | Primary — all Views, SportsbookWorkspace state machine, parlay cart, TSL betting, hub |
| `real_sportsbook_cog.py` | ~1,250 | Real sports — ESPN odds sync, real bet placement, `_parse_commence`, `EventListView` (may be dead) |
| `sportsbook_cards.py` | ~1,480 | All card renders — hub card, match detail, confirmation, parlay analytics |
| `flow_wallet.py` | — | Balance ops, user locks for atomic transactions |
| `sportsbook_core.py` | — | flow.db bet/parlay persistence (mirror target) |

**Key databases:**
- `sportsbook.db` — bets, parlays, parlay_legs, parlay_cart, games_state
- `flow_economy.db` — primary economy DB (real_events synced here — confirm no dual-DB split)

---

## Current Flow (As Mapped)

```
/sportsbook
  → SportsbookHubView (flow_sportsbook.py:2411)
      Row 0: [TSL] [NFL] [NBA] [MLB] [NHL]
      Row 1: [NCAAB] [UFC] [EPL] [MLS] [WNBA]
      Row 2: [Stats] [Leaderboard] [Cart]
      Row 3: [Parlay Stats]
      ↓ click sport

  → SportsbookWorkspace — Sport Selector state (flow_sportsbook.py:1678)
      Row 0: [TSL] [NFL] [NBA] [MLB] [NHL]     ← SAME BUTTONS AS HUB (redundant screen)
      Row 1: [NCAAB] [UFC] [EPL] [MLS] [WNBA]
      Row 2: [💰 Submit (N legs)] [🗑️ Clear]   ← only if cart has legs
      ↓ click sport (again) → game list loads

  → Game List (show_tsl_games / show_real_games)
      Row 0: [Select Game ▼]  (25 games max, NO PAGINATION)
      Row 1 or 2: [🔙 Back] [💰 Submit] [🗑️ Clear]   ← row varies by sport type
      ↓ select game

  → Match Detail (show_tsl_match / show_real_match)
      HTML→PNG card image
      Row 0: [Away Spread]   [Home Spread]
      Row 1: [Away ML]       [Home ML]
      Row 2: [OVER]          [UNDER]
      Row 3: [🔙 Back]
      ↓ click bet type

  → Wager Screen (show_wager, flow_sportsbook.py:2034)
      Row 0: [$50] [$100] [$250] [$500] [$1000]
      Row 1: [✏️ Custom]   [🎰 Add to Parlay]   ← PROBLEM: different flows on same row
      Row 2: [🔙 Back]

STRAIGHT BET PATH: click preset or Custom → modal → _place_straight_bet() → PNG confirmation
PARLAY PATH: click "Add to Parlay" → leg added to parlay_cart (DB) → returns to sport selector
             repeat until ≥2 legs → Submit → ParlayWagerModal → PNG confirmation
```

**Hub card note:** `build_sportsbook_card()` in `sportsbook_cards.py` generates a rich personalized dashboard (balance, sparkline, W/L, open bets) — but `/sportsbook` command uses a plain embed. The card is **never called**.

---

## Bugs to Verify

### Bug 1 — Missing `source` column in `parlay_legs` schema
- **Where:** `flow_sportsbook.py` — the CREATE TABLE block for `parlay_legs` (~line 184) vs the INSERT statement (~line 1313)
- **Claim:** The INSERT includes a `source` column but the table definition doesn't declare it
- **How to verify:** Read both blocks, confirm column mismatch
- **Impact if real:** Any real-sport or cross-sport parlay INSERT will error; grading can't identify sport per leg

### Bug 2 — `real_events` table may exist in two databases
- **Where:** `real_sportsbook_cog.py` creates `real_events`; `flow_sportsbook.py` queries it
- **Claim:** These may reference different DB files, silently splitting event data
- **How to verify:** Grep for `real_events` table creation and all SELECT/INSERT queries — confirm same DB path throughout
- **Impact if real:** Workspace shows events from one DB; odds sync writes to the other; user bets on games that aren't actually being tracked

### Bug 3 — `EventListView` may be dead code
- **Where:** `real_sportsbook_cog.py` ~line 683 defines it; `flow_sportsbook.py:59` imports it
- **Claim:** The workspace reimplements game selection inline (`show_real_games()`), making `EventListView` orphaned
- **How to verify:** Find every call site of `EventListView` — is it instantiated anywhere?
- **Impact if real:** Dead import + potential divergence; if it IS used somewhere, compare behavior to workspace inline implementation

### Bug 4 — Stale odds on bet placement
- **Where:** `show_real_games()` (flow_sportsbook.py ~line 1878) fetches events from DB
- **Claim:** Odds are queried once on game list load; if user spends 5 min drilling down, they bet at stale odds
- **How to verify:** Read the query path from game list → match detail → wager → placement. Is there a fresh DB read before writing the bet?
- **Impact if real:** User submits a bet thinking they're getting -110; actual line shifted; acceptance at wrong odds

### Bug 5 — Back button row inconsistency
- **Claim:** Back button appears at different row numbers on different screens
  - TSL game list: row 1
  - Real game list: row 2
  - TSL match detail: row 3
  - Real match detail: row 2
  - Wager screen: row 2
- **How to verify:** Read each `show_*` method, find the Back button's `row=` parameter
- **Impact:** Users can't muscle-memorize the back button position

---

## UX Problems to Validate & Design Solutions For

### P1 — Parlay is not discoverable (Critical)
"Add to Parlay" is buried at screen 4 of 4. No "Build Parlay" entry at the hub. Users who want a parlay must already know the flow or stumble on it.

Modern sportsbooks (FanDuel, DK) make parlay a first-class mode at entry. The choice between "straight bet" and "parlay" should happen at the hub, not at the money screen.

**Design question:** Should "Parlay" be a hub button that activates parlay mode? Or a full separate flow? Or a toggle on the wager screen that's more prominent?

### P2 — Redundant sport selector (Major)
Hub shows 10 sport buttons. First click opens workspace, which shows the exact same 10 buttons again. The transition is invisible — user likely doesn't know the first click did anything.

**Design question:** Should hub clicks skip directly to the game list? What does the workspace sport nav look like if users can change sport mid-session?

### P3 — Wager screen mixes straight bet and parlay actions (Major)
Row 1 has `[Custom]` (still a straight bet, just different amount) next to `[Add to Parlay]` (completely different flow and destination). These are not equivalent choices and shouldn't be siblings.

**Design question:** How should the wager screen visually separate "place straight bet" from "add to parlay cart"?

### P4 — No cart badge on hub (Moderate)
"Cart" button always says "Cart" regardless of how many legs are in it. User building a 5-leg parlay across sessions has no ambient indicator.

**Design suggestion:** `🛒 Cart [3]` with green styling when non-empty.

### P5 — One-click cart destruction (Moderate)
`🗑️ Clear` wipes the entire parlay in one click. No confirmation.

**Design suggestion:** Confirm modal with leg count: "Clear all 5 legs from your parlay? This can't be undone."

### P6 — Two submission paths with different quality (Moderate)
- Workspace Submit → straight to `ParlayWagerModal` (no leg review)
- Hub Cart → `ParlayCartView` → review screen → `ParlayWagerModal`

Path 1 skips the leg review. Users submitting from the workspace don't see a summary before committing.

**Design suggestion:** Consolidate — all submission paths go through cart review first.

### P7 — Hub card dead code (Minor/Enhancement)
`build_sportsbook_card()` exists and generates a rich personalized dashboard. It's never called. The hub is a plain embed.

**Design question:** Should this be activated? What data is most valuable at the hub entry point?

### P8 — No "My Bets" on the hub (Enhancement)
To see active bets, users need to know a separate command. There's no "My Bets" button on the sportsbook hub.

---

## Design Goals for New Session to Target

- **Parlay as first-class citizen** — visible, intentional, not a hidden alternative
- **Fewer clicks** — current: 4-5 to place a straight bet. Target: 3. Current: 12+ interactions for a 3-leg parlay. Target: ~8.
- **Consistent back button** — same row on every screen
- **One parlay submission path** — always shows cart review before modal
- **Live cart feedback** — running combined odds visible as legs are added
- **No destructive one-click actions** — Clear Cart must confirm

---

## Suggested Investigation Order for New Session

```
1. Read flow_sportsbook.py:184-210          # DB schema block — verify parlay_legs columns
2. Read flow_sportsbook.py:1268-1420        # ParlayWagerModal.on_submit() — full write path
3. Read flow_sportsbook.py:1678-2100        # SportsbookWorkspace — all states + transitions
4. Read real_sportsbook_cog.py:683-780      # EventListView — confirm dead or alive
5. Grep `real_events` across both files     # Confirm single DB path
6. Read sportsbook_cards.py                 # Card inventory — confirm build_sportsbook_card unused
7. Grep `_back_to_sports|_back_to_tsl|_nav_real` # Confirm back button rows
```

---

## Output Expected from New Session

1. **Bug verification report** — confirm/deny each bug above with exact evidence (file:line)
2. **Additional bugs found** — anything the initial review missed
3. **UX redesign spec** — for each problem above, a concrete proposal:
   - What the new flow looks like (step by step)
   - What Views/buttons change
   - Any new View classes or states needed
4. **Prioritized implementation plan** — what to build first, what's a quick win vs. a bigger refactor
