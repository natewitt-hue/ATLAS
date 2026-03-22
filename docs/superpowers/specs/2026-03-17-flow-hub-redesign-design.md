# Flow Hub Redesign — Design Spec

**Date:** 2026-03-17
**Status:** Draft
**Scope:** Rebuild the `/flow` command as a cohesive, stateful economy dashboard with in-place card swapping, unified HTML/PNG rendering for all views, and contextual navigation.

---

## Context

The FLOW module is the economy hub for TSL — sportsbook, casino, prediction markets, and wallet all run through it. Currently, the `/flow` command renders a polished HTML/PNG hub card, but every button scatters the user to a different cog with inconsistent rendering:

- **My Bets** → Discord embed (sportsbook only, no casino/predictions)
- **Portfolio** → Discord embed (predictions only)
- **Wallet** → Discord embed (flat transaction list with emoji prefixes)
- **Leaderboard** → Discord embed (balance-only ranking)

The result feels like a launcher for separate mini-apps, not a cohesive financial dashboard. This redesign unifies the experience.

---

## Design Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Scope | Full redesign (visual + informational + navigational) | All three layers of inconsistency addressed together |
| My Bets vs Portfolio | Keep separate | Different products — sportsbook bets resolve against scores, predictions against real-world events |
| Wallet depth | Clean ledger card | Balance hero + transaction table. No spending insights (YAGNI) |
| Navigation | In-place card swap | Single message, buttons swap the PNG attachment. Feels like tabs in one dashboard |
| Button layout | Two-tier | Row 1 = view tabs (swap card), Row 2 = contextual actions/module launchers |
| Leaderboard | Multi-stat | Balance + ROI + Win Rate columns, viewer's row highlighted |
| Button state | Contextual | Row 1 always visible (active tab highlighted blurple), Row 2 morphs per active view |
| Implementation | Hub-First | Build the hub system + 4 new card renderers. Don't touch working module sub-hubs |

---

## Architecture

### State Machine

The Flow Hub is a **stateful single-message dashboard** with 5 view states:

```
                    ┌─────────────┐
                    │  dashboard  │ (default on /flow)
                    └──────┬──────┘
              ┌────────┬───┴───┬────────┐
              ▼        ▼       ▼        ▼
         ┌─────────┐ ┌────┐ ┌──────┐ ┌─────────────┐
         │ my_bets │ │wall│ │portf.│ │ leaderboard │
         └─────────┘ └────┘ └──────┘ └─────────────┘
```

Each state defines:
1. **Card renderer** — which async function builds the HTML/PNG
2. **Row 2 buttons** — contextual actions for that view

All transitions are in-place: `interaction.response.edit_message(attachments=[new_png], view=new_view)`

### Button Layout

**Row 1 — View Tabs (always visible, 5 buttons):**

| Button | Style (active) | Style (inactive) | Action |
|--------|---------------|-------------------|--------|
| 📊 Dashboard | `primary` (blurple) | `secondary` (gray) | Swap to dashboard card |
| 📋 My Bets | `primary` | `secondary` | Swap to my bets card |
| 📈 Portfolio | `primary` | `secondary` | Swap to portfolio card |
| 💰 Wallet | `primary` | `secondary` | Swap to wallet card |
| 🏆 Leaderboard | `primary` | `secondary` | Swap to leaderboard card |

**Row 2 — Contextual Actions (varies by active state):**

| Active State | Row 2 Buttons |
|-------------|---------------|
| Dashboard | `🏈 Sportsbook` · `🎰 Casino` · `🔮 Markets` · `🎟️ Scratch` |
| My Bets | `📅 Bet History` · `🏈 Sportsbook` · `🛒 Parlay Cart` |
| Portfolio | `🔍 Browse Markets` · `🔮 Markets` |
| Wallet | `📊 Eco Health` (admin only, hidden for non-admins) |
| Leaderboard | `🏈 Sportsbook` · `🎰 Casino` · `🔮 Markets` |

Row 2 module launchers (Sportsbook, Casino, Markets, Scratch) open their existing sub-hub flows as separate messages. They do NOT swap the card.

### View Class

A single `FlowHubView(discord.ui.View)` replaces the current `FlowHubView`:

```
FlowHubView
├── __init__(self, bot, user_id, active_state="dashboard")
├── state: str  ("dashboard" | "my_bets" | "portfolio" | "wallet" | "leaderboard")
├── _rebuild_buttons()  — sets Row 1 styles + Row 2 based on state
├── _render_card()      — dispatches to the correct card renderer
├── _swap_to(interaction, new_state)  — renders card + edits message
│
├── Row 1 callbacks:
│   ├── dashboard_btn()
│   ├── my_bets_btn()
│   ├── portfolio_btn()
│   ├── wallet_btn()
│   └── leaderboard_btn()
│
└── Row 2 callbacks (state-dependent):
    ├── sportsbook_btn()  — launches SportsbookHubView
    ├── casino_btn()      — launches CasinoHubView
    ├── markets_btn()     — launches Polymarket
    ├── scratch_btn()     — triggers daily scratch
    ├── bet_history_btn() — shows bet history (ephemeral)
    ├── parlay_cart_btn() — opens parlay cart
    ├── browse_markets_btn() — opens market browser
    └── eco_health_btn()  — shows eco health (admin, ephemeral)
```

