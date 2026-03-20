# Unified Modal Base — Design Spec

> **Priority:** #2 of 7 Oracle V4 improvements (was C1 in V4 handoff)
> **Date:** 2026-03-19
> **Scope:** oracle_cog.py — 4 AI-powered modals
> **Approach:** Template Method pattern — base class handles lifecycle, subclasses implement `_generate()`

---

## Problem

The 4 AI-powered Oracle modals (AskTSLModal, _AskWebModal, PlayerScoutModal, StrategyRoomModal) each duplicate ~15 lines of identical boilerplate:
- `defer(thinking=True, ephemeral=True)`
- `_HISTORY_OK` guard check (2 of 4)
- `try/except Exception` wrapper with identical error message
- `discord.Embed` construction with `_truncate_for_embed()`, timestamp, footer, `ATLAS_ICON_URL`
- `followup.send(embed=embed, ephemeral=True)`

This duplication means every new modal or modification requires touching the same patterns in multiple places. The unique logic (intent detection, web search, scout SQL, strategy context) is completely different per modal — a clean Template Method extraction.

---

## Design

### Base Class: `_OracleIntelModal`

Placed in oracle_cog.py above the 4 modal class definitions (~line 3079).

```python
class _OracleIntelModal(discord.ui.Modal):
    """Base class for AI-powered Oracle intelligence modals.

    Subclasses implement _generate() which returns the answer text
    and embed configuration. The base class handles the lifecycle:
    defer → guard → try/except → embed → send.
    """

    _requires_history: bool = False

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        if self._requires_history and not _HISTORY_OK:
            await interaction.followup.send(
                "⚠️ Historical database not available.", ephemeral=True
            )
            return

        try:
            answer, embed_kwargs = await self._generate(interaction)
            embed = self._build_embed(answer, **embed_kwargs)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(
                f"❌ Something broke: `{e}`", ephemeral=True
            )

    async def _generate(
        self, interaction: discord.Interaction
    ) -> tuple[str, dict]:
        """Subclasses implement this.

        Returns:
            (answer_text, embed_kwargs) where embed_kwargs contains:
            - title: str — embed title (e.g., "🔬 ATLAS Intelligence — TSL League")
            - color: discord.Color or int — embed color
            - footer: str — pre-formatted footer text
        """
        raise NotImplementedError

    @staticmethod
    def _build_embed(
        answer: str,
        *,
        title: str,
        color,
        footer: str,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=_truncate_for_embed(answer),
            color=color,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text=footer, icon_url=ATLAS_ICON_URL)
        return embed
```

### Contract

Every subclass implements `_generate(interaction)` → `(answer_text, embed_kwargs_dict)`.

The `embed_kwargs` dict must contain:
- `title` (str) — modal-specific embed title
- `color` — embed color (discord.Color or int)
- `footer` (str) — pre-formatted footer text (each modal builds its own format to preserve existing separators)

Early returns (e.g., "couldn't generate query") are handled by raising a custom exception or calling `interaction.followup.send()` directly and returning a sentinel. Since `_generate()` is inside the base class's try/except, a simple approach is:

```python
# In _generate(), for early exits:
await interaction.followup.send("🎯 Couldn't generate...", ephemeral=True)
raise _EarlyReturn()  # Caught by base class, no error message sent
```

With a simple sentinel exception:
```python
class _EarlyReturn(Exception):
    """Signal that _generate() already sent a response."""
    pass
```

And in the base `on_submit`:
```python
try:
    answer, embed_kwargs = await self._generate(interaction)
    embed = self._build_embed(answer, **embed_kwargs)
    await interaction.followup.send(embed=embed, ephemeral=True)
except _EarlyReturn:
    pass  # _generate() already sent its own response
except Exception as e:
    await interaction.followup.send(f"❌ Something broke: `{e}`", ephemeral=True)
```

**Note:** `AskOpenModal()` and `SportsIntelModal()` factory functions (line ~3274) are unaffected — they return `_AskWebModal(mode=...)` which still works identically.

---

## Subclass Conversions

### AskTSLModal (lines ~3081-3211)

```python
class AskTSLModal(_OracleIntelModal, title="📊 Ask ATLAS — TSL League"):
    _requires_history = True

    question = discord.ui.TextInput(...)

    async def _generate(self, interaction):
        q = self.question.value.strip()
        # ... existing name resolution, caller identity, conversation memory,
        #     3-tier intent detection, SQL generation, self-correction,
        #     answer generation logic (lines 3102-3189) ...

        # Preserve existing " | " separator for AskTSL footer
        footer_parts = [f"🔍 {len(rows)} records analyzed", tier_label]
        if alias_map:
            footer_parts.append(f"🔎 Resolved: {', '.join(f'{k}→{v}' for k, v in alias_map.items())}")
        if conv_block:
            footer_parts.append("💬 Conversational")
        footer = " | ".join(footer_parts) + " · ATLAS™ Oracle"

        return answer, {
            "title": "🔬 ATLAS Intelligence — TSL League",
            "color": C_DARK,
            "footer": footer,
        }
```

**What's removed:** defer (line 3094), `_HISTORY_OK` guard (3096-3098), try/except wrapper (3102/3210-3211), embed construction (3191-3207), followup.send (3208).

### _AskWebModal (lines ~3214-3272)

