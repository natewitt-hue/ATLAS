"""
odds_api_client.py — Thin async wrapper for The Odds API
=========================================================
Fetches live odds, scores, and sport metadata for the ATLAS real sportsbook.

Free tier: 500 requests/month. Budget targets ~224 req/month.
Emergency mode: if remaining < 50, skip odds refresh, only poll scores.

Docs: https://the-odds-api.com/liveapi/guides/v4/
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

log = logging.getLogger("odds_api_client")

API_KEY = os.getenv("ODDS_API_KEY", "")
BASE_URL = "https://api.the-odds-api.com/v4"

# Sports we actively track
SUPPORTED_SPORTS = {
    "americanfootball_nfl": "NFL",
    "basketball_nba": "NBA",
}

# Bookmakers to pull odds from (FanDuel is a good single source)
DEFAULT_BOOKMAKERS = "fanduel"

# Emergency mode threshold — stop fetching odds, only poll scores
EMERGENCY_THRESHOLD = 50


class OddsAPIClient:
    """Async client for The Odds API v4."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self.requests_remaining: Optional[int] = None
        self.requests_used: Optional[int] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _track_quota(self, headers: dict):
        """Update quota counters from response headers."""
        remaining = headers.get("x-requests-remaining")
        used = headers.get("x-requests-used")
        if remaining is not None:
            try:
                self.requests_remaining = int(remaining)
            except ValueError:
                pass
        if used is not None:
            try:
                self.requests_used = int(used)
            except ValueError:
                pass
        if self.requests_remaining is not None:
            log.info(f"Odds API quota: {self.requests_remaining} remaining, {self.requests_used} used")

    @property
    def emergency_mode(self) -> bool:
        """True if we should conserve API calls."""
        return (self.requests_remaining is not None
                and self.requests_remaining < EMERGENCY_THRESHOLD)

    async def _get(self, path: str, params: dict | None = None) -> list | dict | None:
        """Make a GET request to the API. Returns parsed JSON or None on error."""
        if not API_KEY:
            log.error("ODDS_API_KEY not set — cannot fetch odds.")
            return None

        session = await self._get_session()
        url = f"{BASE_URL}{path}"
        _params = {"apiKey": API_KEY}
        if params:
            _params.update(params)

        try:
            async with session.get(url, params=_params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                self._track_quota(resp.headers)
                if resp.status == 401:
                    log.error("Odds API: invalid API key (401).")
                    return None
                if resp.status == 429:
                    log.warning("Odds API: rate limited (429). Backing off.")
                    return None
                if resp.status != 200:
                    body = await resp.text()
                    log.warning(f"Odds API {resp.status}: {body[:200]}")
                    return None
                return await resp.json()
        except aiohttp.ClientError as e:
            log.error(f"Odds API request failed: {e}")
            return None
        except Exception as e:
            log.error(f"Odds API unexpected error: {e}")
            return None

    # ── Public API ───────────────────────────────────────────────────────

    async def fetch_sports(self) -> list[dict]:
        """Fetch all in-season sports. Free endpoint (doesn't count toward quota)."""
        data = await self._get("/sports")
        return data if isinstance(data, list) else []

    async def fetch_odds(
        self,
        sport_key: str,
        markets: str = "h2h,spreads,totals",
        bookmakers: str = DEFAULT_BOOKMAKERS,
    ) -> list[dict]:
        """
        Fetch upcoming events with odds for a sport.

        Returns list of events, each with bookmakers[].markets[].outcomes[].
        """
        if self.emergency_mode:
            log.warning(f"Emergency mode — skipping odds fetch for {sport_key}.")
            return []

        data = await self._get(f"/sports/{sport_key}/odds", {
            "regions": "us",
            "markets": markets,
            "bookmakers": bookmakers,
            "oddsFormat": "american",
        })
        return data if isinstance(data, list) else []

    async def fetch_scores(
        self,
        sport_key: str,
        days_from: int = 3,
    ) -> list[dict]:
        """
        Fetch scores for recently completed events.

        days_from: how many days back to look (1-3 recommended).
        """
        data = await self._get(f"/sports/{sport_key}/scores", {
            "daysFrom": str(days_from),
        })
        return data if isinstance(data, list) else []

    async def fetch_all_odds(
        self,
        markets: str = "h2h,spreads,totals",
        bookmakers: str = DEFAULT_BOOKMAKERS,
    ) -> dict[str, list[dict]]:
        """Fetch odds for all supported sports. Returns {sport_key: [events]}."""
        results = {}
        for sport_key in SUPPORTED_SPORTS:
            events = await self.fetch_odds(sport_key, markets, bookmakers)
            if events:
                results[sport_key] = events
        return results

    async def fetch_all_scores(self, days_from: int = 3) -> dict[str, list[dict]]:
        """Fetch scores for all supported sports. Returns {sport_key: [events]}."""
        results = {}
        for sport_key in SUPPORTED_SPORTS:
            scores = await self.fetch_scores(sport_key, days_from)
            if scores:
                results[sport_key] = scores
        return results
