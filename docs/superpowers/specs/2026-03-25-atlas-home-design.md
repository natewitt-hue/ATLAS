# ATLAS Home, GOD Module & Theme Compliance — Design Spec

**Date:** 2026-03-25
**Status:** Draft
**Scope:** New `/atlas` user home command, new `/god` module, admin command migration, Oracle theme fix

---

## 1. Overview

Three coordinated changes:

1. **`/atlas` User Home** — A new top-level slash command that renders a comprehensive "baseball card" PNG of the user's stats across all ATLAS modules, with navigation buttons to every module and a theme cycling UI.
2. **Admin Command Migration** — Move existing `/atlas` admin subcommands to `/boss` (commissioner-level) and a new `/god` module (GOD-role-gated). Delete `echostatus`.
3. **Oracle Theme Compliance** — Wire `theme_id` through the Oracle renderer, the only renderer not currently theme-aware.

---

## 2. `/atlas` User Home

### 2.1 Command Definition

- **Command:** `/atlas` (top-level, no subcommands)
- **Access:** All users
- **Response:** Ephemeral — renders a PNG baseball card + attaches a `discord.ui.View` with module buttons
- **New files:**
  - `atlas_home_cog.py` — Cog with the `/atlas` command and interactive views
  - `atlas_home_renderer.py` — HTML→PNG renderer for the baseball card

### 2.2 Baseball Card Layout

Full baseball card with centered hero header and dedicated sections per module. Renders at 700px wide, 2x DPI, theme-aware via `wrap_card()`.

**Hero Header (centered):**
- Display name
- Role badge (TSL Owner / Commissioner / GOD)
- Rank out of total users
- Balance with weekly delta
- Season ROI percentage
- Current streak (W/L from last results)

**Economy Section (3-col grid):**
- Overall record (W-L-P)
- Win rate percentage
- Net P&L from season start

**Sportsbook Section (3-col grid):**
- TSL betting record
- Best parlay odds hit
- Real sports record

**Casino Section (3-col grid):**
- Total sessions played
- Biggest single win
- Favorite game (most played)

**Predictions Section (3-col grid):**
- Accuracy rate
- Markets participated
- Net prediction P&L

**Footer:**
- ATLAS branding + active theme name + season number

### 2.3 Data Sources

| Data | Source | Table/Query |
|------|--------|-------------|
| Balance, record, W/L/P, streak, theme | `flow_economy.db` | `users_table`, `bets_table` |
| Weekly delta | `flow_economy.db` | `balance_snapshots` |
| Season start balance, ROI | `flow_economy.db` | `users_table.season_start_balance` |
| Rank | `flow_economy.db` | `users_table ORDER BY balance DESC` |
| TSL sportsbook record | `flow_economy.db` | `bets_table` (TSL bets) |
| Real sports record | `flow_economy.db` | `bets_table` (real sports bets) |
| Best parlay | `flow_economy.db` | `bets_table` / `parlay_table` |
| Casino sessions, biggest win, fav game | `flow_economy.db` | `casino_sessions`, `bets_table` (casino category) |
| Prediction accuracy, markets, P&L | `flow_economy.db` | `prediction_positions`, `prediction_markets` |

Data gathering runs in a thread pool executor (same pattern as `flow_cards.py:_gather_flow_data`).

### 2.4 Interactive View

`discord.ui.View` attached to the ephemeral message with the following buttons:

| Button | Label | Style | Action |
|--------|-------|-------|--------|
| Oracle | `🔮 Oracle` | Secondary | Sends info embed pointing to `/oracle` |
| Genesis | `⚔️ Genesis` | Secondary | Sends info embed pointing to `/trade` |
| Flow | `💰 Flow` | Secondary | Sends info embed pointing to `/flow` |
| Sportsbook | `🏈 Sportsbook` | Secondary | Sends info embed pointing to `/sportsbook` |
| Casino | `🎰 Casino` | Secondary | Sends info embed pointing to `/casino` |
| Predictions | `🔮 Predictions` | Secondary | Sends info embed pointing to `/predictions` |
| Theme | `🎨 Theme` | Primary | Opens theme cycling UI |

**Module buttons:** Each sends an ephemeral follow-up with a brief description and usage hint for that module. Keeps users in the home context rather than auto-invoking other commands (which would require complex cross-cog wiring).

### 2.5 Theme Cycling UI

When the user clicks the Theme button:

1. Bot edits the message to show the baseball card re-rendered with the **next** theme
2. View changes to: `⬅️ Prev` | `➡️ Next` | `✅ Apply` | `❌ Cancel`
3. Prev/Next cycle through the theme list, re-rendering the card each time
4. **Apply** — calls `set_theme(user_id, theme_id)`, confirms with a brief message, returns to the home view
5. **Cancel** — reverts to original theme render, returns to the home view

Theme list comes from `atlas_themes.THEMES.keys()`. The preview render uses the candidate `theme_id` directly without persisting until Apply.

---

## 3. Admin Command Migration

### 3.1 Commands Moving to `/boss`

