# "My Team" Quick-Select Default — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an inline green "My Team" button to all team selection views so users can skip the conference→team flow when looking up their own team — the most common use case.

**Architecture:** Each conference-selection view (`ConferenceSelectView`, `OracleConfView`, `OracleDualConfView`) gains an optional green "My Team (Bears)" button at the same row level as AFC/NFC. The button only appears when the requesting user has a team assignment (via `roster.get_entry_by_id()`). For trades, Team A auto-preselects to the user's team, skipping Step 1 entirely. For Oracle dual-team flows, Team A defaults to user's team and Team B shows the normal picker.

**Tech Stack:** Python 3.14, discord.py 2.3+, `roster` module (in-memory O(1) lookup)

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `roster.py` | Modify (lines ~105-117) | Add `get_team_dict(discord_id)` helper that returns the team's full dict from `dm.df_teams` |
| `genesis_cog.py` | Modify (lines 962-1078, 1733-1748, 2006-2021) | Add "My Team" button to `ConferenceSelectView`; auto-preselect Team A in `/trade` and genesis hub Trade button |
| `oracle_cog.py` | Modify (lines 2924-3014) | Add "My Team" button to `OracleConfView` and `OracleDualConfView`; auto-select Team A in dual flows |
| `player_picker.py` | Modify (lines 123-136, 280-291) | Default team filter dropdown to user's team |

---

## Key Design Decisions

1. **Button placement:** Inline with AFC/NFC at row 0, green (`ButtonStyle.success`), labeled "My Team (Bears)" with the team's nickname
2. **Trade flow:** When user has a team, `/trade` and the Genesis hub "New Trade" button skip Step 1 entirely — they auto-resolve Team A and go straight to Step 2 (pick Team B via conference→dropdown)
3. **Oracle dual-team:** Team A auto-resolves to user's team, view opens directly at Step B (pick Team B)
4. **Oracle single-team:** "My Team" button fires the callback immediately with the user's team name
5. **Player picker:** Team filter dropdown defaults to user's team instead of "All Teams"
6. **Graceful fallback:** If user has no team assignment, all views behave exactly as they do today (no "My Team" button shown)

---

## Task 1: Add `get_team_dict()` Helper to Roster Module

**Files:**
- Modify: `roster.py:105-117`

The genesis cog needs a full team dict (with `id`, `nickName`, `abbrName`, `divName`, `userName`, etc.) from `dm.df_teams` to pass into its existing `ConferenceTeamSelect.callback()` flow. The oracle cog just needs the `nickName` string. This helper bridges the gap for genesis.

- [ ] **Step 1: Add `get_team_dict()` function after `get_all_teams()`**

Add this function at line ~118 in `roster.py`, after the existing `get_all_teams()`:

```python
def get_team_dict(discord_id: int) -> dict | None:
    """Return the full dm.df_teams row dict for a user's assigned team.

    Returns None if the user has no team or team data isn't loaded.
    Used by genesis trade flow which needs the full team dict (id, nickName, etc.).
    """
    entry = _by_id.get(discord_id)
    if not entry or dm is None or dm.df_teams is None or dm.df_teams.empty:
        return None
    abbr = entry.team_abbr.upper()
    mask = dm.df_teams["abbrName"].str.upper() == abbr
    if not mask.any():
        return None
    return dm.df_teams[mask].iloc[0].to_dict()
```

- [ ] **Step 2: Verify no import issues**

Run: `python -c "import roster; print('OK')"`
Expected: `OK` (or normal startup — no import errors)

- [ ] **Step 3: Commit**

```bash
git add roster.py
git commit -m "feat(roster): add get_team_dict() helper for my-team quick-select"
```

---

## Task 2: Add "My Team" Button to Genesis `ConferenceSelectView`

**Files:**
- Modify: `genesis_cog.py:962-1002`

The `ConferenceSelectView` currently shows AFC/NFC buttons. We add a conditional green "My Team" button that fires the same team-selection logic as if the user had picked their team from the dropdown.

