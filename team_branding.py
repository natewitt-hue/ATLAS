"""
Team Branding Lookup — ATLAS™ Integration Module
=================================================
Loads the ESPN-scraped team_branding.json and player_headshots.json
and provides fast lookups by nickname, abbreviation, or ESPN ID.

Usage in ATLAS:
    from team_branding import TeamBranding

    branding = TeamBranding("assets/team_branding.json", "assets/player_headshots.json")

    # Lookup by Madden export nickName
    ravens = branding.by_nickname("Ravens")
    # => {"espn_id": "33", "abbreviation": "BAL", "color": "#29126f", ...}

    # Get logo URL (for Pillow or Playwright rendering)
    logo_url = branding.logo_url("Ravens", variant="default")

    # Get team colors as RGB tuples (for Pillow)
    primary, secondary = branding.colors_rgb("Ravens")

    # Get player headshot
    headshot = branding.player_headshot("NFL", "BAL", "Lamar Jackson")

    # Get all players on a team
    roster = branding.team_roster("NFL", "BAL")
"""

import json
from pathlib import Path
from typing import Optional


class TeamBranding:
    """Fast lookup for team branding and player headshots from ESPN data."""

    def __init__(
        self,
        branding_path: str = "assets/team_branding.json",
        headshots_path: str = "assets/player_headshots.json",
        default_league: str = "NFL",
    ):
        self.default_league = default_league

        # Load team branding
        bp = Path(branding_path)
        if bp.exists():
            with open(bp, "r", encoding="utf-8") as f:
                self._raw = json.load(f)
        else:
            print(f"⚠ Team branding file not found: {bp}")
            self._raw = {}

        # Load player headshots
        hp = Path(headshots_path)
        if hp.exists():
            with open(hp, "r", encoding="utf-8") as f:
                self._players_raw = json.load(f)
        else:
            self._players_raw = {}

        # Build lookup indexes
        self._by_nickname = {}   # {league: {nickname_lower: team_dict}}
        self._by_abbr = {}       # {league: {abbr_upper: team_dict}}
        self._by_espn_id = {}    # {league: {espn_id: team_dict}}

        for league, teams in self._raw.items():
            self._by_nickname[league] = {}
            self._by_abbr[league] = {}
            self._by_espn_id[league] = {}
            for team in teams:
                nick = team.get("nickname", "").lower()
                abbr = team.get("abbreviation", "").upper()
                eid = str(team.get("espn_id", ""))

                if nick:
                    self._by_nickname[league][nick] = team
                if abbr:
                    self._by_abbr[league][abbr] = team
                if eid:
                    self._by_espn_id[league][eid] = team

    # ─── Team Lookups ───

    def by_nickname(self, nickname: str, league: str = None) -> Optional[dict]:
        """Lookup team by nickname (e.g. 'Ravens', 'Bears'). Case-insensitive."""
        league = league or self.default_league
        return self._by_nickname.get(league, {}).get(nickname.lower())

    def by_abbreviation(self, abbr: str, league: str = None) -> Optional[dict]:
        """Lookup team by abbreviation (e.g. 'BAL', 'CHI'). Case-insensitive."""
        league = league or self.default_league
        return self._by_abbr.get(league, {}).get(abbr.upper())

    def by_espn_id(self, espn_id: str, league: str = None) -> Optional[dict]:
        """Lookup team by ESPN team ID."""
        league = league or self.default_league
        return self._by_espn_id.get(league, {}).get(str(espn_id))

    def all_teams(self, league: str = None) -> list[dict]:
        """Get all teams for a league."""
        league = league or self.default_league
        return self._raw.get(league, [])

    def all_leagues(self) -> list[str]:
        """Get all available league keys."""
        return list(self._raw.keys())

    # ─── Logo URLs ───

    def logo_url(self, nickname: str, variant: str = "default", league: str = None) -> Optional[str]:
        """
        Get a team logo URL by nickname.

        Variants: 'default', 'dark', 'scoreboard', 'scoreboard_dark',
                  'primary_on_white', 'primary_on_black', 'primary_on_primary',
                  'secondary_on_white', 'secondary_on_black'
        """
        team = self.by_nickname(nickname, league)
        if not team:
            return None
        return team.get("logos", {}).get(variant)

    def logo_path(self, nickname: str, variant: str = "default",
                  league: str = None, base_dir: str = "assets") -> Optional[Path]:
        """Get the local file path for a downloaded logo."""
        team = self.by_nickname(nickname, league)
        if not team:
            return None
        league = league or self.default_league
        abbr = team["abbreviation"].lower()
        suffix = f"_{variant}" if variant != "default" else ""
        path = Path(base_dir) / "logos" / league / f"{abbr}{suffix}.png"
        return path if path.exists() else None

    # ─── Colors ───

    def colors(self, nickname: str, league: str = None) -> tuple[str, str]:
        """Get (primary_hex, alternate_hex) for a team. Returns ('#000000', '#ffffff') as fallback."""
        team = self.by_nickname(nickname, league)
        if not team:
            return ("#000000", "#ffffff")
        return (
            team.get("color") or "#000000",
            team.get("alternate_color") or "#ffffff",
        )

    def colors_rgb(self, nickname: str, league: str = None) -> tuple[tuple, tuple]:
        """Get colors as RGB tuples for Pillow: ((r,g,b), (r,g,b))."""
        primary, alt = self.colors(nickname, league)
        return (_hex_to_rgb(primary), _hex_to_rgb(alt))

    # ─── Player Lookups ───

    def team_roster(self, league: str, team_abbr: str) -> list[dict]:
        """Get all players for a team."""
        return self._players_raw.get(league, {}).get(team_abbr.upper(), [])

    def player_headshot(self, league: str, team_abbr: str, player_name: str) -> Optional[str]:
        """Find a player's headshot URL by name (case-insensitive partial match)."""
        roster = self.team_roster(league, team_abbr)
        name_lower = player_name.lower()
        for p in roster:
            if name_lower in p.get("name", "").lower():
                return p.get("headshot_url")
        return None

    def player_by_espn_id(self, league: str, team_abbr: str, espn_id: str) -> Optional[dict]:
        """Find a player by ESPN ID."""
        for p in self.team_roster(league, team_abbr):
            if str(p.get("espn_id")) == str(espn_id):
                return p
        return None

    def player_headshot_path(self, league: str, team_abbr: str, espn_id: str,
                             base_dir: str = "assets") -> Optional[Path]:
        """Get local path for a downloaded headshot."""
        path = Path(base_dir) / "headshots" / league / team_abbr.lower() / f"{espn_id}.png"
        return path if path.exists() else None

    # ─── Madden Export Helpers ───

    def mm_nickname_to_branding(self, mm_nickname: str) -> Optional[dict]:
        """
        Map a Madden Manager export nickName (from teams.csv) to ESPN branding.
        This is the primary integration point for ATLAS.
        """
        return self.by_nickname(mm_nickname, "NFL")

    def mm_team_colors_pillow(self, mm_nickname: str) -> tuple[tuple, tuple]:
        """Get Pillow-ready RGB tuples from a Madden nickName."""
        return self.colors_rgb(mm_nickname, "NFL")

    def mm_team_logo_path(self, mm_nickname: str, variant: str = "default",
                          base_dir: str = "assets") -> Optional[Path]:
        """Get local logo path from a Madden nickName."""
        return self.logo_path(mm_nickname, variant, "NFL", base_dir)


# ─── Utility ───

def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' or 'RRGGBB' to (R, G, B) tuple."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return (0, 0, 0)
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# ─── Quick Test ───

if __name__ == "__main__":
    branding = TeamBranding()

    print("Available leagues:", branding.all_leagues())
    print()

    # Test NFL lookups
    for nick in ["Ravens", "Bears", "Chiefs", "Cowboys"]:
        team = branding.by_nickname(nick)
        if team:
            primary, alt = branding.colors(nick)
            logo = branding.logo_url(nick)
            print(f"{nick:12s} | {team['abbreviation']} | {primary} / {alt} | logo: {bool(logo)}")
        else:
            print(f"{nick:12s} | NOT FOUND")

    # Test player lookup
    print()
    headshot = branding.player_headshot("NFL", "BAL", "Lamar Jackson")
    print(f"Lamar Jackson headshot: {headshot or 'NOT FOUND'}")