---

## Card Designs

All cards render through `atlas_html_engine` (`wrap_card()` → `render_card()`), 700px width, 2x DPI, using `atlas_style_tokens` CSS variables.

### 1. Dashboard Card (existing, minor updates)

**File:** `flow_cards.py` (existing `build_flow_card()`)

Keep the current Flow Hub card largely as-is:
- Gold status bar (top 10) / green (positive) / red (negative)
- Header: 💰 ATLAS FLOW · ECONOMY HUB
- Balance hero (56px display) + weekly delta
- 7-day sparkline SVG
- 2×2 stat grid: Lifetime Record | Win Rate | Total Wagered | Leaderboard Rank
- 2×1 info panel: Active Positions | ROI
- Footer: "ATLAS Flow Economy"

**Minor update:** Remove the navigation pill row from the card footer (Sportsbook · Casino · Markets · Wallet) since that's now handled by buttons.

### 2. My Bets Card (new)

**New file:** `flow_cards.py` (add `build_my_bets_card()`)

- **Status bar:** Green (net positive exposure) / Red (net negative) / Gold (no bets)
- **Header:** 📋 MY BETS · player name · "ACTIVE POSITIONS" badge
- **Summary panel:** 2-column glass panel
  - Balance: $X,XXX
  - Pending: N bets
- **Straight Bets section:**
  - Section label: "STRAIGHT BETS" (gold, uppercase, small)
  - Each bet row:
    - Team name (from `pick` column) + bet type (ML/Spread/O-U)
    - Wager amount | Potential win
    - Separator line between bets
- **Parlays section:**
  - Section label: "PARLAYS"
  - Each parlay:
    - Leg count + combined odds
    - Wager | Potential payout
    - Leg status row: ✔ (graded win) / ✗ (graded loss) / ○ (pending) per leg
- **Exposure footer:** 2-column glass panel
  - Total At Risk: $XXX
  - Max Payout: $XXX
- **Empty state:** "No active bets. Hit /sportsbook to place some!" (centered, muted text)

**Data source:** `bets_table` (WHERE status='Pending' — note title case) + `parlays_table` (WHERE status='Pending') from `flow_economy.db`. Column: `discord_id` (INTEGER).

**Potential win calculation:** American odds → payout: if odds > 0: `wager * odds / 100`; if odds < 0: `wager * 100 / abs(odds)`. The `odds` column in `bets_table` stores American odds as integer.

**Team name derivation:** Parse `pick` column (e.g., "Bears") or extract from `matchup` column (e.g., "Bears vs Lions"). No sport icon needed — TSL is Madden-only.

**Parlay leg status:** Join `bets_table` via `parlay_id` to get individual leg grading status.

### 3. Portfolio Card (new)

**New file:** `flow_cards.py` (add `build_portfolio_card()`)

- **Status bar:** Green (profitable) / Red (not) / Gold (no positions)
- **Header:** 📈 PORTFOLIO · player name · badge showing open count
- **Position rows:** Each prediction contract:
  - Side badge: YES (green pill) / NO (red pill)
  - Market title (truncated to ~2 lines)
  - Quantity + cost paid
  - Potential payout
  - Status indicator: 🟢 Open / 🏆 Won / 💀 Lost
- **Summary footer:** 2-column glass panel
  - Total Invested: $XXX
  - Max Payout: $XXX
- **Balance footer:** Current balance
- **Empty state:** "No open positions. Browse /markets to find opportunities!"

**Data source:** `prediction_contracts` (WHERE status IN ('open','won','lost') — show recent resolved too) from `flow_economy.db`. **Important:** This table uses `user_id` (TEXT, stringified Discord ID), NOT `discord_id` (INTEGER) like `bets_table`.

### 4. Wallet / Ledger Card (new)

**New file:** `flow_cards.py` (add `build_wallet_card()`)

- **Status bar:** Green (above $1,000 start) / Red (below)
- **Header:** 💰 WALLET · player name
- **Balance hero:** Large centered balance (42px) + weekly delta (+/- colored)
- **Transaction table:** Last 10-15 transactions
  - Source icon column: Color-coded circle/pill per source
    - 🎰 Casino (purple)
    - 🏈 Sportsbook (green)
    - 🔮 Prediction (blue)
    - 👑 Admin (gold)
    - 💵 Stipend (teal)
  - Description column (e.g., "Slots win", "Bet: Bears ML")
  - Amount column: Green (+$80) / Red (-$100)
  - Running balance column (use `balance_after` column directly, no computation needed)