- [ ] **Step 1: Add `roster` module-level import (PREREQUISITE)**

At the top of `genesis_cog.py`, `roster` is currently only imported inline inside `_build_conference_team_options` (line 931). The new view code references `roster` at class construction time, so we need a module-level import. Add near the other imports:

```python
import roster
```

Then remove the inline `import roster` from `_build_conference_team_options` (line 931) since it's now module-level.

- [ ] **Step 2: Modify `ConferenceSelectView.__init__` to accept and store `user_id`**

Change the `__init__` signature and body to accept `user_id` (the interaction user's Discord ID):

```python
class ConferenceSelectView(discord.ui.View):
    """AFC / NFC buttons -- used for both Team A and Team B selection steps."""

    def __init__(
        self, bot: commands.Bot, proposer_id: int,
        step: str = "A",
        team_a: dict | None = None,
        user_id: int | None = None,
    ):
        super().__init__(timeout=180)
        self.bot_ref     = bot
        self.proposer_id = proposer_id
        self.step        = step
        self.team_a      = team_a
        self._user_id    = user_id

        # Add "My Team" button if user has an assigned team
        if user_id:
            entry = roster.get_entry_by_id(user_id)
            if entry:
                team_dict = roster.get_team_dict(user_id)
                if team_dict:
                    # Don't show "My Team" for step B if it would pick the same team as A
                    skip = (
                        team_a
                        and int(team_a.get("id", 0)) == int(team_dict.get("id", 0))
                    )
                    if not skip:
                        btn = discord.ui.Button(
                            label=f"My Team ({entry.team_name})",
                            style=discord.ButtonStyle.success,
                            emoji="⭐",
                            row=0,
                        )
                        btn.callback = self._my_team_callback(team_dict)
                        self.add_item(btn)

    def _my_team_callback(self, team_dict: dict):
        """Return a callback that selects the user's own team."""
        async def callback(interaction: discord.Interaction):
            if self.step == "A":
                # Move to step B with user's team as Team A
                embed = _conference_select_embed(
                    "B",
                    "Pick a conference to select **Team B** (the team receiving).",
                    team_a=team_dict,
                )
                view = ConferenceSelectView(
                    bot=self.bot_ref, proposer_id=self.proposer_id,
                    step="B", team_a=team_dict,
                    user_id=self._user_id,
                )
                await interaction.response.edit_message(embed=embed, view=view)
            else:
                # Step B: user picked their own team as the receiving team
                view = PickerTradeView(
                    team_a=self.team_a, team_b=team_dict,
                    proposer_id=self.proposer_id, bot=self.bot_ref,
                )
                await interaction.response.edit_message(embed=view.step_embed(), view=view)
        return callback
```

- [ ] **Step 3: Update all `ConferenceSelectView(...)` call sites to pass `user_id`**

There are 4 call sites in `genesis_cog.py` that construct `ConferenceSelectView`. Update each:

**Line ~1035 (inside `ConferenceTeamSelect.callback`, step A → step B transition):**
```python
view = ConferenceSelectView(
    bot=self.bot_ref, proposer_id=self.proposer_id,
    step="B", team_a=team,
    user_id=self.proposer_id,
)
```

**Line ~1074 (inside `ConferenceTeamSelectView.back_button`):**
```python
view = ConferenceSelectView(
    bot=self.bot_ref, proposer_id=self.proposer_id,
    step=self.step, team_a=self.team_a,
    user_id=self.proposer_id,
)
```

**Line ~1745 (inside `/trade` command):**
```python
view = ConferenceSelectView(
    bot=self.bot, proposer_id=interaction.user.id, step="A",
    user_id=interaction.user.id,
)
```

**Line ~2018 (inside genesis hub `btn_trade`):**
```python
view = ConferenceSelectView(
    bot=self.bot, proposer_id=interaction.user.id, step="A",
    user_id=interaction.user.id,
)
```

- [ ] **Step 4: Verify the view renders without errors**

Start the bot and run `/trade`. Verify:
- If you have a team assignment: green "My Team (Bears)" button appears alongside AFC/NFC
- Clicking "My Team" skips to Step 2 with your team as Team A
- AFC/NFC still work normally
- If you don't have a team: only AFC/NFC buttons appear (unchanged behavior)

- [ ] **Step 5: Commit**

```bash
git add genesis_cog.py
git commit -m "feat(genesis): add My Team quick-select to trade ConferenceSelectView"
```

---

## Task 3: Auto-Preselect Team A in Trade Flow

**Files:**
- Modify: `genesis_cog.py:1733-1748` (the `/trade` command)
- Modify: `genesis_cog.py:2006-2021` (the genesis hub "New Trade" button)

When a user has a team assignment, skip Step 1 entirely — auto-set their team as Team A and go straight to Step 2 (picking Team B).

- [ ] **Step 1: Modify the `/trade` command to auto-preselect**

Replace the `/trade` command body (lines ~1733-1748) with:

```python
async def trade(self, interaction: discord.Interaction):
    """Conference-button trade flow.  AFC/NFC → 16-team dropdown → picker."""
    if dm.df_teams.empty or not dm.get_players():
        return await interaction.response.send_message(
            "⚠️ Roster data not loaded yet. Run `/wittsync` first.", ephemeral=True,
        )

    # Auto-preselect user's team as Team A if they have one
    user_team = roster.get_team_dict(interaction.user.id)
    if user_team:
        embed = _conference_select_embed(
            "B",
            "Pick a conference to select **Team B** (the team receiving).",
            team_a=user_team,
        )
        embed.set_footer(text="TSL Trade Engine v2.7 • Picker mode • All valuations are advisory")
        view = ConferenceSelectView(
            bot=self.bot, proposer_id=interaction.user.id,
            step="B", team_a=user_team,
            user_id=interaction.user.id,
        )
    else:
        embed = discord.Embed(
            title="💱 Trade Center — Step 1",
            description="Pick a conference to select **Team A** (the team sending).",
            color=AtlasColors.INFO,
        )
        embed.set_footer(text="TSL Trade Engine v2.7 • Picker mode • All valuations are advisory")
        view = ConferenceSelectView(
            bot=self.bot, proposer_id=interaction.user.id, step="A",
            user_id=interaction.user.id,
        )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
```

- [ ] **Step 2: Apply the same pattern to the genesis hub "New Trade" button**

Replace the `btn_trade` method body (lines ~2006-2021) with the same pattern:

```python
async def btn_trade(self, interaction: discord.Interaction, _b: discord.ui.Button):
    """Open the Trade Center directly — launches conference select flow."""
    if dm.df_teams.empty or not dm.get_players():
        return await interaction.response.send_message(
            "Roster data not loaded yet. Run `/wittsync` first.", ephemeral=True,
        )

    # Auto-preselect user's team as Team A if they have one
    user_team = roster.get_team_dict(interaction.user.id)
    if user_team:
        embed = _conference_select_embed(
            "B",
            "Pick a conference to select **Team B** (the team receiving).",
            team_a=user_team,
        )
        embed.set_footer(text="TSL Trade Engine v2.7 · Picker mode · All valuations are advisory")
        view = ConferenceSelectView(
            bot=self.bot, proposer_id=interaction.user.id,
            step="B", team_a=user_team,
            user_id=interaction.user.id,
        )
    else:
        embed = discord.Embed(
            title="💱 Trade Center — Step 1",
            description="Pick a conference to select **Team A** (the team sending).",
            color=AtlasColors.INFO,
        )
        embed.set_footer(text="TSL Trade Engine v2.7 · Picker mode · All valuations are advisory")
        view = ConferenceSelectView(
            bot=self.bot, proposer_id=interaction.user.id, step="A",
            user_id=interaction.user.id,
        )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
```

- [ ] **Step 3: Verify trade auto-preselect**

Start bot, run `/trade`:
- With team assignment: should skip Step 1 and show "Step 2 — pick Team B" immediately, with "Team A: Bears" shown in the embed
- Without team assignment: should show Step 1 as before (AFC/NFC + no My Team button)

- [ ] **Step 4: Commit**

```bash
git add genesis_cog.py
git commit -m "feat(genesis): auto-preselect user's team as Team A in trade flow"
```

---

## Task 4: Add "My Team" Button to Oracle `OracleConfView` (Single-Team)

**Files:**
- Modify: `oracle_cog.py:2924-2953`

The `OracleConfView` is used by Team Report, Owner Profile, Player Scout, Dynasty Profile, Betting Profile, and Game Plan. All of these benefit from a "My Team" button.

- [ ] **Step 1: Add `roster` module-level import (PREREQUISITE)**

`oracle_cog.py` currently imports `roster` inline (lines 806, 1256, 1622). The new view code references `roster` at class construction time, so add a module-level import near the other imports:

```python
import roster
```

Then remove the 3 inline `import roster` statements (lines 806, 1256, 1622) since they're now module-level.

- [ ] **Step 2: Modify `OracleConfView.__init__` to accept `user_id` and add "My Team" button**

```python
class OracleConfView(discord.ui.View):
    """AFC / NFC buttons for single-team Oracle analysis types."""

    def __init__(self, analysis_label: str, callback_fn, hub_view_fn=None,
                 user_id: int | None = None):
        super().__init__(timeout=300)
        self._analysis_label = analysis_label
        self._callback_fn = callback_fn
        self._hub_view_fn = hub_view_fn
        self._user_id = user_id

        # Add "My Team" button if user has an assigned team
        if user_id:
            entry = roster.get_entry_by_id(user_id)
            if entry:
                btn = discord.ui.Button(
                    label=f"My Team ({entry.team_name})",
                    style=discord.ButtonStyle.success,
                    emoji="⭐",
                    row=0,
                )
                btn.callback = self._my_team_cb(entry.team_name)
                self.add_item(btn)

    def _my_team_cb(self, team_name: str):
        async def callback(interaction: discord.Interaction):
            await self._callback_fn(interaction, team_name)
        return callback
```

- [ ] **Step 3: Update `_make_back` to preserve `user_id`**

```python
def _make_back(self):
    embed = _oracle_conf_embed(self._analysis_label, "Pick a conference.")
    return OracleConfView(self._analysis_label, self._callback_fn,
                          self._hub_view_fn, user_id=self._user_id), embed
```

- [ ] **Step 4: Update all `OracleConfView(...)` call sites in `OracleIntelView`**

There are 6 call sites in `OracleIntelView` that create `OracleConfView`. Each needs `user_id=interaction.user.id`:

Search for every `OracleConfView(` in `oracle_cog.py` and add the `user_id` kwarg. The call sites are in:
- `btn_matchup` → actually uses `OracleDualConfView` (skip, covered in Task 5)
- `btn_rivalry` → `OracleDualConfView` (skip)
- `btn_gameplan` (line ~3128): `view = OracleConfView("Game Plan", self._do_game_plan, user_id=interaction.user.id)`
- `btn_team_report` (line ~3140): `view = OracleConfView("Team Report", self._do_team_report, user_id=interaction.user.id)`
- `btn_owner` (line ~3150): `view = OracleConfView("Owner Profile", self._do_owner_profile, user_id=interaction.user.id)`
- `btn_dynasty` (line ~3181): `view = OracleConfView("Dynasty Profile", self._do_dynasty, user_id=interaction.user.id)`
- `btn_betting` (line ~3193): `view = OracleConfView("Betting Profile", self._do_betting_profile, user_id=interaction.user.id)`

For each, add `user_id=interaction.user.id` as a kwarg.

- [ ] **Step 5: Verify Oracle single-team My Team button**

Start bot, open `/oracle`, click "Team Report":
- With team assignment: green "My Team (Bears)" button appears next to AFC/NFC
- Clicking it immediately fires the team report for your team
- AFC/NFC still work normally

- [ ] **Step 6: Commit**

```bash
git add oracle_cog.py
git commit -m "feat(oracle): add My Team quick-select to single-team analysis views"
```

---

## Task 5: Add "My Team" Default to Oracle `OracleDualConfView` (Dual-Team)

**Files:**
- Modify: `oracle_cog.py:2958-3014`

For Matchup Analysis, Rivalry History, and Game Plan (dual-team), Team A should auto-default to the user's team, jumping straight to Step B.

- [ ] **Step 1: Modify `OracleDualConfView.__init__` to accept `user_id`**

```python
class OracleDualConfView(discord.ui.View):
    """AFC/NFC buttons for dual-team analysis (Matchup, Rivalry, Game Plan)."""

    def __init__(self, analysis_label: str, dual_callback_fn, step: str = "A",
                 team_a_name: str | None = None, user_id: int | None = None):
        super().__init__(timeout=300)
        self._analysis_label = analysis_label
        self._dual_callback_fn = dual_callback_fn
        self._step = step
        self._team_a_name = team_a_name
        self._user_id = user_id

        # For step B, add "My Team" button (if it's not the same as Team A)
        if user_id and step == "B":
            entry = roster.get_entry_by_id(user_id)
            if entry and entry.team_name != team_a_name:
                btn = discord.ui.Button(
                    label=f"My Team ({entry.team_name})",
                    style=discord.ButtonStyle.success,
                    emoji="⭐",
                    row=0,
                )
                btn.callback = self._my_team_cb(entry.team_name)
                self.add_item(btn)

    def _my_team_cb(self, team_name: str):
        async def callback(interaction: discord.Interaction):
            await self._dual_callback_fn(interaction, self._team_a_name, team_name)
        return callback
```

- [ ] **Step 2: Update `_make_back` to preserve `user_id`**

```python
def _make_back(self):
    embed = self._step_embed()
    return OracleDualConfView(self._analysis_label, self._dual_callback_fn,
                              self._step, self._team_a_name,
                              user_id=self._user_id), embed
```

- [ ] **Step 3: Update `on_team_selected` inside `_show_teams` to pass `user_id`**

In the `_show_teams` method (line ~2999), the `on_team_selected` closure creates a new `OracleDualConfView` for step B. Update it:

```python
async def on_team_selected(inter: discord.Interaction, team_name: str):
    if self._step == "A":
        embed_b = _oracle_conf_embed(self._analysis_label,
                                     "Pick a conference to select **Team B**.",
                                     team_a=team_name)
        view_b = OracleDualConfView(self._analysis_label, self._dual_callback_fn,
                                    step="B", team_a_name=team_name,
                                    user_id=self._user_id)
        await inter.response.edit_message(embed=embed_b, view=view_b)
    else:
        await self._dual_callback_fn(inter, self._team_a_name, team_name)
```

- [ ] **Step 4: Update the 2 `OracleDualConfView(...)` call sites to auto-preselect Team A**

The call sites are in `OracleIntelView`:

**`btn_matchup` (line ~3103-3109) — replace only the method body, keep `@_safe_interaction` decorator:**

The existing code is:
```python
@discord.ui.button(label="Matchup Analysis", emoji="🏈", style=discord.ButtonStyle.primary, row=0)
@_safe_interaction
async def btn_matchup(self, interaction: discord.Interaction, _b: discord.ui.Button):
    if not _ORACLE_INTEL_OK:
        await interaction.response.send_message("Oracle analysis module offline.", ephemeral=True)
        return
    embed = _oracle_conf_embed("Matchup Analysis", "Pick a conference to select **Team A**.")
    view = OracleDualConfView("Matchup Analysis", self._do_matchup)
    await interaction.response.edit_message(embed=embed, view=view)
```

Replace the method body (keep decorators and signature unchanged) with:
```python
    if not _ORACLE_INTEL_OK:
        await interaction.response.send_message("Oracle analysis module offline.", ephemeral=True)
        return
    user_team_name = roster.get_team_name(interaction.user.id)
    if user_team_name:
        # Auto-preselect user's team as Team A, jump to step B
        embed = _oracle_conf_embed("Matchup Analysis",
                                   "Pick a conference to select **Team B**.",
                                   team_a=user_team_name)
        view = OracleDualConfView("Matchup Analysis", self._do_matchup,
                                  step="B", team_a_name=user_team_name,
                                  user_id=interaction.user.id)
    else:
        embed = _oracle_conf_embed("Matchup Analysis", "Pick a conference to select **Team A**.")
        view = OracleDualConfView("Matchup Analysis", self._do_matchup,
                                  user_id=interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)
```

**`btn_rivalry` (line ~3112-3119) — replace only the method body, keep `@_safe_interaction` decorator:**

The existing code is:
```python
@discord.ui.button(label="Rivalry History", emoji="⚔️", style=discord.ButtonStyle.primary, row=0)
@_safe_interaction
async def btn_rivalry(self, interaction: discord.Interaction, _b: discord.ui.Button):
    if not _ORACLE_INTEL_OK:
        await interaction.response.send_message("Oracle analysis module offline.", ephemeral=True)
        return
    embed = _oracle_conf_embed("Rivalry History", "Pick a conference to select **Owner A's team**.")
    view = OracleDualConfView("Rivalry History", self._do_rivalry)
    await interaction.response.edit_message(embed=embed, view=view)
```

Replace the method body (keep decorators and signature unchanged) with:
```python
    if not _ORACLE_INTEL_OK:
        await interaction.response.send_message("Oracle analysis module offline.", ephemeral=True)
        return
    user_team_name = roster.get_team_name(interaction.user.id)
    if user_team_name:
        embed = _oracle_conf_embed("Rivalry History",
                                   "Pick a conference to select **Owner B's team**.",
                                   team_a=user_team_name)
        view = OracleDualConfView("Rivalry History", self._do_rivalry,
                                  step="B", team_a_name=user_team_name,
                                  user_id=interaction.user.id)
    else:
        embed = _oracle_conf_embed("Rivalry History", "Pick a conference to select **Owner A's team**.")
        view = OracleDualConfView("Rivalry History", self._do_rivalry,
                                  user_id=interaction.user.id)
    await interaction.response.edit_message(embed=embed, view=view)
```

Note: Game Plan uses `OracleConfView` (single-team — "pick the team you want to beat"), NOT `OracleDualConfView`, so it's already handled in Task 4.

- [ ] **Step 5: Verify Oracle dual-team auto-preselect**

Start bot, open `/oracle`, click "Matchup Analysis":
- With team assignment: should skip Step A, show "Team A: Bears" in embed, and prompt to pick Team B with "My Team" button hidden (since Team A is already your team — you'd be matching up against yourself)
- Without team assignment: shows Step A as before

- [ ] **Step 6: Commit**

```bash
git add oracle_cog.py
git commit -m "feat(oracle): auto-preselect user's team in dual-team analysis views"
```

---

## Task 6: Default Player Picker Team Filter to User's Team

**Files:**
- Modify: `player_picker.py:123-136, 280-291`

When the player picker opens, the team filter dropdown should default to the user's team instead of "All Teams".

- [ ] **Step 1: Add `roster` import to `player_picker.py`**

At the top of the file, add:
```python
try:
    import roster
except ImportError:
    roster = None  # type: ignore[assignment]
```

- [ ] **Step 2: Add `user_id` parameter to `PlayerPickerView.__init__`**

The existing `__init__` signature (line 226-234) is:
```python
def __init__(
    self,
    callback,
    multi:       bool = False,
    max_picks:   int  = 10,
    team_filter: str  = "ALL",
    pos_filter:  str  = "ALL",
    label:       str  = "Player Picker",
):
```

Add `user_id: int | None = None` as a new parameter, and add the auto-default logic after `self._team = team_filter` (line 240) but before `self._players = _filter_players(...)` (line 245):

```python
def __init__(
    self,
    callback,
    multi:       bool = False,
    max_picks:   int  = 10,
    team_filter: str  = "ALL",
    pos_filter:  str  = "ALL",
    label:       str  = "Player Picker",
    user_id:     int | None = None,
):
    super().__init__(timeout=300)
    self._callback    = callback
    self._multi       = multi
    self._max_picks   = max_picks
    self._pos_group   = pos_filter
    self._team        = team_filter
    self._cart:  list[dict] = []
    self._label       = label

    # Auto-default team filter to user's team if not explicitly set
    if user_id and roster and self._team == "ALL":
        user_team = roster.get_team_name(user_id)
        if user_team:
            self._team = user_team

    # Build initial player list
    self._players = _filter_players(self._pos_group, self._team)
    self._rebuild_items()
```

- [ ] **Step 3: Update convenience functions `make_single_picker` and `make_multi_picker`**

These are at lines 400-407 of `player_picker.py`. Add `user_id` pass-through:

```python
def make_single_picker(callback, team_filter: str = "ALL", label: str = "Select Player",
                       user_id: int | None = None) -> PlayerPickerView:
    """Convenience: single-player picker, optionally pre-filtered to a team."""
    return PlayerPickerView(callback=callback, multi=False, team_filter=team_filter,
                            label=label, user_id=user_id)


def make_multi_picker(callback, team_filter: str = "ALL", max_picks: int = 5,
                      label: str = "Select Players", user_id: int | None = None) -> PlayerPickerView:
    """Convenience: multi-player picker (cart mode), optionally pre-filtered to a team."""
    return PlayerPickerView(callback=callback, multi=True, max_picks=max_picks,
                            team_filter=team_filter, label=label, user_id=user_id)
```

- [ ] **Step 4: Update call sites that create `PlayerPickerView` or use convenience functions**

Search for all `PlayerPickerView(`, `make_single_picker(`, and `make_multi_picker(` in the codebase and add `user_id=interaction.user.id` where `interaction` is available. The key call sites will be in `genesis_cog.py` (trade picker uses `make_multi_picker`).

- [ ] **Step 5: Verify player picker defaults**

Open any flow that uses the player picker. The team dropdown should show the user's team pre-selected instead of "All Teams".

- [ ] **Step 6: Commit**

```bash
git add player_picker.py genesis_cog.py
git commit -m "feat(player-picker): default team filter to user's team"
```

---

## Task 7: Version Bump and Final Verification

**Files:**
- Modify: `bot.py` (ATLAS_VERSION)

- [ ] **Step 1: Bump ATLAS_VERSION**

Find `ATLAS_VERSION` in `bot.py` and bump the minor version (e.g., `2.X.0` → `2.X+1.0` for this feature).

- [ ] **Step 2: Full integration test**

Test all modified flows:

| Flow | Expected behavior (with team) | Expected behavior (no team) |
|------|-------------------------------|----------------------------|
| `/trade` | Skip to Step 2, Team A = your team | Step 1 with AFC/NFC only |
| Genesis hub → New Trade | Same as above | Same as above |
| Oracle → Team Report | Green "My Team" button + AFC/NFC | AFC/NFC only |
| Oracle → Owner Profile | Green "My Team" button + AFC/NFC | AFC/NFC only |
| Oracle → Matchup Analysis | Skip to Step B, Team A = your team | Step A with AFC/NFC only |
| Oracle → Rivalry History | Skip to Step B, Team A = your team | Step A with AFC/NFC only |
| Player Picker | Team filter defaults to your team | "All Teams" default |

- [ ] **Step 3: Commit version bump**

```bash
git add bot.py
git commit -m "chore: bump ATLAS_VERSION for my-team quick-select feature"
```

---

## Scope Exclusions

These views are intentionally **NOT** modified:

| View | Reason |
|------|--------|
| `AssignConferenceView` (roster.py) | Commissioner assigning *someone else* — defaulting to commish's team would be wrong |
| `OrphanTeamSelectView` (boss_cog.py) | Admin marking a franchise orphan — admin context, not self-lookup |
| `OwnerSelectView` (roster.py) | Not currently used by any cog — dead code |
| `_TeamAssignSelect` (roster.py) | Commissioner flow — same as AssignConferenceView |
