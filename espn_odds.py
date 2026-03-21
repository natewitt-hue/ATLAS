"""
espn_odds.py — ESPN Odds Client for ATLAS™ Real Sportsbook
===========================================================
Fetches live odds, scores, win probabilities, and line movement from
ESPN's public (unofficial) API. Replaces TheRundown / The Odds API.

Endpoints used:
  Scoreboard:  site.api.espn.com/apis/site/v2/sports/{sport}/{league}/scoreboard
  Game Odds:   sports.core.api.espn.com/v2/sports/{sport}/leagues/{league}/events/{id}/competitions/{id}/odds
  Probabilities: .../competitions/{id}/probabilities
  ATS:         .../teams/{team_id}/ats
  Line Movement: .../odds/{provider_id}/history/0/movement

All endpoints are free, no auth, no API key.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from cachetools import TTLCache

log = logging.getLogger("espn_odds")

# ── League Configuration ─────────────────────────────────────────────────────
# Maps old sport_key (backward compat) → (espn_sport_path, espn_league_path, branding_league_key)
LEAGUE_MAP: dict[str, tuple[str, str, str]] = {
    "americanfootball_nfl": ("football",   "nfl",                       "NFL"),
    "basketball_nba":       ("basketball", "nba",                       "NBA"),
    "baseball_mlb":         ("baseball",   "mlb",                       "MLB"),
    "icehockey_nhl":        ("hockey",     "nhl",                       "NHL"),
    "basketball_ncaab":     ("basketball", "mens-college-basketball",   "NCAAB"),
    "mma_ufc":              ("mma",        "ufc",                       "UFC"),
    "soccer_epl":           ("soccer",     "eng.1",                     "EPL"),
    "soccer_mls":           ("soccer",     "usa.1",                     "MLS"),
    "basketball_wnba":      ("basketball", "wnba",                      "WNBA"),
}

# Reverse lookup: branding league key → sport_key
_LEAGUE_TO_SPORT_KEY = {v[2]: k for k, v in LEAGUE_MAP.items()}

# ── Provider IDs ─────────────────────────────────────────────────────────────
DEFAULT_PROVIDER = 1004  # Consensus

PROVIDERS: dict[int, str] = {
    37:   "FanDuel",
    38:   "Caesars",
    41:   "DraftKings",
    58:   "BetMGM",
    68:   "ESPN BET",
    2000: "Bet365",
    1004: "Consensus",
    1003: "NumberFire",
    1002: "TeamRankings",
}

# ── Supported sports display names (backward compat export) ──────────────────
SUPPORTED_SPORTS: dict[str, str] = {k: v[2] for k, v in LEAGUE_MAP.items()}

# ── Season Types ─────────────────────────────────────────────────────────────
SEASON_TYPES = {1: "Preseason", 2: "Regular Season", 3: "Postseason", 4: "Off Season"}


class ESPNOddsClient:
    """Async client for ESPN's public odds/scoreboard API.

    Returns ESPN-native normalized data enriched with TeamBranding
    colors and logos when a branding instance is provided.
    """

    BASE_CORE = "https://sports.core.api.espn.com/v2/sports"
    BASE_SITE = "https://site.api.espn.com/apis/site/v2/sports"

    # Rate limiting — be respectful of unofficial API
    _REQUEST_DELAY = 0.3   # seconds between requests
    _MAX_RETRIES = 3
    _TIMEOUT = 15          # seconds per request

    # Lookahead / lookback for syncing
    ODDS_DAYS_AHEAD = 7
    SCORES_DAYS_BACK = 3

    def __init__(self, branding=None):
        """Initialize ESPN odds client.

        Args:
            branding: Optional TeamBranding instance for logo/color enrichment.
        """
        self.branding = branding
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_lock = asyncio.Lock()
        self._last_request_time: float = 0.0

        # TTL caches — keys are URLs
        self._scoreboard_cache = TTLCache(maxsize=128, ttl=60)   # 60s for scoreboard
        self._odds_cache = TTLCache(maxsize=64, ttl=30)          # 30s for game odds
        self._prob_cache = TTLCache(maxsize=64, ttl=15)          # 15s for live probs
        self._ats_cache = TTLCache(maxsize=64, ttl=3600)         # 1hr for ATS
        self._cache_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ATLAS/1.0"},
            )
        return self._session

    async def close(self):
        """Close the HTTP session. Call from cog_unload."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ── HTTP Layer ────────────────────────────────────────────────────────────

    async def _fetch(self, url: str, cache: TTLCache | None = None) -> dict | None:
        """Fetch a URL with caching, rate limiting, and retry logic."""
        # Check cache
        if cache is not None:
            async with self._cache_lock:
                if url in cache:
                    return cache[url]

        session = await self._get_session()

        for attempt in range(self._MAX_RETRIES):
            async with self._request_lock:
                # Throttle
                loop = asyncio.get_event_loop()
                elapsed = loop.time() - self._last_request_time
                if elapsed < self._REQUEST_DELAY:
                    await asyncio.sleep(self._REQUEST_DELAY - elapsed)

                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=self._TIMEOUT)
                    ) as resp:
                        self._last_request_time = loop.time()

                        if resp.status == 200:
                            data = await resp.json()
                            if cache is not None:
                                async with self._cache_lock:
                                    cache[url] = data
                            return data

                        if resp.status == 404:
                            log.debug(f"ESPN 404: {url}")
                            return None

                        if resp.status == 429 and attempt < self._MAX_RETRIES - 1:
                            wait = 2 ** (attempt + 1)
                            log.warning(f"ESPN 429: retrying in {wait}s...")
                            await asyncio.sleep(wait)
                            continue

                        log.warning(f"ESPN API {resp.status}: {url}")
                        return None

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    log.error(f"ESPN request error: {e}")
                    if attempt < self._MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** (attempt + 1))
                    else:
                        return None

        return None

    # ── Path Helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _espn_paths(league: str) -> tuple[str, str] | None:
        """Convert a branding league key ('NFL') or sport_key to ESPN paths.

        Returns (sport_path, league_path) or None if unknown.
        """
        # Try as sport_key first
        if league in LEAGUE_MAP:
            return LEAGUE_MAP[league][0], LEAGUE_MAP[league][1]
        # Try as branding league key
        sport_key = _LEAGUE_TO_SPORT_KEY.get(league)
        if sport_key:
            return LEAGUE_MAP[sport_key][0], LEAGUE_MAP[sport_key][1]
        log.warning(f"Unknown league: {league}")
        return None

    @staticmethod
    def _sport_key_for(league: str) -> str:
        """Get the backward-compat sport_key for a branding league key."""
        return _LEAGUE_TO_SPORT_KEY.get(league, league)

    @staticmethod
    def _branding_key_for(sport_key: str) -> str:
        """Get the branding league key for a sport_key."""
        entry = LEAGUE_MAP.get(sport_key)
        return entry[2] if entry else sport_key

    # ── Team Enrichment ───────────────────────────────────────────────────────

    def _enrich_team(self, competitor: dict, league_key: str) -> dict:
        """Build a normalized team dict from an ESPN competitor object,
        enriched with TeamBranding data if available.
        """
        team_obj = competitor.get("team", {})
        abbr = team_obj.get("abbreviation", "")
        display_name = team_obj.get("displayName", "")
        nickname = team_obj.get("shortDisplayName", team_obj.get("name", ""))

        # Record — first record summary if available
        record = ""
        records = competitor.get("records", [])
        if records and isinstance(records, list):
            record = records[0].get("summary", "")

        result = {
            "espn_id": str(team_obj.get("id", "")),
            "name": nickname,
            "abbreviation": abbr,
            "display_name": display_name,
            "record": record,
            "color": None,
            "alternate_color": None,
            "logo_url": None,
            "logo_dark_url": None,
        }

        # Enrich from branding
        if self.branding:
            brand = self.branding.by_abbreviation(abbr, league_key)
            if not brand:
                brand = self.branding.by_nickname(nickname, league_key)
            if brand:
                result["color"] = brand.get("color")
                result["alternate_color"] = brand.get("alternate_color")
                result["logo_url"] = brand.get("logos", {}).get("default")
                result["logo_dark_url"] = brand.get("logos", {}).get("dark")

        # Fallback: ESPN logo URL pattern
        if not result["logo_url"] and abbr:
            paths = self._espn_paths(league_key)
            if paths:
                result["logo_url"] = (
                    f"https://a.espncdn.com/i/teamlogos/{paths[1]}/500/{abbr.lower()}.png"
                )

        return result

    # ── Scoreboard Parsing ────────────────────────────────────────────────────

    def _parse_competition(
        self, event: dict, comp: dict, sport_key: str, league_key: str, provider_id: int
    ) -> dict | None:
        """Parse a single competition into our normalized game schema."""
        # Status
        status_obj = comp.get("status", {}).get("type", {})
        status_name = status_obj.get("name", "STATUS_SCHEDULED")
        if status_name == "STATUS_FINAL":
            status = "final"
        elif status_name == "STATUS_IN_PROGRESS":
            status = "live"
        else:
            status = "scheduled"

        # Competitors
        home_comp = away_comp = None
        for c in comp.get("competitors", []):
            if c.get("homeAway") == "home":
                home_comp = c
            else:
                away_comp = c

        if not home_comp or not away_comp:
            return None

        home_team = self._enrich_team(home_comp, league_key)
        away_team = self._enrich_team(away_comp, league_key)

        # Scores
        home_score = _safe_int(home_comp.get("score"))
        away_score = _safe_int(away_comp.get("score"))

        # Odds — find the requested provider
        spread_data = {"home": None, "away": None, "home_odds": None, "away_odds": None,
                       "provider": "", "provider_id": provider_id}
        ml_data = {"home": None, "away": None}
        ou_data = {"total": None, "over_odds": None, "under_odds": None}
        wp_data = {"home": None, "away": None}

        odds_list = [o for o in comp.get("odds", []) if o is not None]
        target_odds = None

        # Try requested provider, fall back to first available
        for o in odds_list:
            pid = o.get("provider", {}).get("id")
            if pid is not None:
                pid = int(pid)
            if pid == provider_id:
                target_odds = o
                break
        if target_odds is None and odds_list:
            target_odds = odds_list[0]

        if target_odds:
            prov = target_odds.get("provider", {})
            spread_data["provider"] = prov.get("name", PROVIDERS.get(provider_id, "Unknown"))
            spread_data["provider_id"] = int(prov.get("id", provider_id))

            # ESPN scoreboard uses nested objects with STRING values:
            #   moneyline.home.close.odds = "-1650"
            #   pointSpread.home.close.line = "-18.5", .odds = "-110"
            #   total.over.close.line = "o233.5", .odds = "-105"

            # Spread (pointSpread object)
            ps = target_odds.get("pointSpread", {})
            ps_home = ps.get("home", {}).get("close", {})
            ps_away = ps.get("away", {}).get("close", {})
            spread_val = _safe_float(ps_home.get("line"))
            if spread_val is not None:
                spread_data["home"] = spread_val
                spread_data["away"] = -spread_val
            spread_data["home_odds"] = _safe_int(ps_home.get("odds"))
            spread_data["away_odds"] = _safe_int(ps_away.get("odds"))

            # Fallback: top-level "spread" field (sometimes present as a number)
            if spread_data["home"] is None:
                fallback_spread = _safe_float(target_odds.get("spread"))
                if fallback_spread is not None:
                    spread_data["home"] = fallback_spread
                    spread_data["away"] = -fallback_spread

            # Moneyline (moneyline object)
            ml = target_odds.get("moneyline", {})
            ml_data["home"] = _safe_int(ml.get("home", {}).get("close", {}).get("odds"))
            ml_data["away"] = _safe_int(ml.get("away", {}).get("close", {}).get("odds"))

            # Fallback: homeTeamOdds/awayTeamOdds (core API format)
            if ml_data["home"] is None:
                ml_data["home"] = _safe_int(target_odds.get("homeTeamOdds", {}).get("moneyLine"))
                ml_data["away"] = _safe_int(target_odds.get("awayTeamOdds", {}).get("moneyLine"))

            # Over/Under (total object)
            tot = target_odds.get("total", {})
            over_close = tot.get("over", {}).get("close", {})
            under_close = tot.get("under", {}).get("close", {})
            # Line comes as "o233.5" or "u233.5" — strip prefix
            total_line = over_close.get("line") or under_close.get("line")
            if total_line and isinstance(total_line, str):
                total_line = total_line.lstrip("oOuU")
            ou_data["total"] = _safe_float(total_line)
            ou_data["over_odds"] = _safe_int(over_close.get("odds"))
            ou_data["under_odds"] = _safe_int(under_close.get("odds"))

            # Fallback: top-level "overUnder" field
            if ou_data["total"] is None:
                ou_data["total"] = _safe_float(target_odds.get("overUnder"))

        # Win probabilities (sometimes inline in odds)
        for o in odds_list:
            # Try nested format first (scoreboard)
            home_wp = _safe_float(
                o.get("homeTeamOdds", {}).get("winPercentage")
                or o.get("winProbability", {}).get("home")
            )
            away_wp = _safe_float(
                o.get("awayTeamOdds", {}).get("winPercentage")
                or o.get("winProbability", {}).get("away")
            )
            if home_wp is not None:
                wp_data["home"] = home_wp / 100.0 if home_wp > 1 else home_wp
                wp_data["away"] = away_wp / 100.0 if away_wp and away_wp > 1 else away_wp
                break

        event_date = event.get("date", comp.get("date", ""))

        return {
            "event_id": str(comp.get("id", event.get("id", ""))),
            "event_name": event.get("name", f"{away_team['display_name']} at {home_team['display_name']}"),
            "event_date": event_date,
            "status": status,
            "league": league_key,
            "sport_key": sport_key,
            "home_team": home_team,
            "away_team": away_team,
            "home_score": home_score,
            "away_score": away_score,
            "spread": spread_data,
            "moneyline": ml_data,
            "over_under": ou_data,
            "win_probability": wp_data,
        }

    # ── Public API: Scoreboard ────────────────────────────────────────────────

    async def get_upcoming_odds(
        self, league: str = "NFL", provider_id: int = DEFAULT_PROVIDER,
    ) -> list[dict]:
        """Fetch upcoming games with odds for a league.

        Args:
            league: Branding league key ('NFL') or sport_key ('americanfootball_nfl').
            provider_id: ESPN betting provider ID (default: 1004 Consensus).

        Returns:
            List of normalized game dicts with odds, or empty list on failure.
        """
        paths = self._espn_paths(league)
        if not paths:
            return []

        sport_path, league_path = paths
        sport_key = self._sport_key_for(league) if league not in LEAGUE_MAP else league
        league_key = self._branding_key_for(sport_key)

        now = datetime.now(timezone.utc)
        results = []
        seen_ids: set[str] = set()

        for day_offset in range(self.ODDS_DAYS_AHEAD):
            date_str = (now + timedelta(days=day_offset)).strftime("%Y%m%d")
            url = f"{self.BASE_SITE}/{sport_path}/{league_path}/scoreboard?dates={date_str}"

            data = await self._fetch(url, cache=self._scoreboard_cache)
            if not data:
                continue

            for event in data.get("events", []):
                for comp in event.get("competitions", []):
                    comp_id = str(comp.get("id", event.get("id", "")))
                    if comp_id in seen_ids:
                        continue
                    seen_ids.add(comp_id)

                    game = self._parse_competition(event, comp, sport_key, league_key, provider_id)
                    if game and game["status"] != "final":
                        results.append(game)

        log.info(f"ESPN: fetched {len(results)} upcoming games for {league_key}")
        return results

    async def get_scores(
        self, league: str = "NFL", days_from: int = SCORES_DAYS_BACK,
    ) -> list[dict]:
        """Fetch completed game scores for a league.

        Returns list of normalized game dicts with final scores.
        """
        paths = self._espn_paths(league)
        if not paths:
            return []

        sport_path, league_path = paths
        sport_key = self._sport_key_for(league) if league not in LEAGUE_MAP else league
        league_key = self._branding_key_for(sport_key)

        now = datetime.now(timezone.utc)
        results = []
        seen_ids: set[str] = set()

        for day_offset in range(days_from + 1):
            date_str = (now - timedelta(days=day_offset)).strftime("%Y%m%d")
            url = f"{self.BASE_SITE}/{sport_path}/{league_path}/scoreboard?dates={date_str}"

            data = await self._fetch(url, cache=self._scoreboard_cache)
            if not data:
                continue

            for event in data.get("events", []):
                for comp in event.get("competitions", []):
                    comp_id = str(comp.get("id", event.get("id", "")))
                    if comp_id in seen_ids:
                        continue
                    seen_ids.add(comp_id)

                    game = self._parse_competition(
                        event, comp, sport_key, league_key, DEFAULT_PROVIDER
                    )
                    if game and game["status"] == "final":
                        results.append(game)

        return results

    async def get_all_upcoming_odds(
        self, provider_id: int = DEFAULT_PROVIDER,
    ) -> dict[str, list[dict]]:
        """Fetch upcoming odds for all supported leagues.

        Returns {sport_key: [games]}.
        """
        results = {}
        for sport_key in LEAGUE_MAP:
            games = await self.get_upcoming_odds(sport_key, provider_id)
            if games:
                results[sport_key] = games
        return results

    async def get_all_scores(
        self, days_from: int = SCORES_DAYS_BACK,
    ) -> dict[str, list[dict]]:
        """Fetch scores for all supported leagues.

        Returns {sport_key: [games]}.
        """
        results = {}
        for sport_key in LEAGUE_MAP:
            scores = await self.get_scores(sport_key, days_from)
            if scores:
                results[sport_key] = scores
        return results

    # ── Public API: Game-Specific (On-Demand) ─────────────────────────────────

    async def get_game_odds(self, event_id: str, league: str = "NFL") -> dict | None:
        """Fetch detailed odds for a single game from all providers."""
        paths = self._espn_paths(league)
        if not paths:
            return None

        sport_path, league_path = paths
        url = (
            f"{self.BASE_CORE}/{sport_path}/leagues/{league_path}"
            f"/events/{event_id}/competitions/{event_id}/odds"
        )

        data = await self._fetch(url, cache=self._odds_cache)
        if not data:
            return None

        # Parse all providers
        providers = {}
        items = data.get("items", data) if isinstance(data, dict) else data
        if isinstance(items, list):
            for item in items:
                prov = item.get("provider", {})
                pid = int(prov.get("id", 0))
                providers[pid] = {
                    "provider": prov.get("name", "Unknown"),
                    "provider_id": pid,
                    "spread": _safe_float(item.get("spread")),
                    "over_under": _safe_float(item.get("overUnder")),
                    "home_moneyline": _safe_int(item.get("homeTeamOdds", {}).get("moneyLine")),
                    "away_moneyline": _safe_int(item.get("awayTeamOdds", {}).get("moneyLine")),
                    "home_spread_odds": _safe_int(item.get("homeTeamOdds", {}).get("spreadOdds")),
                    "away_spread_odds": _safe_int(item.get("awayTeamOdds", {}).get("spreadOdds")),
                }

        return {"event_id": event_id, "providers": providers}

    async def get_live_probabilities(self, event_id: str, league: str = "NFL") -> dict | None:
        """Fetch live win probabilities for a game."""
        paths = self._espn_paths(league)
        if not paths:
            return None

        sport_path, league_path = paths
        url = (
            f"{self.BASE_CORE}/{sport_path}/leagues/{league_path}"
            f"/events/{event_id}/competitions/{event_id}/probabilities"
        )

        data = await self._fetch(url, cache=self._prob_cache)
        if not data:
            return None

        items = data.get("items", [])
        if items:
            latest = items[-1] if isinstance(items, list) else items
            return {
                "home": _safe_float(latest.get("homeWinPercentage")),
                "away": _safe_float(latest.get("awayWinPercentage")),
            }
        return None

    async def get_team_ats(
        self, team_id: str, league: str = "NFL",
        year: int = 2025, season_type: int = 2,
    ) -> dict | None:
        """Fetch a team's record against the spread."""
        paths = self._espn_paths(league)
        if not paths:
            return None

        sport_path, league_path = paths
        url = (
            f"{self.BASE_CORE}/{sport_path}/leagues/{league_path}"
            f"/seasons/{year}/types/{season_type}/teams/{team_id}/ats"
        )

        data = await self._fetch(url, cache=self._ats_cache)
        return data

    async def get_line_movement(
        self, event_id: str, league: str = "NFL",
        provider_id: int = DEFAULT_PROVIDER, limit: int = 100,
    ) -> list[dict]:
        """Fetch line movement history for a game."""
        paths = self._espn_paths(league)
        if not paths:
            return []

        sport_path, league_path = paths
        url = (
            f"{self.BASE_CORE}/{sport_path}/leagues/{league_path}"
            f"/events/{event_id}/competitions/{event_id}"
            f"/odds/{provider_id}/history/0/movement?limit={limit}"
        )

        data = await self._fetch(url)
        if not data:
            return []

        items = data.get("items", [])
        return [
            {
                "timestamp": item.get("timestamp", ""),
                "spread": _safe_float(item.get("spread")),
                "over_under": _safe_float(item.get("overUnder")),
            }
            for item in items
            if isinstance(item, dict)
        ]


# ── Utility Helpers ───────────────────────────────────────────────────────────

def _safe_int(val) -> int | None:
    """Safely convert a value to int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    """Safely convert a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