- **Footer:** "ATLAS Flow Economy"

**Data source:** `transactions` table (ORDER BY created_at DESC LIMIT 15) from `flow_economy.db`. The `balance_after` column stores the post-transaction balance. The `source` column identifies the transaction type (TSL_BET, CASINO, PREDICTION, ADMIN, STIPEND).

### 5. Leaderboard Card (new)

**New file:** `flow_cards.py` (add `build_leaderboard_card()`)

- **Status bar:** Gold gradient (always)
- **Header:** 🏆 LEADERBOARD · "TSL FLOW RANKINGS"
- **Table header row:** # | Name | Balance | ROI | Win Rate (muted, uppercase, small)
- **Top 10 rows:** Each user:
  - Rank: #1 🥇, #2 🥈, #3 🥉, #4-10 plain
  - Name (truncated)
  - Balance (abbreviated: $2.8k)
  - ROI % (green if positive, red if negative)
  - Win Rate % (colored)
- **Viewer highlight:** User's own row with distinct gold border/background
  - If user is outside top 10, show at bottom: "▶ YOU: #X | $X,XXX | +XX% | XX%"
- **Footer:** "ATLAS Flow Economy"

**Data source:** `users_table` + aggregated stats from `flow_economy.db`

**Aggregation logic:**
- **Balance:** `users_table.balance`
- **ROI:** `(balance - 1000) / 1000 * 100` (all users start at $1,000)
- **Win Rate:** Sportsbook only — `COUNT(status='Won') / COUNT(status IN ('Won','Lost'))` from `bets_table` WHERE `discord_id = user`. Prediction contracts excluded since they're a different product.
- **Ranking:** ORDER BY `balance` DESC

---

## Technical Details

### Rendering Pipeline

All new cards follow the established pattern:

```python
async def build_my_bets_card(user_id: int, db_path: str) -> bytes:
    # 1. Query data from flow_economy.db
    # 2. Build HTML body string
    # 3. status_class = determine_status(data)
    # 4. full_html = wrap_card(body_html, status_class)
    # 5. return await render_card(full_html)
```

### Files Modified

| File | Change |
|------|--------|
| `economy_cog.py` | Replace `FlowHubView` class with new stateful version. Update `/flow` command to use new view. |
| `flow_cards.py` | Add `build_my_bets_card()`, `build_portfolio_card()`, `build_wallet_card()`, `build_leaderboard_card()`. Minor update to `build_flow_card()` (remove nav pills). |

### Files NOT Modified

- `flow_sportsbook.py` — Sportsbook hub/games untouched
- `casino/` — Casino hub/games untouched
- `polymarket_cog.py` — Markets hub untouched
- `atlas_html_engine.py` — No engine changes needed
- `atlas_style_tokens.py` — May add source-type colors (casino purple, sportsbook green, prediction blue, admin gold, stipend teal) for the Wallet card. Minor addition if needed.

### Discord API Considerations

- **Message editing:** `interaction.response.edit_message()` supports swapping `attachments` and `view` simultaneously
- **Button limits:** Discord allows max 5 buttons per row, max 5 rows. We use 2 rows (5 tabs + up to 4 contextual) = well within limits
- **View timeout:** Set `timeout=300` (5 min) on the view. After timeout, buttons go dormant. User re-runs `/flow` to get a fresh hub.
- **Concurrency:** Only the user who invoked `/flow` can interact with their hub (check `interaction.user.id == self.user_id`)
- **Response pattern:** Use `interaction.response.edit_message()` directly for tab swaps (200-500ms render is well under 3s limit). No defer needed for normal tab transitions.
- **Persistent view migration:** The current `FlowHubView` uses `timeout=None` and is registered as a persistent view. The new stateful view uses `timeout=300` and holds instance state (`user_id`, `active_state`). This is an intentional breaking change — old `/flow` messages from before the update will have dead buttons. Users re-run `/flow` to get a fresh hub. Remove the `bot.add_view(FlowHubView())` persistent view registration.

---

## Verification Plan

1. **Visual:** Run each card renderer standalone and compare output PNGs against design spec
2. **Navigation:** Test all 5 tab transitions — verify card swaps and button rows update correctly
3. **State integrity:** Rapidly click between tabs — verify no race conditions or stale state
4. **Empty states:** Test each card with a user who has no bets/positions/transactions
5. **Edge cases:** User with 0 balance, user not in top 10 leaderboard, user with 15+ transactions
6. **Admin buttons:** Verify Eco Health button only appears for commissioners
7. **Module launchers:** Verify Sportsbook/Casino/Markets/Scratch buttons still open their sub-hubs correctly
8. **Timeout:** Let view expire after 5 min — verify graceful degradation
9. **Mobile:** Verify 700px cards render readable on mobile Discord (the whole point of the redesign)
