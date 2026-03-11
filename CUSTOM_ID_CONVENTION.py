"""
ATLAS -- Custom ID Convention
Naming standard for all Discord UI component IDs.
Version: 1.0.0

EVERY button, select menu, and modal in ATLAS must use this format.
No exceptions. This prevents ID collisions, makes debugging trivial,
and allows the UIStateManager to route interactions by module.

FORMAT:     atlas:[module]:[action]

EXAMPLES:
    atlas:sportsbook:tsl_games
    atlas:sportsbook:real_sports
    atlas:sportsbook:markets
    atlas:sportsbook:my_bets
    atlas:sportsbook:cashout

    atlas:casino:slots
    atlas:casino:blackjack
    atlas:casino:scratch
    atlas:casino:dice
    atlas:casino:my_stats

    atlas:stats:team_card
    atlas:stats:owner_card
    atlas:stats:hot_cold
    atlas:stats:clutch
    atlas:stats:draft
    atlas:stats:power
    atlas:stats:recap
    atlas:stats:player

    atlas:eco:transactions
    atlas:eco:leaderboard
    atlas:eco:portfolio
    atlas:eco:claim_stipend

    atlas:trade:new_proposal
    atlas:trade:my_trades
    atlas:trade:history
    atlas:trade:cap_calc

    atlas:commish:sb_mgmt
    atlas:commish:casino_mgmt
    atlas:commish:eco_mgmt
    atlas:commish:markets_mgmt
    atlas:commish:trade_desk
    atlas:commish:system_tools

RULES:

1. Prefix is ALWAYS "atlas"
2. Module names are lowercase, single word when possible
3. Actions use snake_case
4. Max 100 chars total (Discord limit)
5. No spaces, no special characters beyond colon and underscore
6. Modals use same convention:  atlas:[module]:modal_[action]
   Example: atlas:trade:modal_new_proposal
7. Select menus:  atlas:[module]:select_[action]
   Example: atlas:trade:select_partner

MODULES REGISTRY:

    sportsbook  -- TSL Games, Real Sports, Polymarkets
    casino      -- Slots, Blackjack, Scratch, Dice
    stats       -- Team/Owner/Player cards, reports
    eco         -- Wallet, transactions, leaderboard
    oracle      -- AI analysis (5 modes)
    trade       -- Proposals, history, cap calculator
    sentinel    -- Rules, force requests, 4th down
    codex       -- History, /ask AI
    commish     -- Commissioner console
    genesis     -- Draft/prospects (future)
    echo        -- Commissioner voice (future)

NOTE: Pagination views (PaginationView, LazyPaginationView) do NOT use
static custom_ids. They are ephemeral (3-min timeout) and discord.py
auto-generates unique IDs per instance to prevent collisions.
"""