These are commissioner-level operations. `boss_cog.py` already has commissioner permission checks.

| New Command | Old Command | Implementation |
|-------------|-------------|----------------|
| `/boss sync` | `/atlas sync` | Move `_sync_impl()` to `boss_cog.py` |
| `/boss clearsync` | `/atlas clearsync` | Move clearsync logic to `boss_cog.py` |
| `/boss status` | `/atlas status` | Move status logic to `boss_cog.py` |

### 3.2 Commands Moving to `/god`

| New Command | Old Command | Implementation |
|-------------|-------------|----------------|
| `/god affinity` | `/atlas affinity` | Move to `god_cog.py` |
| `/god rebuilddb` | `/atlas rebuilddb` | Move `_rebuilddb_impl()` to `god_cog.py` |

### 3.3 Deleted

- `/atlas echostatus` — removed entirely, no replacement
- The `echostatus` command is registered in `echo_cog.py` (lines 49-60), which imports `atlas_group` from `bot.py`. The `setup()` function's `atlas_group` registration block and `_echostatus_impl` method must be removed from `echo_cog.py` to prevent a crash when `atlas_group` is deleted from `bot.py`.

### 3.4 Cleanup in `bot.py`

Remove:
- `atlas_group` definition (lines ~649-653)
- All `@atlas_group.command` functions (lines ~658-736)
- `bot.tree.add_command(atlas_group)` (line ~738)
- `_bot_start_time` variable (moves to `boss_cog.py` or `god_cog.py` as needed)
- `_sync_impl()` and `_rebuilddb_impl()` functions (move to respective cogs)

---

## 4. `/god` Module

### 4.1 New Files

- `god_cog.py` — New cog for GOD-role-gated commands

### 4.2 Permission Model

**New role hierarchy:**
```
GOD  →  Commissioner  →  TSL Owner  →  User
```

**New functions in `permissions.py`:**
- `is_god(interaction: discord.Interaction) -> bool` — checks for "GOD" Discord role on the member
- `require_god()` — decorator form, returns ephemeral denial if the user lacks the GOD role
- GOD role implicitly has all commissioner powers (but commissioner does NOT have GOD powers)

### 4.3 Cog Structure

```python
god_group = app_commands.Group(
    name="god",
    description="ATLAS God-tier administration.",
)
```

The group-level check uses `require_god()` so all subcommands inherit the permission gate.

### 4.4 Cog Load Order

`god_cog` loads after `boss_cog` (position 16 in the load order). It has no dependencies on other cogs beyond `permissions.py` and core data modules.

---

## 5. Oracle Theme Compliance

### 5.1 Current Problem

`oracle_renderer.py` is the only renderer not passing `theme_id` to `wrap_card()`:
```python
# render_oracle_card() — missing theme_id
full_html = wrap_card(body_with_css, "")
```

### 5.2 Fix

**`oracle_renderer.py`:**
- `render_oracle_card(result)` → `render_oracle_card(result, *, theme_id: str | None = None)`
- `render_oracle_card_to_file(result)` → `render_oracle_card_to_file(result, *, theme_id: str | None = None)`
- Pass `theme_id` through: `wrap_card(body_with_css, "", theme_id=theme_id)`

**`oracle_cog.py`:**
- `_run_and_send()` resolves `get_theme_for_render(interaction.user.id)` and passes `theme_id` to `render_oracle_card_to_file()`

### 5.3 Accent Colors Unchanged

Per-analysis-type accent colors (gold for matchup, rose for rivalry, etc.) are content-semantic and stay hardcoded. The theme controls the card shell (background, panels, overlays, borders, text shades) via `wrap_card()` CSS variable injection.

---

## 6. Files Changed

| File | Change Type | Description |
|------|-------------|-------------|
| `atlas_home_cog.py` | **New** | `/atlas` command, home view, theme cycling UI |
| `atlas_home_renderer.py` | **New** | Baseball card HTML builder + data gathering |
| `god_cog.py` | **New** | `/god` group with `affinity`, `rebuilddb` |
| `permissions.py` | **Modified** | Add `is_god()`, `require_god()` |
| `boss_cog.py` | **Modified** | Add `sync`, `clearsync`, `status` subcommands |
| `bot.py` | **Modified** | Remove `atlas_group` and all admin subcommands, bump version, add `god_cog` to load order |
| `oracle_renderer.py` | **Modified** | Add `theme_id` parameter, pass to `wrap_card()` |
| `oracle_cog.py` | **Modified** | Resolve theme in `_run_and_send()`, pass to renderer |
| `echo_cog.py` | **Modified** | Remove `echostatus` registration block and `_echostatus_impl` |

---

## 7. Non-Goals

- No changes to any renderer other than Oracle — all others already pass `theme_id`
- No new themes — existing 10 themes are sufficient
- No cross-cog command invocation from module buttons — buttons show info embeds, not auto-invoke
- No changes to the theme data model or `flow_wallet.py` theme functions
- No public visibility for the home card — ephemeral only
