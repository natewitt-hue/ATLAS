"""
odds_api_client.py — Async wrapper for TheRundown API
======================================================
Fetches live odds, scores, and sport metadata for the ATLAS real sportsbook.

Adapter layer: translates TheRundown responses into the same shape that
real_sportsbook_cog.py expects (originally designed for The Odds API v4).

Docs: https://therundown.io/api/v2
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp

log = logging.getLogger("odds_api_client")

API_KEY = os.getenv("THERUNDOWN_API_KEY", "")
BASE_URL = "https://therundown.io/api/v2"

# Sports we actively track (keys kept for backward compat with cog code)
SUPPORTED_SPORTS = {
    "americanfootball_nfl": "NFL",
    "basketball_nba":       "NBA",
    "baseball_mlb":         "MLB",
    "icehockey_nhl":        "NHL",
    "basketball_ncaab":     "NCAAB",
    "mma_ufc":              "UFC/MMA",
    "soccer_epl":           "EPL",
    "soccer_mls":           "MLS",
    "basketball_wnba":      "WNBA",
}

# Map our sport keys → TheRundown sport IDs
SPORT_ID_MAP = {
    "americanfootball_nfl": 2,
    "basketball_nba":       4,
    "baseball_mlb":         3,
    "icehockey_nhl":        6,
    "basketball_ncaab":     5,
    "mma_ufc":              7,
    "soccer_epl":           11,
    "soccer_mls":           10,
    "basketball_wnba":      8,
}

# DraftKings affiliate ID on TheRundown
BOOKMAKER_ID = "19"

# TheRundown market name → old API market key
MARKET_NAME_MAP = {
    "moneyline": "h2h",
    "handicap":  "spreads",
    "totals":    "totals",
}


class OddsAPIClient:
    """Async client wrapping TheRundown API v2, returning Odds-API-shaped dicts."""

    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._request_lock = asyncio.Lock()
        self._last_request_time: float = 0.0
        # Datapoint tracking from response headers
        self.requests_remaining: Optional[int] = None  # X-Datapoints-Remaining
        self.requests_used: Optional[int] = None       # consumed (limit - remaining)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-TheRundown-Key": API_KEY},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _track_datapoints(self, headers):
        """Update datapoint counters from TheRundown response headers."""
        remaining = headers.get("X-Datapoints-Remaining")
        limit = headers.get("X-Datapoints-Limit")
        if remaining is not None:
            try:
                self.requests_remaining = int(remaining)
            except ValueError:
                pass
        if limit is not None and remaining is not None:
            try:
                self.requests_used = int(limit) - int(remaining)
            except ValueError:
                pass
        if self.requests_remaining is not None:
            log.info(f"TheRundown datapoints: {self.requests_remaining} remaining, {self.requests_used} used")

    @property
    def emergency_mode(self) -> bool:
        """True if datapoints are running low (< 1000 remaining)."""
        return (self.requests_remaining is not None
                and self.requests_remaining < 1000)

    # Free plan: 1 req/sec. Use 1.1s to stay safely under.
    _REQUEST_INTERVAL = 1.1
    _MAX_RETRIES = 3

    async def _get(self, path: str) -> dict | list | None:
        """Make a GET request to TheRundown. Returns parsed JSON or None.

        Uses a lock to serialize all requests and enforce 1 req/sec across
        concurrent background tasks.
        """
        if not API_KEY:
            log.error("THERUNDOWN_API_KEY not set — cannot fetch odds.")
            return None

        session = await self._get_session()
        url = f"{BASE_URL}{path}"

        for attempt in range(1 + self._MAX_RETRIES):
            async with self._request_lock:
                # Throttle: wait if we called too recently
                loop = asyncio.get_event_loop()
                elapsed = loop.time() - self._last_request_time
                if elapsed < self._REQUEST_INTERVAL:
                    await asyncio.sleep(self._REQUEST_INTERVAL - elapsed)

                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                        self._last_request_time = loop.time()
                        self._track_datapoints(resp.headers)

                        if resp.status == 401:
                            log.error("TheRundown API: invalid API key (401).")
                            return None
                        if resp.status == 404:
                            log.warning(f"TheRundown API 404: {url}")
                            return None
                        if resp.status == 429:
                            # Check body to distinguish burst throttle vs daily/monthly cap
                            try:
                                err_body = await resp.json()
                            except Exception:
                                err_body = {}
                            err_msg = err_body.get("error", "")

                            if "limit reached" in err_msg.lower():
                                # Daily or monthly cap — retrying won't help
                                log.warning(f"TheRundown API: {err_msg}. No retries.")
                                return None

                            if attempt < self._MAX_RETRIES:
                                raw_retry = resp.headers.get("Retry-After")
                                wait = 3 * (2 ** attempt)  # default: 3s, 6s, 12s
                                if raw_retry:
                                    try:
                                        val = int(raw_retry)
                                        wait = max(1, val) if val <= 300 else wait
                                    except ValueError:
                                        pass
                                wait = min(wait, 30)
                                log.warning(f"TheRundown API: burst rate limited. Retrying in {wait}s...")
                                await asyncio.sleep(wait)
                                continue
                            log.warning("TheRundown API: rate limited (429). Max retries reached.")
                            return None
                        if resp.status != 200:
                            body = await resp.text()
                            log.warning(f"TheRundown API {resp.status}: {body[:200]}")
                            return None
                        return await resp.json()
                except aiohttp.ClientError as e:
                    log.error(f"TheRundown API request failed: {e}")
                    return None
                except Exception as e:
                    log.error(f"TheRundown API unexpected error: {e}")
                    return None
        return None

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_teams(event: dict) -> tuple[str, str]:
        """Extract (home_team, away_team) full names from a TheRundown event."""
        home = away = ""
        for team in event.get("teams", []):
            full_name = f"{team.get('name', '')} {team.get('mascot', '')}".strip()
            if team.get("is_home"):
                home = full_name
            else:
                away = full_name
        return home, away

    @staticmethod
    def _pick_main_line(lines: list[dict]) -> tuple[int, float | None] | None:
        """Pick the main FanDuel line from a participant's lines.

        Prefers is_main_line=True; falls back to line closest to -110.
        Returns (price, point) or None.
        """
        candidates = []
        for line in lines:
            fd = line.get("prices", {}).get(BOOKMAKER_ID)
            if not fd:
                continue
            price = fd.get("price")
            if price is None:
                continue
            point_val = line.get("value")
            point = float(point_val) if point_val else None
            if fd.get("is_main_line", False):
                return (price, point)
            candidates.append((price, point))
        if candidates:
            return min(candidates, key=lambda c: abs(abs(c[0]) - 110))
        return None

    @staticmethod
    def _transform_markets(event: dict) -> list[dict]:
        """Convert TheRundown markets → Odds API bookmakers[].markets[] shape.

        Extracts FanDuel (affiliate 23) prices only, picks the main line
        for spreads/totals.
        """
        transformed = []

        for market in event.get("markets", []):
            # Only full-game markets
            if market.get("period_id", 0) != 0:
                continue

            raw_name = market.get("name", "")
            mapped_key = MARKET_NAME_MAP.get(raw_name)
            if not mapped_key:
                continue

            participants = market.get("participants", [])

            # For totals, we need Over and Under to share the same point value.
            # Find the best shared point by scoring across both participants.
            if mapped_key == "totals" and len(participants) == 2:
                outcomes = OddsAPIClient._pick_totals(participants)
            else:
                outcomes = []
                for p in participants:
                    result = OddsAPIClient._pick_main_line(p.get("lines", []))
                    if result:
                        price, point = result
                        outcome = {"name": p.get("name", ""), "price": price}
                        if point is not None:
                            outcome["point"] = point
                        outcomes.append(outcome)

            if outcomes:
                transformed.append({"key": mapped_key, "outcomes": outcomes})

        return transformed

    @staticmethod
    def _pick_totals(participants: list[dict]) -> list[dict]:
        """Pick a shared main line for Over/Under totals.

        Finds the point value where both sides have FanDuel prices and
        the combined deviation from -110 is minimized.
        """
        # Build price lookup: {point: {participant_name: price}}
        by_point: dict[float, dict[str, int]] = {}
        main_found: dict[str, tuple[int, float]] = {}

        for p in participants:
            p_name = p.get("name", "")
            for line in p.get("lines", []):
                fd = line.get("prices", {}).get(BOOKMAKER_ID)
                if not fd:
                    continue
                price = fd.get("price")
                if price is None:
                    continue
                point_val = line.get("value")
                if not point_val:
                    continue
                point = float(point_val)

                if fd.get("is_main_line", False):
                    main_found[p_name] = (price, point)

                by_point.setdefault(point, {})[p_name] = price

        # If both sides have a main line at the same point, use it
        if len(main_found) == 2:
            names = list(main_found.keys())
            if main_found[names[0]][1] == main_found[names[1]][1]:
                point = main_found[names[0]][1]
                return [
                    {"name": n, "price": p, "point": point}
                    for n, (p, _) in main_found.items()
                ]

        # Fallback: find point with both sides, closest to -110
        p_names = [p.get("name", "") for p in participants]
        best_point = None
        best_score = float("inf")

        for point, prices in by_point.items():
            if len(prices) < 2:
                continue
            score = sum(abs(abs(prices[n]) - 110) for n in p_names if n in prices)
            if score < best_score:
                best_score = score
                best_point = point

        if best_point is not None:
            return [
                {"name": n, "price": by_point[best_point][n], "point": best_point}
                for n in p_names
                if n in by_point[best_point]
            ]

        return []

    # ── Public API ─────────────────────────────────────────────────────────

    async def fetch_sports(self) -> list[dict]:
        """Fetch all available sports from TheRundown."""
        data = await self._get("/sports")
        if isinstance(data, dict):
            return data.get("sports", [])
        return []

    async def fetch_odds(
        self,
        sport_key: str,
        markets: str = "h2h,spreads,totals",
        bookmakers: str = "draftkings",
    ) -> list[dict]:
        """Fetch upcoming events with odds for a sport.

        Returns list of events shaped like The Odds API v4 output.
        """
        sport_id = SPORT_ID_MAP.get(sport_key)
        if sport_id is None:
            log.warning(f"Unknown sport key: {sport_key}")
            return []

        now = datetime.now(timezone.utc)
        results = []
        seen_ids = set()

        # Fetch today + next 3 days to cover upcoming games
        for day_offset in range(4):
            date_str = (now + timedelta(days=day_offset)).strftime("%Y-%m-%d")
            data = await self._get(f"/sports/{sport_id}/events/{date_str}")
            if not data or not isinstance(data, dict):
                continue

            for event in data.get("events", []):
                event_id = event.get("event_id", "")
                if not event_id or event_id in seen_ids:
                    continue
                seen_ids.add(event_id)

                # Skip completed games
                score_data = event.get("score", {})
                status = score_data.get("event_status", "")
                if status == "STATUS_FINAL":
                    continue

                home, away = self._extract_teams(event)
                if not home or not away:
                    continue

                transformed_markets = self._transform_markets(event)
                if not transformed_markets:
                    continue

                results.append({
                    "id": event_id,
                    "sport_key": sport_key,
                    "sport_title": SUPPORTED_SPORTS.get(sport_key, sport_key),
                    "home_team": home,
                    "away_team": away,
                    "commence_time": event.get("event_date", ""),
                    "bookmakers": [{
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": transformed_markets,
                    }],
                })

        log.info(f"TheRundown: fetched {len(results)} upcoming events for {sport_key}")
        return results

    async def fetch_scores(
        self,
        sport_key: str,
        days_from: int = 3,
    ) -> list[dict]:
        """Fetch scores for recently completed events.

        Returns list shaped like The Odds API v4 scores output.
        """
        sport_id = SPORT_ID_MAP.get(sport_key)
        if sport_id is None:
            return []

        now = datetime.now(timezone.utc)
        results = []
        seen_ids = set()

        # Fetch today + previous days
        for day_offset in range(days_from + 1):
            date_str = (now - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            data = await self._get(f"/sports/{sport_id}/events/{date_str}")
            if not data or not isinstance(data, dict):
                continue

            for event in data.get("events", []):
                event_id = event.get("event_id", "")
                if not event_id or event_id in seen_ids:
                    continue
                seen_ids.add(event_id)

                score_data = event.get("score", {})
                status = score_data.get("event_status", "")
                completed = status == "STATUS_FINAL"

                home_score = score_data.get("score_home")
                away_score = score_data.get("score_away")

                # Only return events that have score data
                if home_score is None and away_score is None:
                    continue

                home, away = self._extract_teams(event)
                if not home or not away:
                    continue

                results.append({
                    "id": event_id,
                    "sport_key": sport_key,
                    "sport_title": SUPPORTED_SPORTS.get(sport_key, sport_key),
                    "home_team": home,
                    "away_team": away,
                    "commence_time": event.get("event_date", ""),
                    "completed": completed,
                    "scores": [
                        {"name": home, "score": str(home_score)},
                        {"name": away, "score": str(away_score)},
                    ],
                })

        return results

    async def fetch_all_odds(
        self,
        markets: str = "h2h,spreads,totals",
        bookmakers: str = "draftkings",
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
