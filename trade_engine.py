"""
trade_engine.py — ATLAS Trade Value Engine (2.7 Logic)

─────────────────────────────────────────────────────────────────────────────
Fixes applied (v2 — WittGPT Code Review rebuild):
  - BUG #2:  _contract_delta() now returns 0 when signable is False.
             Unsignable UFAs no longer get phantom cap% bonuses.
  - BUG #3:  Bare `except: pass` replaced with `except Exception as e: log.warning()`
             so real errors surface in logs instead of being silently swallowed.
  - BUG #4:  parity_state.json is now read ONCE before the cornerstone loop,
             not re-opened per player. `import json, os` moved to top of file.
  - BUG #5:  `import math` moved to top-level (was inside player_value).
  - FIX #11: _scarcity_multiplier() now calls dm.get_position_scarcity()
             which returns cached data (no more 1700-player scan per call).
  - FIX #14: Season-dependent constants extracted to config dict at top.
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
from dataclasses import dataclass, field
import json           # FIX #4: moved to top-level
import logging        # FIX #3: proper logging for caught exceptions
import math           # FIX #5: moved to top-level
import os             # FIX #4: moved to top-level
import pathlib
import data_manager as dm

_PARITY_STATE_PATH = pathlib.Path(__file__).parent / "parity_state.json"

log = logging.getLogger(__name__)

# ability_engine is optional — trade valuation works without it (meta bonus = 0)
try:
    import ability_engine as ae
    _ABILITY_TABLE = ae.ABILITY_TABLE
except (ImportError, AttributeError):
    ae = None
    _ABILITY_TABLE = {}

# ── FIX #14: Season-dependent config ─────────────────────────────────────────
# Change these once instead of hunting through ternaries everywhere.
SEASON_CONFIG = {
    "green_band_early":  12,    # delta% threshold for GREEN band (season <= 5)
    "green_band_late":    9,    # delta% threshold for GREEN band (season >= 6)
    "yellow_band_early": 20,    # delta% threshold for YELLOW band (season <= 5)
    "yellow_band_late":  20,    # delta% threshold for YELLOW band (season >= 6)
    "seasonal_mult_early": 1.15,  # pick EV multiplier (season <= 2)
    "seasonal_mult_late":  0.80,  # pick EV multiplier (season >= 7)
    "early_season_cutoff": 2,
    "late_season_cutoff":  7,
    "band_season_cutoff":  5,
}

OVR_TABLE: dict[int, int] = {
    99: 3150, 98: 2900, 97: 2650, 96: 2400, 95: 2200, 94: 2000, 93: 1800, 92: 1625,
    91: 1475, 90: 1350, 89: 1225, 88: 1100, 87: 1000, 86:  900, 85:  810, 84:  720,
    83:  640, 82:  565, 81:  495, 80:  430, 79:  370, 78:  315, 77:  265, 76:  220,
    75:  180,
}

def _base_ovr(ovr: int) -> int:
    for threshold in sorted(OVR_TABLE.keys(), reverse=True):
        if ovr >= threshold: return OVR_TABLE[threshold]
    return 100

POS_MULTIPLIER: dict[str, float] = {
    "QB": 1.40, "LEDGE": 1.20, "REDGE": 1.20, "CB": 1.18, "WR": 1.15, "DT": 1.15,
    "LT": 1.10, "RT": 1.08, "TE": 1.05, "MIKE": 1.05, "WILL": 1.02, "SAM": 1.00,
    "HB": 1.00, "FS": 1.00, "SS": 0.98, "LG": 0.95, "RG": 0.95, "C": 0.95, "FB": 0.80,
    "K": 0.50, "P": 0.50,
}

def _pos_multiplier(pos: str) -> float: return POS_MULTIPLIER.get(pos, 1.00)

AGE_MULTIPLIER: dict[int, float] = {
    20: 1.28, 21: 1.28, 22: 1.20, 23: 1.15, 24: 1.10, 25: 1.05, 26: 1.00, 27: 0.97,
    28: 0.94, 29: 0.90, 30: 0.85, 31: 0.80, 32: 0.75, 33: 0.70,
}

def _age_multiplier(age: int) -> float:
    if age <= 20: return 1.28
    if age >= 33: return 0.70
    return AGE_MULTIPLIER.get(age, 1.00)

def _regression_modifier(pos: str, age: int) -> float:
    if pos == "HB": return 0.90 if age >= 28 else (0.95 if age >= 26 else 1.00)
    if pos == "FB" and age >= 30: return 0.90
    return 1.00

def _flat_bonuses(age: int) -> int:
    bonus = 400 if age <= 21 else 0
    if age <= 24: bonus += min(max(0, 24 - age + 1) * 125, 500)
    return bonus

def _contract_delta(years_remaining: int, cap_pct: float, signable: bool) -> int:
    """
    FIX #2: Now respects the `signable` parameter.
    Unsignable UFAs (years_remaining == 0) get 0 contract value —
    cap% bonuses are meaningless for players who can't re-sign.
    """
    if not signable:
        return 0

    delta = 450 if years_remaining >= 4 else (200 if years_remaining >= 2 else 0)
    # High cap% = big contract = proven starter asset. The bonus reflects
    # that the player is a locked-in contributor, not a cap liability.
    # Low cap% also gets a small bonus (team-friendly deal = flexible roster).
    if cap_pct < 1.5: delta += 200
    if cap_pct >= 8.0: delta += 400
    elif cap_pct >= 6.0: delta += 250
    elif cap_pct >= 4.5: delta += 150
    return delta

def _ufa_penalty(player: dict, ovr: int, signable: bool) -> int:
    if signable: return 0
    base_penalty = 450 if ovr >= 88 else 300
    week = dm.CURRENT_WEEK if hasattr(dm, "CURRENT_WEEK") else 1
    return int(base_penalty * 1.25) if week >= 14 else base_penalty

ABILITY_TIER_POINTS: dict[str, int] = {"S": 350, "A": 200, "B": 125, "C": 0}

def _meta_cap_bonus(player: dict) -> int:
    ability_pts = sum(ABILITY_TIER_POINTS.get(_ABILITY_TABLE.get(player.get(f"ability{i}", ""), {}).get("tier", "C"), 0) for i in range(1, 7))
    ability_pts = min(ability_pts, 750)
    ovr = player.get("overallRating") or player.get("playerBestOvr") or 0
    attr_pts = 400 if ovr >= 97 else (300 if ovr >= 94 else (200 if ovr >= 91 else (100 if ovr >= 88 else 0)))
    return ability_pts + attr_pts

def _scarcity_multiplier(pos: str) -> float:
    """FIX #11: dm.get_position_scarcity() now returns cached data."""
    try:
        cls = dm.get_position_scarcity().get(pos, {}).get("scarcity_class", "Normal")
        return 1.07 if cls == "Scarce" else (0.95 if cls == "Saturated" else 1.00)
    except Exception as e:
        log.warning(f"[Trade] Scarcity lookup failed for {pos}: {e}")
        return 1.00

