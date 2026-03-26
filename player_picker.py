"""
player_picker.py — Reusable Player Filter & Select UI for TSL Bot
─────────────────────────────────────────────────────────────────────────────
A single-import, drop-in player selection system usable from any cog.

Usage (any module):

    from player_picker import PlayerPickerView

    # Single-player pick (hot/cold, roster lookup, etc.)
    async def my_command(self, interaction):
        async def on_pick(inter, player):
            # player is the full dict from dm.get_players()
            await inter.response.send_message(f"You picked {player['firstName']} {player['lastName']}")

        view = PlayerPickerView(callback=on_pick)
        await interaction.response.send_message(
            embed=PlayerPickerView.filter_embed(), view=view, ephemeral=True
        )

    # Multi-player pick (trade center, package builder, etc.)
    async def my_trade_command(self, interaction):
        async def on_pick(inter, players):
            # players is a list of player dicts
            names = [f"{p['firstName']} {p['lastName']}" for p in players]
            await inter.response.send_message(f"Package: {', '.join(names)}")

        view = PlayerPickerView(callback=on_pick, multi=True, max_picks=5)
        await interaction.response.send_message(
            embed=PlayerPickerView.filter_embed(), view=view, ephemeral=True
        )

    # Pre-filter to a specific team (trade center: show only Team A players)
    view = PlayerPickerView(callback=on_pick, team_filter="Ravens", multi=True)

Architecture:
    1. FilterView:      Two dropdowns — Position and Team. User picks filters.
    2. PlayerSelectView: Shows filtered players (up to 25, sorted OVR desc).
                         In multi mode, shows a cart + Submit button.
    3. callback fires with the selected player dict (single) or list (multi).

All player data comes from dm.get_players() (full /export/players CSV).
Positions and teams are auto-populated from live data — no hardcoding.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import discord
import data_manager as dm

try:
    import roster
except ImportError:
    roster = None  # type: ignore[assignment]

# ── Constants ─────────────────────────────────────────────────────────────────

# Canonical position groups for the filter dropdown.
# Grouped so the dropdown doesn't blow past 25 options.
POS_GROUPS: dict[str, list[str]] = {
    "QB":          ["QB"],
    "RB":          ["HB", "FB"],
    "WR":          ["WR"],
    "TE":          ["TE"],
    "OL":          ["LT", "LG", "C", "RG", "RT"],
    "Edge / DE":   ["LE", "RE", "LEDGE", "REDGE"],
    "DT":          ["DT"],
    "LB":          ["MLB", "LOLB", "ROLB", "MIKE", "WILL", "SAM"],
    "CB":          ["CB"],
    "S":           ["FS", "SS"],
    "K / P":       ["K", "P"],
}

from atlas_colors import AtlasColors
TSL_GOLD  = AtlasColors.TSL_GOLD.value
TSL_BLACK = 0x1A1A1A

# ── Helpers ───────────────────────────────────────────────────────────────────

def _all_players() -> list[dict]:
    """Return full roster. Prefer /export/players CSV over stat leaders."""
    players = dm.get_players()
    if not players and not dm.df_players.empty:
        players = dm.df_players.to_dict(orient="records")
    return players or []


def _pos_of(p: dict) -> str:
    return str(p.get("pos") or p.get("position") or "").upper().strip()


def _team_nick(p: dict) -> str:
    return str(p.get("teamName") or "").strip()


def _ovr(p: dict) -> int:
    try:
        return int(float(p.get("playerBestOvr") or p.get("overallRating") or 0))
    except (ValueError, TypeError):
        return 0


def _display_name(p: dict) -> str:
    fn = str(p.get("firstName") or "").strip()
    ln = str(p.get("lastName") or "").strip()
    return f"{fn} {ln}".strip() or str(p.get("rosterId", "Unknown"))


def _player_label(p: dict) -> str:
    """Short label for Select option: 'J. Jefferson WR · OVR 96 · Vikings'"""
    name = _display_name(p)
    # Abbreviate first name
    parts = name.split()
    if len(parts) >= 2:
        name_short = f"{parts[0][0]}. {' '.join(parts[1:])}"
    else:
        name_short = name
    pos  = _pos_of(p) or "?"
    ovr  = _ovr(p)
    team = _team_nick(p) or "FA"
    dev_raw = str(p.get("dev") or "")
    dev_icon = {"Superstar X-Factor": "⚡", "Superstar": "★", "Star": "✦"}.get(dev_raw, "")
    label = f"{dev_icon}{name_short} {pos} · {ovr} OVR · {team}"
    return label[:100]


def _get_team_options() -> list[discord.SelectOption]:
    """Build team dropdown options from live df_teams (sorted by nickName).

    Discord select menus are capped at 25 options. With 31 teams + "All Teams"
    that would be 32 entries. We always show "All Teams" first, then the first
    23 alphabetical teams, then a visible overflow indicator as option 25 so
    the user knows teams were omitted rather than silently dropping them.
    """
    players = _all_players()
    seen: set[str] = set()
    teams: list[str] = []
    for p in players:
        tn = _team_nick(p)
        if tn and tn not in seen:
            seen.add(tn)
            teams.append(tn)
    sorted_teams = sorted(teams)

    # Slot 1: "All Teams"; slots 2–25: up to 23 teams + optional overflow marker
    _MAX_TEAMS = 23
    options = [discord.SelectOption(label="All Teams", value="ALL", default=True)]
    for tn in sorted_teams[:_MAX_TEAMS]:
        options.append(discord.SelectOption(label=tn, value=tn))

    overflow_count = len(sorted_teams) - _MAX_TEAMS
    if overflow_count > 0:
        options.append(discord.SelectOption(
            label=f"(+{overflow_count} more — use /players filter)",
            value="__overflow__",
            description="Too many teams to list. Use /players with a name search instead.",
        ))

    return options


def _get_pos_options() -> list[discord.SelectOption]:
    """Build position group dropdown options."""
    options = [discord.SelectOption(label="All Positions", value="ALL", default=True)]
    for group in POS_GROUPS:
        options.append(discord.SelectOption(label=group, value=group))
    return options


def _filter_players(
    pos_group: str = "ALL",
    team: str = "ALL",
    search: str = "",
) -> list[dict]:
    """
    Filter + sort players. Returns up to 25, OVR descending.
    pos_group: POS_GROUPS key or 'ALL'
    team: team nickName or 'ALL'
    search: substring search against full name (optional)
    """
    players = _all_players()

    if pos_group != "ALL":
        allowed = POS_GROUPS.get(pos_group, [])
        players = [p for p in players if _pos_of(p) in allowed]

    if team != "ALL":
        players = [p for p in players if _team_nick(p).lower() == team.lower()]

    if search.strip():
        q = search.strip().lower()
        players = [p for p in players if q in _display_name(p).lower()]

    # Sort OVR desc, then name
    players.sort(key=lambda p: (-_ovr(p), _display_name(p)))
    return players[:25]


def _build_player_options(players: list[dict]) -> list[discord.SelectOption]:
    options = []
    for p in players:
        rid   = str(p.get("rosterId") or p.get("id") or hash((_display_name(p), _ovr(p), p.get("position", ""))))
        label = _player_label(p)
        desc  = f"{_display_name(p)} · {_team_nick(p)}"[:100]
        options.append(discord.SelectOption(label=label, value=rid, description=desc))
    return options or [discord.SelectOption(label="No players match", value="NONE")]


def _make_filter_embed(pos_group: str = "ALL", team: str = "ALL", cart: list[dict] | None = None) -> discord.Embed:
    """Build the persistent filter status embed shown above the dropdowns."""
    embed = discord.Embed(title="🔍 Player Picker", color=TSL_GOLD)
    filters = []
    if pos_group != "ALL":
        filters.append(f"**Position:** {pos_group}")
    if team != "ALL":
        filters.append(f"**Team:** {team}")
    embed.description = (
        ("Active filters: " + " | ".join(filters)) if filters
        else "No filters applied — showing top 25 players by OVR."
    )
    if cart:
        names = [_display_name(p) for p in cart]
        embed.add_field(
            name=f"🛒 Selected ({len(cart)})",
            value="\n".join(f"• {n}" for n in names) or "—",
            inline=False
        )
    embed.set_footer(text="Use the dropdowns to filter, then select a player.")
    return embed


# ── Player Picker View ────────────────────────────────────────────────────────

class PlayerPickerView(discord.ui.View):
    """
    Full filter + select flow in one view.
    Renders two filter dropdowns (Position, Team) and a player list dropdown.
    Calls callback(interaction, player) for single, callback(interaction, [players]) for multi.

    Parameters:
        callback    — async callable(interaction, player_or_list)
        multi       — True = cart mode, user can add multiple players
        max_picks   — cap on multi-select (default 10)
        team_filter — pre-set team filter (e.g. "Ravens"); user can still change it
        pos_filter  — pre-set position filter
        label       — custom title shown in the embed
    """

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

    # ── Public helpers ────────────────────────────────────────────────────────

    @staticmethod
    def filter_embed(label: str = "Player Picker") -> discord.Embed:
        """Static method — returns a starter embed before any filters are chosen."""
        embed = discord.Embed(title=f"🔍 {label}", color=TSL_GOLD)
        embed.description = "Use the dropdowns to filter by position and team, then select a player."
        embed.set_footer(text="Showing top 25 players by OVR within the selected filters.")
        return embed

    def current_embed(self) -> discord.Embed:
        return _make_filter_embed(self._pos_group, self._team, self._cart if self._multi else None)

    # ── Internal rebuild ──────────────────────────────────────────────────────

    def _rebuild_items(self):
        """Clear and re-add all items based on current filter state."""
        self.clear_items()

        # Row 0: Position filter
        pos_sel = discord.ui.Select(
            placeholder=f"Position: {self._pos_group}",
            options=_get_pos_options(),
            row=0,
            min_values=1, max_values=1,
        )
        # Mark current selection
        for opt in pos_sel.options:
            opt.default = (opt.value == self._pos_group)
        pos_sel.callback = self._on_pos_change
        self.add_item(pos_sel)

        # Row 1: Team filter
        team_opts = _get_team_options()
        for opt in team_opts:
            opt.default = (opt.value == self._team)
        team_sel = discord.ui.Select(
            placeholder=f"Team: {self._team}",
            options=team_opts,
            row=1,
            min_values=1, max_values=1,
        )
        team_sel.callback = self._on_team_change
        self.add_item(team_sel)

        # Row 2: Player list
        player_opts = _build_player_options(self._players)
        player_sel  = discord.ui.Select(
            placeholder="Select a player...",
            options=player_opts,
            row=2,
            min_values=1, max_values=1,
            disabled=(not self._players),
        )
        player_sel.callback = self._on_player_select
        self.add_item(player_sel)

        # Row 3: Multi mode — Submit cart button
        if self._multi and self._cart:
            submit_btn = discord.ui.Button(
                label=f"✅ Submit ({len(self._cart)} selected)",
                style=discord.ButtonStyle.success,
                row=3,
            )
            submit_btn.callback = self._on_submit_cart
            self.add_item(submit_btn)

            clear_btn = discord.ui.Button(
                label="🗑️ Clear Cart",
                style=discord.ButtonStyle.danger,
                row=3,
            )
            clear_btn.callback = self._on_clear_cart
            self.add_item(clear_btn)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _on_pos_change(self, interaction: discord.Interaction):
        self._pos_group = interaction.data["values"][0]
        self._players   = _filter_players(self._pos_group, self._team)
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def _on_team_change(self, interaction: discord.Interaction):
        selected = interaction.data["values"][0]
        if selected == "__overflow__":
            # User clicked the overflow placeholder — ignore and keep current state
            return await interaction.response.defer()
        self._team    = selected
        self._players = _filter_players(self._pos_group, self._team)
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def _on_player_select(self, interaction: discord.Interaction):
        selected_rid = interaction.data["values"][0]
        if selected_rid == "NONE":
            return await interaction.response.defer()

        # Find the player by rosterId
        player = next(
            (p for p in self._players
             if str(p.get("rosterId") or p.get("id") or id(p)) == selected_rid),
            None
        )
        if not player:
            return await interaction.response.send_message("❌ Player not found.", ephemeral=True)

        if self._multi:
            # Cart mode — add to cart, cap at max_picks
            name = _display_name(player)
            already = any(
                str(p.get("rosterId") or p.get("id") or id(p)) == selected_rid
                for p in self._cart
            )
            if already:
                await interaction.response.send_message(
                    f"⚠️ **{name}** is already in your selection.", ephemeral=True
                )
                return
            if len(self._cart) >= self._max_picks:
                await interaction.response.send_message(
                    f"⚠️ Maximum {self._max_picks} players allowed.", ephemeral=True
                )
                return
            self._cart.append(player)
            self._rebuild_items()
            await interaction.response.edit_message(embed=self.current_embed(), view=self)
        else:
            # Single mode — fire callback immediately
            self.stop()
            await self._callback(interaction, player)

    async def _on_submit_cart(self, interaction: discord.Interaction):
        if not self._cart:
            return await interaction.response.send_message("⚠️ Nothing selected yet.", ephemeral=True)
        self.stop()
        await self._callback(interaction, list(self._cart))

    async def _on_clear_cart(self, interaction: discord.Interaction):
        self._cart = []
        self._rebuild_items()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def on_timeout(self):
        # Disable all items and update the message
        for item in self.children:
            item.disabled = True
        if hasattr(self, "message") and self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                pass


# ── Convenience factory functions ─────────────────────────────────────────────

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
