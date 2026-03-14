"""
roster.py — ATLAS Owner Registry
─────────────────────────────────────────────────────────────────────────────
Single source of truth for Discord user ↔ team assignments.

All modules should import from here instead of maintaining hardcoded dicts.

Usage:
    import roster

    roster.load()                              # Call once at startup (after data_manager.load_all)
    owner = roster.get_owner("CHI")            # OwnerEntry or None
    team  = roster.get_team(discord_id)        # "CHI" or None
    afc   = roster.get_by_conference("AFC")    # list[OwnerEntry]

    roster.assign(discord_id, "CHI")           # Commissioner command
    roster.unassign(discord_id)                # Commissioner command

Storage: tsl_members.team column in tsl_history.db
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

import discord

# ── DB path (same as build_member_db) ─────────────────────────────────────────
_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_DIR, "tsl_history.db")

# Optional import — may not be ready at import time
try:
    import data_manager as dm
except ImportError:
    dm = None  # type: ignore[assignment]


# ══════════════════════════════════════════════════════════════════════════════
# Section 1 — Data Model
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class OwnerEntry:
    """Represents one owner ↔ team assignment."""
    discord_id:       int
    discord_username: str
    db_username:      str | None
    nickname:         str | None
    team_abbr:        str           # "CHI", "CIN", etc.
    team_name:        str           # "Bears", "Bengals" — from data_manager
    conference:       str           # "AFC" or "NFC"     — from data_manager


# ══════════════════════════════════════════════════════════════════════════════
# Section 2 — In-Memory Cache
# ══════════════════════════════════════════════════════════════════════════════

_by_team: dict[str, OwnerEntry] = {}    # team_abbr (upper) → OwnerEntry
_by_id:   dict[int, OwnerEntry] = {}    # discord_id         → OwnerEntry
_loaded: bool = False


# ══════════════════════════════════════════════════════════════════════════════
# Section 3 — Team / Conference Lookups (from data_manager)
# ══════════════════════════════════════════════════════════════════════════════

def _team_name(abbr: str) -> str:
    """Look up full team nickname from live API data. 'CHI' → 'Bears'."""
    if dm is None or dm.df_teams is None:
        return abbr
    for _, row in dm.df_teams.iterrows():
        if str(row.get("abbrName", "")).upper() == abbr.upper():
            return str(row.get("nickName", abbr))
    return abbr


def _team_conference(abbr: str) -> str:
    """Look up conference from live API data. Returns 'AFC' or 'NFC'."""
    if dm is None or dm.df_teams is None:
        return ""
    for _, row in dm.df_teams.iterrows():
        if str(row.get("abbrName", "")).upper() == abbr.upper():
            div = str(row.get("divName", ""))
            return "AFC" if div.upper().startswith("AFC") else "NFC"
    return ""


def get_all_teams() -> list[dict]:
    """Return list of all 32 teams as dicts with abbrName, nickName, conference."""
    if dm is None or dm.df_teams is None:
        return []
    teams = []
    for _, row in dm.df_teams.iterrows():
        abbr = str(row.get("abbrName", ""))
        nick = str(row.get("nickName", ""))
        div  = str(row.get("divName", ""))
        conf = "AFC" if div.upper().startswith("AFC") else "NFC"
        if abbr:
            teams.append({"abbrName": abbr, "nickName": nick, "conference": conf})
    return sorted(teams, key=lambda t: t["nickName"])


# ══════════════════════════════════════════════════════════════════════════════
# Section 4 — Load / Refresh Cache
# ══════════════════════════════════════════════════════════════════════════════

def load(db_path: str = DB_PATH) -> int:
    """Load team assignments from tsl_members into memory.

    Call once at startup AFTER data_manager.load_all().
    Returns number of assignments loaded.
    """
    global _loaded
    _by_team.clear()
    _by_id.clear()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT discord_id, discord_username, db_username, nickname, team
        FROM tsl_members
        WHERE team IS NOT NULL AND team != '' AND active = 1
    """).fetchall()
    conn.close()

    for r in rows:
        did_str = r["discord_id"]
        if not did_str:
            continue
        try:
            did = int(did_str)
        except (ValueError, TypeError):
            continue

        abbr = str(r["team"]).upper()
        entry = OwnerEntry(
            discord_id=did,
            discord_username=r["discord_username"] or "",
            db_username=r["db_username"],
            nickname=r["nickname"],
            team_abbr=abbr,
            team_name=_team_name(abbr),
            conference=_team_conference(abbr),
        )
        _by_team[abbr] = entry
        _by_id[did] = entry

    _loaded = True
    return len(_by_team)


