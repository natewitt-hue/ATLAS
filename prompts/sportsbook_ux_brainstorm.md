# ATLAS Real Sportsbook UX Overhaul — Brainstorm Session

## Context

You are working on ATLAS, a Discord bot for The Simulation League (TSL). Working directory: `C:\Users\natew\Desktop\discord_bot`. Read `CLAUDE.md` first.

ATLAS has a unified sportsbook hub (`/sportsbook`) that lets users bet TSL Bucks on both simulated Madden games AND real-world sports (NFL, NBA, MLB, NHL, NCAAB, UFC, EPL, MLS, WNBA) using live ESPN odds.

## The Problem

The real sports betting flow is clunky. Users click through a chain of separate ephemeral messages that stack up in chat. There's no back button, no persistent state, and the UX feels like navigating a phone tree compared to DraftKings' buttery smooth experience.

**Current real sports flow (broken):**
```
Hub button (NBA/NFL/etc)
  → _show_real_sport() defers, creates new ephemeral followup
    → EventListView: select menu picks a game → new ephemeral followup
      → MatchCardView: 6 buttons (ML/Spread/Total × Home/Away) → new ephemeral followup
        → WagerPresetView: pick amount → new ephemeral followup
          → Bet placed
```
Each `→` is a NEW ephemeral message. User sees 4-5 stacked messages. Can't go back. Can't change their mind without starting over.

**TSL sportsbook flow (already fixed — use as reference):**
```
Hub button (TSL)
  → SportsbookWorkspace.open_to_sport() — single ephemeral message
    → Game list (edit in place) → pick game (edit in place) → bet type (edit in place)
    → ← Back at every step (edits same message)
```
The TSL flow uses `SportsbookWorkspace` (flow_sportsbook.py:1687) — a single View that holds state and calls `_update_workspace()` to edit the same message. All navigation is `edit_message`, never `followup.send`. This is the gold standard.

## What Exists

### Files to study:
- `flow_sportsbook.py` — Contains `SportsbookWorkspace` (line 1687) and `SportsbookHubView` (line 2603). The workspace pattern is battle-tested for TSL games. **This is your reference architecture.**
- `real_sportsbook_cog.py` — Contains the broken views: `EventListView` (722), `MatchCardView` (823), `BetTypeView` (939), `PickSelectView` (1055), `CustomRealWagerModal` (1230). **This is what needs to be refactored.**
- `sportsbook_cards.py` — `build_real_match_detail_card()` renders the match detail PNG. Keep this.
- `espn_odds.py` — ESPN odds client. Don't touch.
- `team_branding.py` — Team colors/logos. Don't touch.
- `odds_utils.py` — Shared math. Don't touch.

### SportsbookWorkspace pattern (the answer):
```python
class SportsbookWorkspace(discord.ui.View):
    """Edit-in-place workspace for sportsbook drill-downs.
    All child states render into a single ephemeral message.
    Navigation never spawns new messages — it calls _refresh().
    """
    async def _update_workspace(self, interaction, embed, view, *, file=None):
        """Edit the workspace message in-place."""
        if not interaction.response.is_done():
            await interaction.response.edit_message(**kwargs)
        else:
            await interaction.edit_original_response(**kwargs)
```
Key insight: the workspace already handles real sports partially — `open_to_sport()` routes to `show_real_events()`. But then it delegates to the old `EventListView`/`MatchCardView` chain which spawns new messages.

### Discord constraints to design around:
- **25 option cap** on select menus (already handled)
- **5 rows max** per View (buttons + selects)
- **80 char** button label cap
- **Embeds can't have clickable text** — use buttons/selects
- **Files (images) can be swapped** via `edit_message(attachments=[new_file])`
- **Modals require a non-deferred interaction** — can't `defer()` then `send_modal()`

## The Goal

**DraftKings-level UX in Discord.** The user experience should feel like:

1. Tap a sport → see today's games with odds at a glance
2. Tap a game → see the match card (rendered PNG) with all betting lines as buttons
3. Tap a line → confirm amount and place bet (or add to parlay cart)
4. ← Back at every step, instant, no message spam
5. Cart always visible in footer, submit parlay from anywhere
6. After placing a bet, return to the game list (not dead-end)

All within ONE ephemeral message that morphs through states.

## Brainstorm Questions

Think about these before writing any code:

1. **Architecture:** Should the real sportsbook flow be absorbed INTO `SportsbookWorkspace` (extend it with new states), or should `real_sportsbook_cog.py` get its own parallel Workspace class? Pros/cons of each.

2. **State machine:** Map out every state the workspace needs. What data does each state need? How does the user get to each state and get back?

3. **Match card rendering:** The current flow renders a PNG match card via `build_real_match_detail_card()`. This is the premium visual. How do we keep it while editing in place? (Hint: `edit_message(attachments=[new_file])` works.)

4. **Bet placement UX:** Currently uses `WagerPresetView` (quick-pick buttons: $100, $250, $500, $1000, Custom) and `CustomRealWagerModal`. How does this fit into the single-message workspace? Should wager selection be inline buttons or should it open a modal?

5. **Parlay integration:** The cart footer already shows in `SportsbookWorkspace`. Real sport legs need to flow into the same cart. How?

6. **25-game cap:** Some sports have 15+ games in a day. The current select menu caps at 25. Is a select menu the right UX, or should we paginate with buttons (← Page 1 of 3 →)?

7. **Performance:** Each state transition calls `edit_message`. If we're swapping a PNG attachment on every game selection, is there a latency concern? Should we cache rendered cards?

8. **Dead-end prevention:** After bet placement, where does the user land? Back to game list? Back to match card (to bet another line)? Both options?

## Design Principles

- **One message, many states.** Never `followup.send` for navigation.
- **← Back everywhere.** Every drill-down has a back button.
- **Cart is ambient.** Footer always shows parlay cart status.
- **Fast.** If a state doesn't need a PNG, don't render one.
- **Clean.** Consistent button styles, colors from AtlasColors, sport emoji.
- **Addictive.** After placing a bet, make it trivially easy to place another.

## Deliverable

A detailed implementation plan with:
- State machine diagram (all states + transitions + back paths)
- File-by-file change list
- Which views/classes get created, modified, or deleted
- The specific data each state carries
- Button/select layout for each state (row assignments)
- Migration strategy (how to deprecate old views without breaking existing users)

Do NOT write code yet. Just the plan. We'll execute in a follow-up session.