def _rings_tax_multiplier(team_id: int) -> float:
    """FIX #1: dm.get_rings_count() now returns cached data."""
    try:
        rings = dm.get_rings_count(team_id)
        return 1.08 if rings >= 2 else (1.04 if rings >= 1 else 1.00)
    except Exception as e:
        log.warning(f"[Trade] Rings lookup failed for team {team_id}: {e}")
        return 1.00

def _bundling_penalty(assets: list[dict], current_player: dict) -> float:
    pos = current_player.get("pos", "")
    count = len([a for a in assets if a.get("pos") == pos])
    return 0.92 if count == 1 else (0.88 if count >= 2 else 1.00)

@dataclass
class PlayerValueBreakdown:
    name: str; ovr: int; pos: str; age: int; base_ovr: int; pos_mult: float; age_mult: float
    regression_mod: float; flat_bonus: int; contract_delta: int; ufa_penalty: int; meta_bonus: int
    scarcity_mult: float; rings_mult: float; bundle_mult: float; final_value: int

    def summary_lines(self) -> list[str]:
        return [
            f"**{self.name}** ({self.pos}, OVR {self.ovr}, Age {self.age})",
            f"  Base OVR:        {self.base_ovr}",
            f"  × Pos Mult:      ×{self.pos_mult:.2f}",
            f"  × Age Mult:      ×{self.age_mult:.2f}",
            f"  × Regression:    ×{self.regression_mod:.2f}",
            f"  + Flat Bonus:    +{self.flat_bonus}",
            f"  + Contract:      {self.contract_delta:+d}",
            f"  − UFA Penalty:   -{self.ufa_penalty}",
            f"  + Meta Cap:      +{self.meta_bonus}",
            f"  × Scarcity:      ×{self.scarcity_mult:.2f}",
            f"  × Rings Tax:     ×{self.rings_mult:.2f}",
            f"  × Bundle:        ×{self.bundle_mult:.2f}",
            f"  ──────────────────────",
            f"  **TOTAL:         {self.final_value} pts**",
        ]