# ══════════════════════════════════════════════════════════════════════════════
# Section 5 — Read API
# ══════════════════════════════════════════════════════════════════════════════

def get_owner(team_abbr: str) -> OwnerEntry | None:
    """Look up owner by team abbreviation. 'CHI' → OwnerEntry or None."""
    return _by_team.get(team_abbr.upper())


def get_team(discord_id: int) -> str | None:
    """Look up team abbreviation by Discord ID. Returns 'CHI' or None."""
    entry = _by_id.get(discord_id)
    return entry.team_abbr if entry else None


def get_entry_by_id(discord_id: int) -> OwnerEntry | None:
    """Full OwnerEntry lookup by Discord ID."""
    return _by_id.get(discord_id)


def get_all() -> list[OwnerEntry]:
    """Return all current team assignments, sorted by team name."""
    return sorted(_by_team.values(), key=lambda e: e.team_name)


def get_by_conference(conf: str) -> list[OwnerEntry]:
    """Filter assignments by conference ('AFC' or 'NFC'), sorted by team name."""
    c = conf.upper()
    return sorted(
        [e for e in _by_team.values() if e.conference == c],
        key=lambda e: e.team_name,
    )


def get_nickname(discord_id: int) -> str | None:
    """Shortcut: discord_id → nickname (or None)."""
    entry = _by_id.get(discord_id)
    return entry.nickname if entry else None


def get_team_name(discord_id: int) -> str | None:
    """Shortcut: discord_id → team nickname like 'Bears' (or None)."""
    entry = _by_id.get(discord_id)
    return entry.team_name if entry else None


def is_loaded() -> bool:
    """Whether the roster cache has been populated."""
    return _loaded


# ══════════════════════════════════════════════════════════════════════════════
# Section 6 — Write API (Commissioner Only)
# ══════════════════════════════════════════════════════════════════════════════

def assign(discord_id: int, team_abbr: str, db_path: str = DB_PATH) -> bool:
    """Assign a Discord user to a team. Updates DB + refreshes cache.

    Returns True if successful, False if discord_id not found in tsl_members.
    """
    abbr = team_abbr.upper()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Verify member exists
    row = cur.execute(
        "SELECT discord_username FROM tsl_members WHERE discord_id = ?",
        (str(discord_id),),
    ).fetchone()
    if not row:
        conn.close()
        return False

    # Clear any other member who currently holds this team
    cur.execute(
        "UPDATE tsl_members SET team = NULL WHERE team = ? AND discord_id != ?",
        (abbr, str(discord_id)),
    )

    # Set the assignment
    cur.execute(
        "UPDATE tsl_members SET team = ? WHERE discord_id = ?",
        (abbr, str(discord_id)),
    )
    conn.commit()
    conn.close()

    # Refresh in-memory cache
    load(db_path)
    return True


