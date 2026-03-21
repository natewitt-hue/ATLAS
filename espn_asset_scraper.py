"""
ESPN Asset Scraper — Team Branding + Player Headshots
=====================================================
Pulls team logos, colors, abbreviations, and player headshot URLs
from ESPN's public (unofficial) API for all major sports leagues.

Outputs:
  assets/
    team_branding.json       — master lookup: teams by league
    player_headshots.json    — master lookup: players by league > team
    logos/{league}/{abbr}.png           — default team logos
    logos/{league}/{abbr}_dark.png      — dark-mode team logos
    headshots/{league}/{team_abbr}/{player_id}.png  — player headshots

Usage:
    py espn_asset_scraper.py                    # metadata only (JSON files)
    py espn_asset_scraper.py --download-logos    # + download team logos
    py espn_asset_scraper.py --download-all      # + download logos AND headshots
    py espn_asset_scraper.py --leagues NFL,NBA   # specific leagues only

Designed for: ATLAS™ / TSLVerse™
Author: Nathan (TheWitt) — generated via Claude
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ─────────────────────────────────────────────
# League Configuration
# ─────────────────────────────────────────────
LEAGUES = {
    # Pro leagues
    "NFL":  {"sport": "football",   "league": "nfl",  "limit": None},
    "NBA":  {"sport": "basketball", "league": "nba",  "limit": None},
    "MLB":  {"sport": "baseball",   "league": "mlb",  "limit": None},
    "NHL":  {"sport": "hockey",     "league": "nhl",  "limit": None},
    "WNBA": {"sport": "basketball", "league": "wnba", "limit": None},
    "MLS":  {"sport": "soccer",     "league": "usa.1","limit": None},

    # International soccer
    "EPL":        {"sport": "soccer", "league": "eng.1",      "limit": None},
    "LA_LIGA":    {"sport": "soccer", "league": "esp.1",      "limit": None},
    "SERIE_A":    {"sport": "soccer", "league": "ita.1",      "limit": None},
    "BUNDESLIGA": {"sport": "soccer", "league": "ger.1",      "limit": None},
    "LIGUE_1":    {"sport": "soccer", "league": "fra.1",      "limit": None},

    # College (top programs — use limit to avoid 300+ teams)
    "NCAAFB": {"sport": "football",   "league": "college-football",          "limit": 150},
    "NCAAMB": {"sport": "basketball", "league": "mens-college-basketball",   "limit": 100},
    "NCAAWB": {"sport": "basketball", "league": "womens-college-basketball", "limit": 100},
}

BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ATLAS-Scraper/1.0"

# Rate limiting — be respectful
REQUEST_DELAY = 0.3          # seconds between API calls
DOWNLOAD_DELAY = 0.05        # seconds between image downloads
MAX_DOWNLOAD_WORKERS = 4     # parallel image download threads
MAX_RETRIES = 3


# ─────────────────────────────────────────────
# HTTP Helpers
# ─────────────────────────────────────────────
def fetch_json(url: str, retries: int = MAX_RETRIES) -> dict | None:
    """GET a URL and return parsed JSON, with retry logic."""
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"  ⚠ Attempt {attempt+1}/{retries} failed for {url}: {e}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)  # exponential backoff
        except json.JSONDecodeError as e:
            print(f"  ⚠ JSON decode error for {url}: {e}")
            return None
    return None


def download_image(url: str, dest: Path) -> bool:
    """Download an image to disk. Returns True on success."""
    if dest.exists():
        return True  # already cached
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            dest.write_bytes(resp.read())
        return True
    except Exception as e:
        print(f"  ⚠ Download failed: {dest.name} — {e}")
        return False


# ─────────────────────────────────────────────
# Team Branding Scraper
# ─────────────────────────────────────────────
def scrape_teams(league_key: str, cfg: dict) -> list[dict]:
    """Scrape all teams for a league, returning normalized team dicts."""
    url = f"{BASE_URL}/{cfg['sport']}/{cfg['league']}/teams"
    if cfg.get("limit"):
        url += f"?limit={cfg['limit']}"

    print(f"\n{'='*50}")
    print(f"  Scraping teams: {league_key}")
    print(f"  URL: {url}")

    data = fetch_json(url)
    if not data:
        print(f"  ✗ Failed to fetch teams for {league_key}")
        return []

    # Navigate ESPN's nested structure
    raw_teams = []
    if "sports" in data:
        raw_teams = data["sports"][0]["leagues"][0].get("teams", [])
    else:
        raw_teams = data.get("teams", [])

    teams = []
    for entry in raw_teams:
        t = entry.get("team", entry)

        # Extract logo URLs by variant
        logos = {}
        for logo in t.get("logos", []):
            rels = logo.get("rel", [])
            href = logo.get("href", "")
            if not href:
                continue

            # Classify logo variant
            if "dark" in rels and "scoreboard" in rels:
                logos["scoreboard_dark"] = href
            elif "scoreboard" in rels:
                logos["scoreboard"] = href
            elif "dark" in rels and "default" in rels:
                logos["dark"] = href
            elif "default" in rels:
                logos["default"] = href
            elif "primary_logo_on_white_color" in rels:
                logos["primary_on_white"] = href
            elif "primary_logo_on_black_color" in rels:
                logos["primary_on_black"] = href
            elif "primary_logo_on_primary_color" in rels:
                logos["primary_on_primary"] = href
            elif "secondary_logo_on_white_color" in rels:
                logos["secondary_on_white"] = href
            elif "secondary_logo_on_black_color" in rels:
                logos["secondary_on_black"] = href

        team_data = {
            "espn_id":          t.get("id", ""),
            "abbreviation":     t.get("abbreviation", ""),
            "display_name":     t.get("displayName", ""),
            "short_name":       t.get("shortDisplayName", ""),
            "nickname":         t.get("name", t.get("nickname", "")),
            "location":         t.get("location", ""),
            "slug":             t.get("slug", ""),
            "color":            f"#{t['color']}" if t.get("color") else None,
            "alternate_color":  f"#{t['alternateColor']}" if t.get("alternateColor") else None,
            "logos":            logos,
            "league":           league_key,
        }
        teams.append(team_data)

    print(f"  ✓ Found {len(teams)} teams")
    time.sleep(REQUEST_DELAY)
    return teams


# ─────────────────────────────────────────────
# Player Roster + Headshot Scraper
# ─────────────────────────────────────────────
def scrape_roster(league_key: str, cfg: dict, team: dict) -> list[dict]:
    """Scrape the roster for a single team, returning player dicts with headshot URLs."""
    espn_id = team["espn_id"]
    abbr = team["abbreviation"]
    url = f"{BASE_URL}/{cfg['sport']}/{cfg['league']}/teams/{espn_id}/roster"

    data = fetch_json(url)
    if not data:
        print(f"    ⚠ No roster data for {abbr}")
        return []

    players = []
    for group in data.get("athletes", []):
        position_group = group.get("position", "Unknown")
        for athlete in group.get("items", []):
            headshot = athlete.get("headshot", {})
            headshot_url = headshot.get("href", "")

            # Build predictable headshot URL as fallback
            pid = athlete.get("id", "")
            sport_path = cfg["sport"] if cfg["sport"] != "soccer" else "soccer"
            # NFL uses "nfl", NBA uses "nba", etc.
            league_path = cfg["league"]
            # For pro leagues, the standard pattern works:
            fallback_url = f"https://a.espncdn.com/i/headshots/{league_path}/players/full/{pid}.png"

            player_data = {
                "espn_id":        pid,
                "name":           athlete.get("displayName", athlete.get("fullName", "Unknown")),
                "first_name":     athlete.get("firstName", ""),
                "last_name":      athlete.get("lastName", ""),
                "jersey":         athlete.get("jersey", ""),
                "position":       athlete.get("position", {}).get("abbreviation", "")
                                  if isinstance(athlete.get("position"), dict)
                                  else str(athlete.get("position", "")),
                "position_group": position_group,
                "headshot_url":   headshot_url or fallback_url,
                "team_abbr":      abbr,
                "team_name":      team["display_name"],
                "league":         league_key,
            }
            players.append(player_data)

    time.sleep(REQUEST_DELAY)
    return players


# ─────────────────────────────────────────────
# Image Downloaders
# ─────────────────────────────────────────────
def download_team_logos(teams: list[dict], base_dir: Path):
    """Download default + dark logos for all teams."""
    tasks = []
    for team in teams:
        league = team["league"]
        abbr = team["abbreviation"].lower()

        if team["logos"].get("default"):
            tasks.append((
                team["logos"]["default"],
                base_dir / "logos" / league / f"{abbr}.png"
            ))
        if team["logos"].get("dark"):
            tasks.append((
                team["logos"]["dark"],
                base_dir / "logos" / league / f"{abbr}_dark.png"
            ))
        if team["logos"].get("scoreboard"):
            tasks.append((
                team["logos"]["scoreboard"],
                base_dir / "logos" / league / f"{abbr}_scoreboard.png"
            ))

    print(f"\n  Downloading {len(tasks)} logo images...")
    _parallel_download(tasks)


def download_player_headshots(players: list[dict], base_dir: Path):
    """Download headshot PNGs for all players."""
    tasks = []
    for p in players:
        if not p["headshot_url"]:
            continue
        league = p["league"]
        team_abbr = p["team_abbr"].lower()
        pid = p["espn_id"]
        dest = base_dir / "headshots" / league / team_abbr / f"{pid}.png"
        tasks.append((p["headshot_url"], dest))

    print(f"\n  Downloading {len(tasks)} headshot images...")
    _parallel_download(tasks)


def _parallel_download(tasks: list[tuple[str, Path]]):
    """Download a list of (url, dest) pairs in parallel."""
    success = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as pool:
        futures = {
            pool.submit(download_image, url, dest): (url, dest)
            for url, dest in tasks
        }
        for i, future in enumerate(as_completed(futures), 1):
            if future.result():
                success += 1
            else:
                failed += 1
            if i % 100 == 0:
                print(f"    ... {i}/{len(tasks)} processed")
            time.sleep(DOWNLOAD_DELAY)

    print(f"  ✓ Downloaded: {success} | Failed: {failed}")


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="ESPN Asset Scraper — Team Branding + Player Headshots"
    )
    parser.add_argument(
        "--download-logos", action="store_true",
        help="Download team logo PNGs to assets/logos/"
    )
    parser.add_argument(
        "--download-headshots", action="store_true",
        help="Download player headshot PNGs to assets/headshots/"
    )
    parser.add_argument(
        "--download-all", action="store_true",
        help="Download both logos and headshots"
    )
    parser.add_argument(
        "--leagues", type=str, default=None,
        help="Comma-separated league keys to scrape (e.g. NFL,NBA,NHL). Default: all"
    )
    parser.add_argument(
        "--skip-rosters", action="store_true",
        help="Skip player roster scraping (teams/logos only)"
    )
    parser.add_argument(
        "--output-dir", type=str, default="assets",
        help="Output directory (default: ./assets)"
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine which leagues to scrape
    if args.leagues:
        selected = [k.strip().upper() for k in args.leagues.split(",")]
        league_configs = {k: v for k, v in LEAGUES.items() if k in selected}
        missing = [k for k in selected if k not in LEAGUES]
        if missing:
            print(f"⚠ Unknown leagues: {', '.join(missing)}")
            print(f"  Available: {', '.join(LEAGUES.keys())}")
    else:
        league_configs = LEAGUES

    do_logos = args.download_logos or args.download_all
    do_headshots = args.download_headshots or args.download_all

    # ── Phase 1: Scrape Teams ──
    print("\n" + "═"*60)
    print("  PHASE 1: TEAM BRANDING")
    print("═"*60)

    all_teams = []
    for league_key, cfg in league_configs.items():
        teams = scrape_teams(league_key, cfg)
        all_teams.extend(teams)

    # Organize by league for JSON output
    teams_by_league = {}
    for t in all_teams:
        league = t["league"]
        if league not in teams_by_league:
            teams_by_league[league] = []
        teams_by_league[league].append(t)

    # Write team branding JSON
    branding_path = output_dir / "team_branding.json"
    with open(branding_path, "w", encoding="utf-8") as f:
        json.dump(teams_by_league, f, indent=2, ensure_ascii=False)
    print(f"\n✓ Team branding saved: {branding_path}")
    print(f"  Total teams: {len(all_teams)} across {len(teams_by_league)} leagues")

    # ── Phase 2: Download Logos ──
    if do_logos:
        print("\n" + "═"*60)
        print("  PHASE 2: DOWNLOADING LOGOS")
        print("═"*60)
        download_team_logos(all_teams, output_dir)

    # ── Phase 3: Scrape Rosters + Headshots ──
    if not args.skip_rosters:
        print("\n" + "═"*60)
        print("  PHASE 3: PLAYER ROSTERS + HEADSHOTS")
        print("═"*60)

        all_players = []
        for league_key, cfg in league_configs.items():
            league_teams = teams_by_league.get(league_key, [])
            print(f"\n  {league_key}: Scraping rosters for {len(league_teams)} teams...")

            for i, team in enumerate(league_teams, 1):
                abbr = team["abbreviation"]
                players = scrape_roster(league_key, cfg, team)
                all_players.extend(players)

                if i % 10 == 0:
                    print(f"    ... {i}/{len(league_teams)} teams done")

        # Organize by league > team for JSON output
        players_by_league = {}
        for p in all_players:
            league = p["league"]
            team = p["team_abbr"]
            if league not in players_by_league:
                players_by_league[league] = {}
            if team not in players_by_league[league]:
                players_by_league[league][team] = []
            players_by_league[league][team].append(p)

        # Write player headshots JSON
        headshots_path = output_dir / "player_headshots.json"
        with open(headshots_path, "w", encoding="utf-8") as f:
            json.dump(players_by_league, f, indent=2, ensure_ascii=False)
        print(f"\n✓ Player headshots saved: {headshots_path}")
        print(f"  Total players: {len(all_players)}")

        # ── Phase 4: Download Headshot Images ──
        if do_headshots:
            print("\n" + "═"*60)
            print("  PHASE 4: DOWNLOADING HEADSHOT IMAGES")
            print("═"*60)
            print("  ⚠ This will download thousands of images. Be patient.")
            download_player_headshots(all_players, output_dir)

    # ── Summary ──
    print("\n" + "═"*60)
    print("  COMPLETE")
    print("═"*60)
    print(f"  Output directory: {output_dir.resolve()}")
    print(f"  Teams scraped:    {len(all_teams)}")
    if not args.skip_rosters:
        print(f"  Players scraped:  {len(all_players)}")
    print(f"\n  Files:")
    print(f"    {branding_path}")
    if not args.skip_rosters:
        print(f"    {headshots_path}")
    if do_logos:
        print(f"    {output_dir / 'logos/'} (team logo PNGs)")
    if do_headshots:
        print(f"    {output_dir / 'headshots/'} (player headshot PNGs)")

    # ── Quick-reference: JSON structure ──
    print(f"""
  ┌─────────────────────────────────────────────────────┐
  │  team_branding.json structure:                      │
  │                                                     │
  │  {{"NFL": [                                          │
  │    {{"espn_id": "33",                                │
  │     "abbreviation": "BAL",                          │
  │     "display_name": "Baltimore Ravens",             │
  │     "nickname": "Ravens",                           │
  │     "color": "#29126f",                             │
  │     "alternate_color": "#000000",                   │
  │     "logos": {{                                      │
  │       "default": "https://a.espncdn.com/...",       │
  │       "dark": "https://a.espncdn.com/...",          │
  │       ...                                           │
  │     }}                                               │
  │    }}, ...                                            │
  │  ]}}                                                  │
  ├─────────────────────────────────────────────────────┤
  │  player_headshots.json structure:                   │
  │                                                     │
  │  {{"NFL": {{"BAL": [                                  │
  │    {{"espn_id": "3916387",                           │
  │     "name": "Lamar Jackson",                        │
  │     "jersey": "8",                                  │
  │     "position": "QB",                               │
  │     "headshot_url": "https://a.espncdn.com/..."     │
  │    }}, ...                                            │
  │  ]}}, ...}}                                            │
  └─────────────────────────────────────────────────────┘
""")


if __name__ == "__main__":
    main()
