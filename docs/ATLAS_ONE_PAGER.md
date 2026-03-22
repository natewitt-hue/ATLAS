# We built a Discord bot for our Madden sim league. It got out of hand.

---

**ATLAS** — Autonomous TSL League Administration System
v2.16.0 | 39,645 lines of Python | Built by one person

---

## What started as a stats tracker now does this:

It manages **31 teams** across **95+ Super Bowl seasons** of continuous league history.

It syncs live from the MaddenStats API — rosters, games, trades, abilities, standings — and maintains a historical database with 2,000+ players and 1,000+ games.

It evaluates trades with AI. Grades draft classes A+ through D. Writes weekly recaps with real stats and narrative.

It runs a **full sportsbook** — Elo ratings calculated from every historical game, dynamic spreads, moneylines, and over/unders. Parlays up to 6 legs. Real sports odds from 9 leagues synced every 15 minutes.

It has a **five-game casino** — six-deck blackjack, 3-reel slots with progressive jackpots, multiplayer crash with live 2-second updates, PvP coinflip challenges, and daily scratch cards. Every result rendered as a custom image card.

It pulls live **prediction markets from Polymarket**, scores them with a 6-factor curation algorithm (velocity, tension, freshness, urgency, liquidity, diversity), and drops a daily spotlight with AI-written editorial analysis.

It **renders every interaction as a custom image** — HTML pages converted to PNG via a Playwright browser pool. Dark luxury aesthetic. Gold accents. Procedural noise textures. Base64-embedded fonts. 480px at 2x DPI for crisp mobile display.

It **enforces league rules autonomously** — blowout detection every 15 minutes, 20+ banned position change transitions with attribute validation, a formal complaint system with five penalty tiers, force request tracking with screenshot evidence analysis.

It has **three AI personas** — casual for banter, official for rulings (cites exact rule sections), analytical for stats — and it auto-selects based on which channel you're in.

It **tracks how it feels about you.** An affinity system scores every interaction. Friends get warmth and inside jokes. Cross it enough and it gets dismissive. It holds grudges. It speaks in third person as "ATLAS" and it never says "I."

---

## By the numbers

| | |
|---|---|
| **39,645** | lines of Python |
| **95+** | Super Bowl seasons of history |
| **31** | active teams |
| **12** | specialized modules |
| **100+** | slash commands |
| **5** | casino games |
| **9** | real sports leagues |
| **15+** | prediction market categories |
| **3** | AI persona modes |
| **4** | affinity tiers (FRIEND → HOSTILE) |
| **1** | person who built it |

---

## Built with

Python 3.14 | discord.py 2.3+ | Google Gemini 2.0 Flash | Playwright | SQLite | Pandas

---

## The stack, if you're curious

- **Data:** MaddenStats API → Pandas DataFrames → SQLite (3 databases, atomic rebuilds)
- **AI:** Gemini 2.0 Flash — NL→SQL→NL queries, trade analysis, draft grading, 4th down rulings, weekly recaps, persona voice
- **Rendering:** HTML/CSS → Playwright PNG pipeline with 4-page browser pool, unified design tokens, 2x DPI
- **Economy:** Elo engine (adaptive K-factors, margin-of-victory, seasonal regression) → spreads/ML/O/U → sportsbook + casino + prediction markets
- **Identity:** 88+ alias mappings resolving Discord names to in-game usernames with fuzzy matching
- **Compliance:** Blowout monitor, position rules engine, complaint tracker, force request system, disconnect protocol

---

*ATLAS is the admin infrastructure for The Simulation League. If your league is still using spreadsheets, we understand. We were too, once.*