def player_value(player: dict, selling_team_id: int = 0, bundle_already_evaluated: list[dict] | None = None) -> PlayerValueBreakdown:
    bundle = bundle_already_evaluated or []

    # ── OVR: check both field names — export uses playerBestOvr ──────────────
    ovr_raw = player.get("overallRating") or player.get("playerBestOvr") or 75
    try:
        ovr = int(float(ovr_raw))
    except (ValueError, TypeError):
        ovr = 75

    pos  = player.get("pos", "HB")
    name = f"{player.get('firstName', '')} {player.get('lastName', '')}".strip()

    # ── Age: export may not have an age column — derive from yearsPro ─────────
    # FIX #5: math is now a top-level import
    age_raw = player.get("age")
    if age_raw is None or (isinstance(age_raw, float) and math.isnan(age_raw)):
        # Derive: base draft age 22 + seasons played
        try:
            rookie_yr    = int(float(player.get("rookieYear", dm.CURRENT_SEASON) or dm.CURRENT_SEASON))
            seasons_played = max(dm.CURRENT_SEASON - rookie_yr, 0)
            age = 22 + seasons_played
        except (ValueError, TypeError):
            age = 25
    else:
        try:
            f = float(age_raw)
            age = 25 if math.isnan(f) or math.isinf(f) else int(f)
        except (ValueError, TypeError):
            age = 25

    # FIX #3: bare except replaced with specific exception + logging
    try:
        contract = dm.get_contract_details(player.get("rosterId", 0))
        years_remaining = contract.get("years_remaining", 2)
        cap_pct = contract.get("cap_pct", 3.0)
        signable = contract.get("signable_flag", True)
    except Exception as e:
        log.warning(f"[Trade] Contract lookup failed for {name}: {e}")
        years_remaining = int(player.get("contractYearsLeft", 2) or 2)
        cap_pct = 3.0
        signable = True

    base = _base_ovr(ovr)
    pos_mult = _pos_multiplier(pos)
    age_mult = _age_multiplier(age)
    reg_mod = _regression_modifier(pos, age)
    core = base * pos_mult * age_mult * reg_mod
    flat = _flat_bonuses(age)
    contract_delta = _contract_delta(years_remaining, cap_pct, signable)
    ufa_pen = _ufa_penalty(player, ovr, signable)
    meta = _meta_cap_bonus(player)
    subtotal = core + flat + contract_delta - ufa_pen + meta
    scar_mult = _scarcity_multiplier(pos)
    # TODO (post-7.0): Use drafting team's rings, not selling team's.
    # Per CLAUDE.md, ring count should credit the team that originally drafted the player
    # (player_draft_map.drafting_team). Requires team_name → team_id lookup since
    # player_draft_map stores name strings and get_rings_count() takes an int.
    rings_mult = _rings_tax_multiplier(selling_team_id)
    market_adjusted = subtotal * scar_mult * rings_mult
    bundle_mult = _bundling_penalty(bundle, player)
    final = max(int(market_adjusted * bundle_mult), 50)

    return PlayerValueBreakdown(name, ovr, pos, age, base, pos_mult, age_mult, reg_mod, flat, contract_delta, ufa_pen, meta, scar_mult, rings_mult, bundle_mult, final)

PICK_BASE_VALUES: dict[int, int] = {1: 2200, 2: 900, 3: 500, 4: 300, 5: 175, 6: 100, 7: 60}

def pick_ev(round_: int, draft_year: int, team_id: int = 0, slot_in_round: int = 16) -> dict:
    current_season = dm.CURRENT_SEASON if hasattr(dm, "CURRENT_SEASON") else 1
    base = PICK_BASE_VALUES.get(round_, 60)
    slot_factor = 1.0 + (16 - slot_in_round) * 0.012
    primary = base * slot_factor
    ev = int((primary * 0.70) + (base * (slot_factor * 0.92) * 0.20) + (base * (slot_factor * 1.08) * 0.10))
    risk_haircut = int(ev * 0.05)
    ev -= risk_haircut
    years_out = max(0, draft_year - current_season)
    temporal_factor = {0: 1.00, 1: 0.75, 2: 0.50, 3: 0.25}.get(years_out, 0.20)
    ev = int(ev * temporal_factor)
    contender_discount = 0
    # FIX #3: bare except replaced with specific exception + logging
    try:
        if dm.get_team_record_dict(team_id).get("wins", 0) >= 10:
            contender_discount = int(ev * 0.10)
            ev -= contender_discount
    except Exception as e:
        log.warning(f"[Trade] Contender discount lookup failed for team {team_id}: {e}")

    # FIX #14: use SEASON_CONFIG instead of inline ternaries
    if current_season <= SEASON_CONFIG["early_season_cutoff"]:
        seasonal_mult = SEASON_CONFIG["seasonal_mult_early"]
    elif current_season >= SEASON_CONFIG["late_season_cutoff"]:
        seasonal_mult = SEASON_CONFIG["seasonal_mult_late"]
    else:
        seasonal_mult = 1.00
    ev = max(int(ev * seasonal_mult), 25)
    
    breakdown = [
        f"Round {round_} | Draft Year: Season {draft_year} (S{years_out} out)",
        f"  Base slot EV:      {base}",
        f"  - Risk haircut:    -{risk_haircut} (−5%)",
        f"  × Temporal:        ×{temporal_factor:.2f}",
        f"  - Contender disc.: -{contender_discount}",
        f"  × Seasonal mult:   ×{seasonal_mult:.2f}",
        f"  ───────────────────────",
        f"  **Final EV: {ev} pts**",
    ]
    return {"base_ev": base, "risk_haircut": risk_haircut, "temporal_factor": temporal_factor, "years_out": years_out, "contender_discount": contender_discount, "seasonal_mult": seasonal_mult, "final_ev": ev, "breakdown": breakdown}