```python
class _AskWebModal(_OracleIntelModal):
    # _requires_history = False (default)

    question = discord.ui.TextInput(
        label="Your Question",
        placeholder="Ask anything...",
        required=True,
        max_length=300,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, *, mode: str = "open"):
        self._mode = mode
        if mode == "sports":
            super().__init__(title="🏈 Ask ATLAS — Sports Intel")
            self.question.label = "Your Sports Question"
            self.question.placeholder = "e.g. Who leads the NFL in passing yards? Latest trade rumors?"
        else:
            super().__init__(title="🌐 Ask ATLAS — Open Intel")
            self.question.label = "Your Question"
            self.question.placeholder = "e.g. Who is better, Luka or Jokic? Latest NFL news?"

    async def _generate(self, interaction):
        q = self.question.value.strip()
        persona = get_persona("analytical")
        result = await atlas_ai.generate_with_search(q, system=persona)

        # Mode-based config — matches existing colors and fallback messages exactly
        if self._mode == "sports":
            title, color = "🏈 ATLAS Intelligence — Sports Intel", ATLAS_GOLD
            footer_mode = "Sports Intel mode"
            fallback_msg = "ATLAS couldn't pull intel on that one."
        else:
            title, color = "🌐 ATLAS Intelligence — Open Intel", C_BLUE
            footer_mode = "Open Intel mode"
            fallback_msg = "ATLAS couldn't pull a response on that one."

        answer = result.text or fallback_msg

        footer = f"{footer_mode} · Web search enabled · ATLAS™ Oracle"
        if result.fallback_used:
            footer += "  ·  ⚡ via Gemini fallback"

        return answer, {"title": title, "color": color, "footer": footer}
```

**What's removed:** defer (line 3237), try/except wrapper (3241/3270-3271), embed construction (3258-3267), followup.send (3268). Label/placeholder customization preserved in `__init__`.

### PlayerScoutModal (lines ~3282-3459)

```python
class PlayerScoutModal(_OracleIntelModal, title="🎯 Ask ATLAS — Player Scout"):
    _requires_history = True

    question = discord.ui.TextInput(...)

    async def _generate(self, interaction):
        q = self.question.value.strip()
        # ... existing team resolution, conversation memory, scout schema,
        #     Sonnet SQL generation, self-correction, answer generation
        #     (lines 3303-3430) ...

        footer_parts = [f"🔍 {len(rows)} players analyzed"]
        if team_name:
            footer_parts.append(f"🏈 {team_name}")
        if self_corrected:
            footer_parts.append("⚠️ Self-corrected")
        footer_parts.append("ATLAS™ Oracle · Scout Mode")

        return answer, {
            "title": "🎯 ATLAS Intelligence — Player Scout",
            "color": AtlasColors.TSL_BLUE,
            "footer": " · ".join(footer_parts),
        }
```

**What's removed:** defer (line 3294), `_HISTORY_OK` guard (3296-3298), try/except wrapper (3302/3457-3458), embed construction (3439-3454), followup.send (3455).

### StrategyRoomModal (lines ~3461-3526)

```python
class StrategyRoomModal(_OracleIntelModal, title="🧠 Ask ATLAS — Strategy Room"):
    # _requires_history = False (default)

    question = discord.ui.TextInput(...)

    async def _generate(self, interaction):
        q = self.question.value.strip()
        # ... existing standings/team context building,
        #     generate_with_search() call (lines 3478-3510) ...

        footer = "Strategy Room · TSL context + web search · ATLAS™ Oracle"
        if result.fallback_used:
            footer += "  ·  ⚡ via Gemini fallback"

        return answer, {
            "title": "🧠 ATLAS Intelligence — Strategy Room",
            "color": AtlasColors.SUCCESS,
            "footer": footer,
        }
```

**What's removed:** defer (line 3473), try/except wrapper (3477/3524-3525), embed construction (3512-3521), followup.send (3522).

---

## Early Return Handling

Each modal currently has early return points (e.g., "Couldn't generate a query"). These are handled via `_EarlyReturn`:

```python
# In PlayerScoutModal._generate():
if not sql:
    await interaction.followup.send(
        "🎯 Couldn't generate a scouting query...", ephemeral=True
    )
    raise _EarlyReturn()
```

The base class catches `_EarlyReturn` silently — no "Something broke" message sent.

---

## Files Modified

| File | Changes |
|------|---------|
| `oracle_cog.py` | Add `_OracleIntelModal` + `_EarlyReturn`, convert 4 modal classes |
| `bot.py` | Bump `ATLAS_VERSION` (3.6.1 → 3.7.0 — minor, architectural change) |

No new files. No changes to the modal logic — only the lifecycle wrapper moves to the base class.

---

## Non-AI Modals (Explicitly Out of Scope)

H2HModal, TeamSearchModal, TeamMatchupModal, SeasonRecapModal are SQL-driven or non-AI. They don't share the same lifecycle pattern and are left unchanged.

---

## Testing

### Verification

1. All 4 modals still work via the Oracle Hub (functional parity)
2. Error handling still catches exceptions and shows "Something broke" message
3. Early returns still send the correct user-facing message (not the generic error)
4. `_HISTORY_OK = False` guard still blocks AskTSL and PlayerScout
5. Embed titles, colors, footers, and separators unchanged from before refactor (AskTSL uses `" | "`, others use `" · "`)

### How to Test

- Use Oracle Hub → click each of the 5 buttons → submit a question
- Verify embed title, color, footer match pre-refactor behavior
- Force an error (bad SQL) → verify self-correction or error message appears

---

## Priority Order (Full V4 Roadmap)

| # | Item | Status |
|---|------|--------|
| 1 | PlayerScout Upgrade | Done (v3.6.1) |
| **2** | **Unified Modal Base** | **This spec** |
| 3 | Multi-Retry SQL (was C3) | Pending |
| 4 | StrategyRoom Enrichment (was C6) | Pending |
| 5 | Cross-Modal Memory (was C7) | Partially addressed by #1 |
| 6 | Query Caching (was C2) | Pending |
| 7 | Result Citation (was C4) | Pending |