def unassign(discord_id: int, db_path: str = DB_PATH) -> bool:
    """Remove a user's team assignment. Updates DB + refreshes cache.

    Returns True if the user had an assignment, False otherwise.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    row = cur.execute(
        "SELECT team FROM tsl_members WHERE discord_id = ?",
        (str(discord_id),),
    ).fetchone()
    if not row or not row[0]:
        conn.close()
        return False

    cur.execute(
        "UPDATE tsl_members SET team = NULL WHERE discord_id = ?",
        (str(discord_id),),
    )
    conn.commit()
    conn.close()

    load(db_path)
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Section 7 — Discord UI Components
# ══════════════════════════════════════════════════════════════════════════════

def build_owner_options(conf: str, *, exclude_id: int | None = None) -> list[discord.SelectOption]:
    """Build select menu options for owners in a conference.

    Each option: label='Bears — Troy', value=str(discord_id)
    Unassigned teams are skipped.
    """
    entries = get_by_conference(conf)
    options = []
    for e in entries:
        if exclude_id and e.discord_id == exclude_id:
            continue
        label = f"{e.team_name} — {e.nickname or e.discord_username}"
        options.append(discord.SelectOption(
            label=label[:100],
            value=str(e.discord_id),
            description=e.team_abbr,
        ))
    return options


def build_team_options(conf: str) -> list[discord.SelectOption]:
    """Build select menu options for ALL teams in a conference (assigned or not).

    Each option: label='Bears', value='CHI', description='Owner: Troy' or 'Unassigned'
    Used by /commish assign to pick a team.
    """
    all_teams = get_all_teams()
    options = []
    for t in all_teams:
        if t["conference"] != conf.upper():
            continue
        abbr = t["abbrName"]
        nick = t["nickName"]
        owner = get_owner(abbr)
        desc = f"Owner: {owner.nickname or owner.discord_username}" if owner else "Unassigned"
        options.append(discord.SelectOption(
            label=nick,
            value=abbr,
            description=desc,
        ))
    return options


class _OwnerListSelect(discord.ui.Select):
    """Select menu showing owners from one conference."""

    def __init__(self, options: list[discord.SelectOption], callback_fn):
        super().__init__(
            placeholder="Select an owner...",
            min_values=1, max_values=1,
            options=options,
        )
        self._callback_fn = callback_fn

    async def callback(self, interaction: discord.Interaction):
        discord_id = int(self.values[0])
        entry = get_entry_by_id(discord_id)
        await self._callback_fn(interaction, entry)


class _OwnerListView(discord.ui.View):
    """Wraps _OwnerListSelect with a back button."""

    def __init__(self, options: list[discord.SelectOption], callback_fn, parent_view):
        super().__init__(timeout=180)
        self.add_item(_OwnerListSelect(options, callback_fn))
        self._parent = parent_view

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=self._parent)


class OwnerSelectView(discord.ui.View):
    """Reusable two-step owner picker: Conference buttons -> Owner select.

    Usage:
        async def on_pick(interaction, owner: OwnerEntry):
            await interaction.response.send_message(f"Picked {owner.nickname}")

        view = OwnerSelectView(callback=on_pick)
        await interaction.response.send_message("Pick an owner:", view=view)
    """

    def __init__(self, callback, *, exclude_id: int | None = None, timeout: int = 180):
        super().__init__(timeout=timeout)
        self._callback = callback
        self._exclude_id = exclude_id

    @discord.ui.button(label="AFC", style=discord.ButtonStyle.primary, emoji="\U0001f3c8")
    async def afc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_conference(interaction, "AFC")

    @discord.ui.button(label="NFC", style=discord.ButtonStyle.secondary, emoji="\U0001f3c8")
    async def nfc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_conference(interaction, "NFC")

    async def _show_conference(self, interaction: discord.Interaction, conf: str):
        options = build_owner_options(conf, exclude_id=self._exclude_id)
        if not options:
            return await interaction.response.send_message(
                f"No assigned owners in {conf}.", ephemeral=True,
            )
        view = _OwnerListView(options, self._callback, parent_view=self)
        await interaction.response.edit_message(view=view)


class _TeamAssignSelect(discord.ui.Select):
    """Select menu for picking a team to assign (used by /commish assign)."""

    def __init__(self, options: list[discord.SelectOption], member: discord.Member):
        super().__init__(
            placeholder="Select a team...",
            min_values=1, max_values=1,
            options=options,
        )
        self._member = member

    async def callback(self, interaction: discord.Interaction):
        team_abbr = self.values[0]
        team_nick = _team_name(team_abbr)

        success = assign(self._member.id, team_abbr)
        if not success:
            return await interaction.response.edit_message(
                content=f"Failed to assign **{self._member.display_name}** — "
                        f"user not found in member registry.",
                view=None, embed=None,
            )

        embed = discord.Embed(
            title="Team Assignment Updated",
            description=(
                f"**{self._member.display_name}** (<@{self._member.id}>) "
                f"has been assigned to the **{team_nick}** ({team_abbr})."
            ),
            color=discord.Color.green(),
        )
        await interaction.response.edit_message(embed=embed, view=None, content=None)


class _TeamAssignView(discord.ui.View):
    """Wraps _TeamAssignSelect with a back button."""

    def __init__(self, options: list[discord.SelectOption], member: discord.Member, parent_view):
        super().__init__(timeout=180)
        self.add_item(_TeamAssignSelect(options, member))
        self._parent = parent_view

    @discord.ui.button(label="\u2190 Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(view=self._parent)


class AssignConferenceView(discord.ui.View):
    """AFC / NFC buttons for /commish assign — shows teams to assign."""

    def __init__(self, member: discord.Member):
        super().__init__(timeout=180)
        self._member = member

    @discord.ui.button(label="AFC", style=discord.ButtonStyle.primary, emoji="\U0001f3c8")
    async def afc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_teams(interaction, "AFC")

    @discord.ui.button(label="NFC", style=discord.ButtonStyle.secondary, emoji="\U0001f3c8")
    async def nfc_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._show_teams(interaction, "NFC")

    async def _show_teams(self, interaction: discord.Interaction, conf: str):
        options = build_team_options(conf)
        if not options:
            return await interaction.response.send_message(
                f"No {conf} teams found.", ephemeral=True,
            )
        view = _TeamAssignView(options, self._member, parent_view=self)
        await interaction.response.edit_message(view=view)