@dataclass
class TradeSide:
    players: list[dict] = field(default_factory=list)
    picks: list[dict] = field(default_factory=list)
    team_id: int = 0

@dataclass
class TradeEvalResult:
    side_a_value: int; side_b_value: int; delta_pct: float; band: str; breakdown_a: list[str]; breakdown_b: list[str]; notes: list[str]

def evaluate_trade(side_a: TradeSide, side_b: TradeSide) -> TradeEvalResult:
    notes, breakdown_a, breakdown_b = [], [], []
    a_total, b_total = 0, 0
    evaluated_a, evaluated_b = [], []

    for p in side_a.players:
        vb = player_value(p, side_a.team_id, evaluated_a)
        a_total += vb.final_value
        breakdown_a.extend(vb.summary_lines() + [""])
        evaluated_a.append(p)

    for pk in side_a.picks:
        ev_data = pick_ev(pk.get("round", 1), pk.get("year", dm.CURRENT_SEASON), pk.get("team_id", side_a.team_id), pk.get("slot", 16))
        a_total += ev_data["final_ev"]
        breakdown_a.extend(ev_data["breakdown"] + [""])

    for p in side_b.players:
        vb = player_value(p, side_b.team_id, evaluated_b)
        b_total += vb.final_value
        breakdown_b.extend(vb.summary_lines() + [""])
        evaluated_b.append(p)

    for pk in side_b.picks:
        ev_data = pick_ev(pk.get("round", 1), pk.get("year", dm.CURRENT_SEASON), pk.get("team_id", side_b.team_id), pk.get("slot", 16))
        b_total += ev_data["final_ev"]
        breakdown_b.extend(ev_data["breakdown"] + [""])

    max_val = max(a_total, b_total, 1)
    delta_pct = abs(a_total - b_total) / max_val * 100
    
    # FIX #14: use SEASON_CONFIG for band thresholds
    season = dm.CURRENT_SEASON if hasattr(dm, "CURRENT_SEASON") else 1
    if season <= SEASON_CONFIG["band_season_cutoff"]:
        green_threshold  = SEASON_CONFIG["green_band_early"]
        yellow_threshold = SEASON_CONFIG["yellow_band_early"]
    else:
        green_threshold  = SEASON_CONFIG["green_band_late"]
        yellow_threshold = SEASON_CONFIG["yellow_band_late"]

    band = "GREEN" if delta_pct <= green_threshold else ("YELLOW" if delta_pct <= yellow_threshold else "RED")

    if band == "RED": notes.append("🚫 Trade exceeds 20% value gap — automatically declined by ATLAS.")
    elif band == "YELLOW": notes.append("⚠️ Trade is in YELLOW band — flagged for commissioner review.")

    # FIX #4: Read parity_state.json ONCE before the loop, not per player.
    # Also: json and os are now top-level imports.
    cornerstone_data: dict = {}
    try:
        if _PARITY_STATE_PATH.exists():
            with open(_PARITY_STATE_PATH) as f:
                cornerstone_data = json.load(f).get("cornerstones", {})
    except Exception as e:
        log.warning(f"[Trade] Failed to read parity_state.json: {e}")

    for p in side_a.players + side_b.players:
        # FIX #3: bare except replaced with specific exception + logging
        try:
            if not dm.get_contract_details(p.get("rosterId", 0)).get("signable_flag", True):
                notes.append(f"⚠️ {p.get('firstName','')} {p.get('lastName','')} is unsignable UFA.")
        except Exception as e:
            log.warning(f"[Trade] UFA check failed for rosterId {p.get('rosterId', '?')}: {e}")
        
        # FIX #4: Cornerstone check uses pre-loaded data, no file I/O in loop
        roster_id_str = str(p.get("rosterId", ""))
        if roster_id_str and roster_id_str in cornerstone_data:
            notes.append(f"🔒 **BLOCKED**: {p.get('firstName','')} {p.get('lastName','')} is designated as a Cornerstone and cannot be traded this season.")
            band = "RED"  # Force red if trying to trade cornerstone

    return TradeEvalResult(a_total, b_total, delta_pct, band, breakdown_a, breakdown_b, notes)
