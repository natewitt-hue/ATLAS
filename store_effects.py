"""
store_effects.py -- ATLAS Flow Store Effects Lookup API
========================================================
Lightweight standalone module. Other cogs import THIS, NOT flow_store.py.
All functions are synchronous (SQLite single-row lookups are <1ms).

Zero imports from flow_store.py — no circular dependencies.
========================================================
"""

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from flow_wallet import DB_PATH

_DB_TIMEOUT = 10
log = logging.getLogger(__name__)

# -- Category → effect_type mapping ------------------------------------------
_CATEGORY_MAP = {
    "casino": "casino_mult",
    "sportsbook": "sb_edge",
    "xp": "xp_mult",
}


def _db_con() -> sqlite3.Connection:
    """Match flow_sportsbook.py connection pattern."""
    con = sqlite3.connect(DB_PATH, timeout=_DB_TIMEOUT)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def _utcnow() -> str:
    """ISO-format UTC timestamp for SQL comparisons."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# =============================================================================
#  PUBLIC API
# =============================================================================


def get_active_effects(
    discord_id: int, effect_type: Optional[str] = None
) -> list[dict]:
    """Return all active, non-expired effects for a user.

    Inline expiration: any effects past ``expires_at`` are marked inactive
    in the same call and excluded from results.
    """
    with _db_con() as con:
        con.row_factory = sqlite3.Row

        if effect_type:
            rows = con.execute(
                "SELECT * FROM store_effects "
                "WHERE discord_id=? AND is_active=1 AND effect_type=?",
                (discord_id, effect_type),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM store_effects "
                "WHERE discord_id=? AND is_active=1",
                (discord_id,),
            ).fetchall()

        now = _utcnow()
        expired_ids: list[int] = []
        results: list[dict] = []

        for row in rows:
            if row["expires_at"] and row["expires_at"] < now:
                expired_ids.append(row["effect_id"])
                continue
            d = dict(row)
            try:
                d["effect_data"] = json.loads(d["effect_data"])
            except (json.JSONDecodeError, TypeError):
                d["effect_data"] = {}
            results.append(d)

        # Mark expired effects inline
        if expired_ids:
            placeholders = ",".join("?" for _ in expired_ids)
            con.execute(
                f"UPDATE store_effects SET is_active=0 "
                f"WHERE effect_id IN ({placeholders})",
                expired_ids,
            )

    return results


def get_multiplier(discord_id: int, category: str) -> float:
    """Get the active multiplier for a category.

    category: 'casino' | 'sportsbook' | 'xp'

    Returns 1.0 if no active boost. V1: no stacking — returns
    the single active value.
    """
    etype = _CATEGORY_MAP.get(category)
    if not etype:
        return 1.0

    effects = get_active_effects(discord_id, etype)
    if not effects:
        return 1.0

    eff = effects[0]
    data = eff["effect_data"]

    if category == "sportsbook":
        bonus_pct = data.get("bonus_pct", 0)
        return 1.0 + (bonus_pct / 100.0)

    # casino / xp — direct multiplier field
    return float(data.get("multiplier", 1.0))


def has_effect(discord_id: int, effect_type: str) -> bool:
    """Quick boolean check — is this effect type active for this user?"""
    return len(get_active_effects(discord_id, effect_type)) > 0


def consume_effect(discord_id: int, effect_type: str) -> bool:
    """Consume one use of a limited-use effect (reroll, insurance).

    Decrements ``uses`` in ``effect_data`` JSON. If uses hits 0,
    sets ``is_active=0``.

    Returns True if consumed, False if nothing to consume.
    """
    with _db_con() as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT effect_id, effect_data FROM store_effects "
            "WHERE discord_id=? AND effect_type=? AND is_active=1 "
            "ORDER BY started_at ASC LIMIT 1",
            (discord_id, effect_type),
        ).fetchone()

        if not row:
            return False

        try:
            data = json.loads(row["effect_data"])
        except (json.JSONDecodeError, TypeError):
            data = {}

        uses = data.get("uses")
        if uses is not None and uses > 0:
            data["uses"] = uses - 1
            if data["uses"] <= 0:
                con.execute(
                    "UPDATE store_effects SET is_active=0, effect_data=? "
                    "WHERE effect_id=?",
                    (json.dumps(data), row["effect_id"]),
                )
            else:
                con.execute(
                    "UPDATE store_effects SET effect_data=? WHERE effect_id=?",
                    (json.dumps(data), row["effect_id"]),
                )
        else:
            # No uses key — single-use, deactivate immediately
            con.execute(
                "UPDATE store_effects SET is_active=0 WHERE effect_id=?",
                (row["effect_id"],),
            )

    return True


def get_badges_and_flair(discord_id: int) -> dict:
    """Return all permanent display effects for profile rendering.

    Returns::

        {
            "badges":   [{"badge_name": ..., "badge_emoji": ..., ...}, ...],
            "trophies": [{"trophy_name": ..., "trophy_icon": ..., ...}, ...],
            "flair":    {"border_color": ..., "glow": ...} or None,
        }
    """
    badges = [
        e["effect_data"]
        for e in get_active_effects(discord_id, "badge")
    ]
    trophies = [
        e["effect_data"]
        for e in get_active_effects(discord_id, "trophy")
    ]
    flair_list = get_active_effects(discord_id, "profile_flair")
    flair = flair_list[0]["effect_data"] if flair_list else None

    return {"badges": badges, "trophies": trophies, "flair": flair}


def expire_stale_effects() -> int:
    """Bulk-expire all effects past their expires_at.

    Called by the store cog's periodic task.
    Returns count of effects expired.
    """
    now = _utcnow()
    with _db_con() as con:
        cur = con.execute(
            "UPDATE store_effects SET is_active=0 "
            "WHERE is_active=1 AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        return cur.rowcount
