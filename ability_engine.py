"""
ability_engine.py — TSL Ability Engine & Roster Governance Protocol
─────────────────────────────────────────────────────────────────────────────
Audits Madden NFL 26 rosters to ensure all player abilities are EARNED
through merit thresholds (The Lock & Key system).

Data sources: players.json + playerAbilities.json from MaddenStats API
Integration:  import ability_engine; results = ability_engine.run_audit(players, abilities)

Discord commands (registered in bot.py):
  /abilityaudit              — Full league audit, summary counts
  /abilityaudit team:<name> — Single team detailed report
  /abilitycheck player:<name> — Single player deep dive
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional
import re

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: DEV TRAIT BUDGET
# Normal dev players cannot have Superstar/XF abilities at all — they are
# excluded from the audit entirely (no budget, no flags).
# ─────────────────────────────────────────────────────────────────────────────

# Max abilities per tier that each dev trait may equip.
# Tiers: S > A > B > C  (C = common/stamina/recovery type abilities)
# XFactor gets 1S + 1A + 1B + unlimited C
# Superstar gets 0S + 1A + 1B + unlimited C
# Star gets 0S + 0A + 1B + unlimited C
DEV_BUDGET = {
    "Normal":             {"S": 0, "A": 0, "B": 0, "C": 99},
    "Star":               {"S": 0, "A": 0, "B": 1, "C": 99},
    "Superstar":          {"S": 0, "A": 1, "B": 1, "C": 99},
    "Superstar X-Factor": {"S": 1, "A": 1, "B": 1, "C": 99},
}

DEV_INT_TO_STR = {0: "Normal", 1: "Star", 2: "Superstar", 3: "Superstar X-Factor"}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: MASTER ABILITY TABLE
#
# Every ability that appears in the export is tiered here.
# Tier assignment logic:
#   S = Elite, position-defining, requires top-5% stats at the position
#   A = Strong, requires above-average stats (top 20-30%)
#   B = Solid, moderate threshold (above average, ~50th-70th pct)
#   C = Common/universal (recovery, stamina, generic traits) — no stat gate
#
# Threshold format: dict of {stat_field: min_value} — ALL must be met (AND logic)
#   EXCEPT fields ending in "__or" which use OR logic with the next __or field.
#   Physical floor fields are also included here under the same AND logic.
#
# Archetype field: if set, calculate_true_archetype() must return that value.
#   None = any archetype qualifies.
#
# Eligible positions: which pos values may legally equip this ability.
#   Empty list = any position (truly universal).
# ─────────────────────────────────────────────────────────────────────────────

ABILITY_TABLE: dict[str, dict] = {

    # ── QUARTERBACKS ──────────────────────────────────────────────────────────
    "Gunslinger":        {"tier":"S","positions":["QB"],"archetype":"Strong Arm",
                          "thresholds":{"throwPowerRating":96,"strengthRating":75}},
    "Bazooka":           {"tier":"S","positions":["QB"],"archetype":"Strong Arm",
                          "thresholds":{"throwPowerRating":97}},
    "Dots":              {"tier":"S","positions":["QB"],"archetype":"Field General",
                          "thresholds":{"throwAccDeepRating":95,"throwAccMidRating":95}},
    "Omaha":             {"tier":"S","positions":["QB"],"archetype":"Field General",
                          "thresholds":{"throwAccMidRating":96,"throwAccShortRating":96,"awareRating":92}},
    "Pass Lead Elite":   {"tier":"A","positions":["QB"],"archetype":None,
                          "thresholds":{"throwPowerRating":92}},
    "Fastbreak":         {"tier":"A","positions":["QB"],"archetype":"Scrambler",
                          "thresholds":{"speedRating":88,"accelRating":90}},
    "Escape Artist":     {"tier":"A","positions":["QB"],"archetype":"Scrambler",
                          "thresholds":{"speedRating":87,"agilityRating":80}},
    "Run & Gun":         {"tier":"A","positions":["QB"],"archetype":"Improviser",
                          "thresholds":{"throwOnRunRating":92,"speedRating":85}},
    "Inside Deadeye":    {"tier":"A","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccShortRating":93,"throwAccMidRating":92}},
    "No-Look Deadeye":   {"tier":"A","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccDeepRating":93,"throwPowerRating":90}},
    "Pocket Deadeye":    {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccMidRating":90,"throwAccShortRating":90}},
    "Dashing Deadeye":   {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwOnRunRating":88,"speedRating":80}},
    "Sideline Deadeye":  {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccDeepRating":88}},
    "Lofting Deadeye":   {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccDeepRating":85,"throwPowerRating":88}},
    "Red Zone Deadeye":  {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccShortRating":90,"throwAccMidRating":88}},
    "High Point Deadeye":{"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwPowerRating":90,"throwAccDeepRating":86}},
    "Roaming Deadeye":   {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccDeepRating":90,"throwAccMidRating":90}},
    "Long Range Deadeye":{"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwPowerRating":93}},
    "Conductor":         {"tier":"B","positions":["QB"],"archetype":"Field General",
                          "thresholds":{"throwAccMidRating":88,"throwAccShortRating":88,"awareRating":88}},
    "Homer":             {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccDeepRating":84,"throwAccMidRating":86}},
    "Comeback":          {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccMidRating":88,"awareRating":85}},
    "Closer":            {"tier":"B","positions":["QB","HB","MIKE"],"archetype":None,
                          "thresholds":{"awareRating":86}},
    "Clutch":            {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwUnderPressureRating":88}},
    "Gutsy Scrambler":   {"tier":"B","positions":["QB"],"archetype":"Scrambler",
                          "thresholds":{"speedRating":80,"throwOnRunRating":82}},
    "Gambit":            {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccDeepRating":88,"playActionRating":80}},
    "Quick Draw":        {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccShortRating":88,"throwAccMidRating":85}},
    "Safety Valve":      {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccShortRating":90,"awareRating":88}},
    "Sleight of Hand":   {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccShortRating":88,"throwOnRunRating":86}},
    "Set Feet Lead":     {"tier":"B","positions":["QB"],"archetype":"Field General",
                          "thresholds":{"throwAccDeepRating":86,"throwAccMidRating":88}},
    "Tight Out":         {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwAccShortRating":90}},
    "Bulldozer":         {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"strengthRating":70,"breakTackleRating":65}},
    "Blitz Radar":       {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"awareRating":90,"sensePressureTrait":1}},
    "Pro Reads":         {"tier":"B","positions":["QB"],"archetype":"Field General",
                          "thresholds":{"awareRating":93,"throwAccMidRating":88}},
    "Protected":         {"tier":"B","positions":["QB"],"archetype":None,
                          "thresholds":{"throwUnderPressureRating":92,"awareRating":88}},
    "Fearless":          {"tier":"C","positions":["QB"],"archetype":None,"thresholds":{}},
    "Anchored Extender": {"tier":"C","positions":["QB"],"archetype":None,"thresholds":{}},
    "Agile Extender":    {"tier":"C","positions":["QB"],"archetype":None,"thresholds":{}},
    "Brick Wall":        {"tier":"C","positions":["QB"],"archetype":None,"thresholds":{}},

    # ── WIDE RECEIVERS ────────────────────────────────────────────────────────
    "Route Technician":  {"tier":"S","positions":["WR","TE"],"archetype":"Route Runner",
                          "thresholds":{"routeRunShortRating":95,"routeRunMedRating":95,"agilityRating":92}},
    "Double Me":         {"tier":"S","positions":["WR","TE"],"archetype":None,
                          "thresholds":{"speedRating":95,"routeRunDeepRating":90}},
    "Deep Elite":        {"tier":"S","positions":["WR"],"archetype":"Deep Threat",
                          "thresholds":{"routeRunDeepRating":96,"speedRating":95}},
    "Mid Elite":         {"tier":"S","positions":["WR"],"archetype":"Route Runner",
                          "thresholds":{"routeRunMedRating":96,"routeRunShortRating":94}},
    "Deep Out Elite":    {"tier":"A","positions":["WR","TE"],"archetype":"Deep Threat",
                          "thresholds":{"routeRunDeepRating":92,"speedRating":94}},
    "Slot-O-Matic":      {"tier":"A","positions":["WR","TE"],"archetype":"Slot",
                          "thresholds":{"routeRunShortRating":90,"accelRating":92}},
    "Red Zone Threat":   {"tier":"A","positions":["WR","TE"],"archetype":None,
                          "thresholds":{"specCatchRating":90,"jumpRating":90}},
    "Short Elite":       {"tier":"A","positions":["WR"],"archetype":"Route Runner",
                          "thresholds":{"routeRunShortRating":92,"accelRating":90}},
    "Matchup Nightmare": {"tier":"A","positions":["WR","TE","HB"],"archetype":None,
                          "thresholds":{"speedRating":88,"routeRunShortRating":82}},
    "Short In Elite":    {"tier":"B","positions":["WR","TE"],"archetype":None,
                          "thresholds":{"routeRunShortRating":88}},
    "Short Out Elite":   {"tier":"B","positions":["WR","TE","HB"],"archetype":None,
                          "thresholds":{"routeRunShortRating":85,"accelRating":88}},
    "Mid In Elite":      {"tier":"B","positions":["WR","TE"],"archetype":None,
                          "thresholds":{"routeRunMedRating":85}},
    "Mid Out Elite":     {"tier":"B","positions":["WR","TE","FB"],"archetype":None,
                          "thresholds":{"routeRunMedRating":82}},
    "Deep In Elite":     {"tier":"B","positions":["WR","TE"],"archetype":None,
                          "thresholds":{"routeRunDeepRating":88}},
    "Deep In Zone KO":   {"tier":"B","positions":["FS","SS"],"archetype":None,
                          "thresholds":{"zoneCoverRating":88,"speedRating":88}},
    "Grab and Smash":    {"tier":"B","positions":["WR","TE"],"archetype":None,
                          "thresholds":{"specCatchRating":85,"strengthRating":65}},
    "Grab-N-Go":         {"tier":"B","positions":["WR","TE","HB"],"archetype":None,
                          "thresholds":{"catchRating":85,"speedRating":85}},
    "Runoff Elite":      {"tier":"B","positions":["WR"],"archetype":None,
                          "thresholds":{"speedRating":90,"accelRating":90}},
    "YAC 'Em Up":        {"tier":"B","positions":["WR","TE"],"archetype":None,
                          "thresholds":{"yACCatchTrait":1,"speedRating":87}},
    "RAC 'em Up":        {"tier":"B","positions":["WR","TE"],"archetype":None,
                          "thresholds":{"catchRating":86,"agilityRating":85}},
    "Wrecking Ball":     {"tier":"B","positions":["WR","TE","HB"],"archetype":None,
                          "thresholds":{"stiffArmRating":85,"strengthRating":65}},
    "Chuck Out":         {"tier":"B","positions":["WR","CB"],"archetype":None,
                          "thresholds":{"pressRating":80}},
    "Honorary Lineman":  {"tier":"B","positions":["WR"],"archetype":None,
                          "thresholds":{"strengthRating":70,"impactBlockRating":75}},
    "Slot Apprentice":   {"tier":"C","positions":["WR"],"archetype":None,"thresholds":{}},
    "Outside Apprentice":{"tier":"C","positions":["WR"],"archetype":None,"thresholds":{}},
    "Max Security":      {"tier":"C","positions":["WR","TE","HB"],"archetype":None,"thresholds":{}},
    "Reach For It":      {"tier":"C","positions":["WR","TE","HB","FB"],"archetype":None,"thresholds":{}},
    "Ironman":           {"tier":"C","positions":["WR","CB"],"archetype":None,"thresholds":{}},
    "Tireless Runner":   {"tier":"C","positions":["WR","TE","HB"],"archetype":None,"thresholds":{}},
    "Second Wind":       {"tier":"C","positions":["WR","TE","HB","FB"],"archetype":None,"thresholds":{}},
    "Return Man":        {"tier":"C","positions":["WR","HB","CB"],"archetype":None,"thresholds":{}},

    # ── TIGHT ENDS ────────────────────────────────────────────────────────────
    # (TE abilities use WR entries above; TE-unique below)
    "Backfield Mismatch":{"tier":"B","positions":["HB","FB"],"archetype":None,
                          "thresholds":{"catchRating":80,"routeRunShortRating":72}},

    # ── HALFBACKS ─────────────────────────────────────────────────────────────
    "Human Joystick":    {"tier":"S","positions":["HB"],"archetype":"Elusive",
                          "thresholds":{"agilityRating":95,"changeOfDirectionRating":93}},
    "Ankle Breaker":     {"tier":"A","positions":["HB","WR"],"archetype":None,
                          "thresholds":{"jukeMoveRating":90,"agilityRating":88}},
    "Goal Line Back":    {"tier":"A","positions":["HB"],"archetype":"Power Back",
                          "thresholds":{"breakTackleRating":90,"strengthRating":80}},
    "First One Free":    {"tier":"A","positions":["HB","WR","QB"],"archetype":None,
                          "thresholds":{"speedRating":92,"accelRating":92}},
    "Truzz":             {"tier":"A","positions":["HB","QB"],"archetype":None,
                          "thresholds":{"carryRating":92,"breakTackleRating":85}},
    "Freight Train":     {"tier":"A","positions":["HB","QB"],"archetype":None,
                          "thresholds":{"breakTackleRating":92,"strengthRating":78}},
    "Juke Box":          {"tier":"B","positions":["HB","QB","WR","TE"],"archetype":None,
                          "thresholds":{"jukeMoveRating":85,"agilityRating":82}},
    "Spin Cycle":        {"tier":"B","positions":["HB","QB","WR","TE"],"archetype":None,
                          "thresholds":{"spinMoveRating":85,"agilityRating":80}},
    "Balance Beam":      {"tier":"B","positions":["HB","TE"],"archetype":None,
                          "thresholds":{"breakTackleRating":82,"carryRating":82}},
    "Arm Bar":           {"tier":"B","positions":["HB","TE"],"archetype":None,
                          "thresholds":{"stiffArmRating":86,"strengthRating":68}},
    "Evasive":           {"tier":"B","positions":["HB","WR"],"archetype":None,
                          "thresholds":{"agilityRating":88,"changeOfDirectionRating":85}},
    "Bruiser":           {"tier":"B","positions":["HB","WR"],"archetype":None,
                          "thresholds":{"strengthRating":72,"stiffArmRating":80}},
    "Tank":              {"tier":"B","positions":["HB","TE"],"archetype":None,
                          "thresholds":{"breakTackleRating":85,"strengthRating":75}},
    "Leap Frog":         {"tier":"B","positions":["HB","TE"],"archetype":None,
                          "thresholds":{"jumpRating":88,"speedRating":86}},
    "Steamroller":       {"tier":"B","positions":["HB","WR","TE"],"archetype":None,
                          "thresholds":{"truckRating":86,"strengthRating":68}},
    "Backlash":          {"tier":"B","positions":["HB"],"archetype":None,
                          "thresholds":{"breakTackleRating":86,"carryRating":86}},
    "Playmaker":         {"tier":"B","positions":["HB"],"archetype":None,
                          "thresholds":{"agilityRating":86,"changeOfDirectionRating":84}},
    "Energizer":         {"tier":"B","positions":["HB","WR","FB"],"archetype":None,
                          "thresholds":{"speedRating":88,"accelRating":88}},
    "Satellite":         {"tier":"C","positions":["HB"],"archetype":None,"thresholds":{}},
    "All Day":           {"tier":"C","positions":["HB","LT"],"archetype":None,"thresholds":{}},

    # ── FULLBACKS ─────────────────────────────────────────────────────────────
    # FB abilities covered in HB/WR/TE sections above

    # ── OFFENSIVE LINE ────────────────────────────────────────────────────────
    "Nasty Streak":      {"tier":"A","positions":["LT","LG","C","RG","RT","FB"],"archetype":None,
                          "thresholds":{"runBlockRating":90,"strengthRating":88}},
    "Puller Elite":      {"tier":"A","positions":["LT","LG","RG","RT"],"archetype":None,
                          "thresholds":{"runBlockRating":92,"agilityRating":68}},
    "Post Up":           {"tier":"A","positions":["LT","LG","RG","RT"],"archetype":None,
                          "thresholds":{"passBlockRating":90,"strengthRating":88}},
    "Omniscient":        {"tier":"A","positions":["LT","LG","RG","RT"],"archetype":None,
                          "thresholds":{"passBlockRating":88,"awareRating":85}},
    "Fool Me Once":      {"tier":"B","positions":["LT","LG","C","RG","RT"],"archetype":None,
                          "thresholds":{"passBlockRating":85,"awareRating":80}},
    "Matador":           {"tier":"B","positions":["LT","LG","RG"],"archetype":None,
                          "thresholds":{"runBlockRating":85,"agilityRating":65}},
    "Run Protector":     {"tier":"B","positions":["LT","LG","C","RG","RT","TE"],"archetype":None,
                          "thresholds":{"runBlockRating":82,"strengthRating":82}},
    "Natural Talent":    {"tier":"B","positions":["LT","LG","C","RT"],"archetype":None,
                          "thresholds":{"passBlockRating":80}},
    "Threat Detector":   {"tier":"B","positions":["LT","LG","C","RG","RT"],"archetype":None,
                          "thresholds":{"awareRating":83,"passBlockRating":80}},
    "Unspun":            {"tier":"B","positions":["LT","LG","RG","RT"],"archetype":None,
                          "thresholds":{"passBlockRating":82,"strengthRating":82}},
    "Linchpin":          {"tier":"B","positions":["C"],"archetype":None,
                          "thresholds":{"passBlockRating":88,"awareRating":85}},
    "Pass Protector":    {"tier":"B","positions":["LT","LG","TE"],"archetype":None,
                          "thresholds":{"passBlockRating":80}},
    "Screen Protector":  {"tier":"B","positions":["LT","LG","RG","RT","FB"],"archetype":None,
                          "thresholds":{"runBlockRating":78}},
    "Secure Protector":  {"tier":"B","positions":["LT","LG","RG","FB"],"archetype":None,
                          "thresholds":{"passBlockRating":78,"strengthRating":80}},
    "Tear Proof":        {"tier":"C","positions":["LT","LG","C","RG","RT"],"archetype":None,"thresholds":{}},
    "Edge Protector":    {"tier":"C","positions":["LT","RT"],"archetype":None,"thresholds":{}},
    "Lifeguard":         {"tier":"C","positions":["LT","RG","RT"],"archetype":None,"thresholds":{}},
    "Tough Nut":         {"tier":"C","positions":["LT","RT"],"archetype":None,"thresholds":{}},

    # ── EDGE RUSHERS (LEDGE / REDGE) ──────────────────────────────────────────
    "Edge Threat Elite": {"tier":"S","positions":["LEDGE","REDGE"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":94,"speedRating":88,"accelRating":90}},
    "Unstoppable Force": {"tier":"S","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":96,"strengthRating":88}},
    "Edge Threat":       {"tier":"A","positions":["LEDGE","REDGE"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":88,"speedRating":84}},
    "El Toro":           {"tier":"A","positions":["LEDGE","REDGE","DT"],"archetype":"Power Rusher",
                          "thresholds":{"powerMovesRating":90,"strengthRating":88}},
    "Fearmonger":        {"tier":"A","positions":["LEDGE","REDGE","DT"],"archetype":"Speed Rusher",
                          "thresholds":{"finesseMovesRating":90,"speedRating":85}},
    "Blitz":             {"tier":"A","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":90,"accelRating":88}},
    "Double Or Nothing": {"tier":"A","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"finesseMovesRating":88,"powerMovesRating":85}},
    "Relentless":        {"tier":"A","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":88,"staminaRating":90}},
    "Pass Committed":    {"tier":"A","positions":["LEDGE","REDGE"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":86,"awareRating":80}},
    "Ripper":            {"tier":"B","positions":["LEDGE","REDGE"],"archetype":"Finesse Rusher",
                          "thresholds":{"finesseMovesRating":88,"accelRating":88}},
    "Swim Club":         {"tier":"B","positions":["LEDGE","REDGE","DT"],"archetype":"Finesse Rusher",
                          "thresholds":{"finesseMovesRating":85}},
    "Spinner":           {"tier":"B","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"finesseMovesRating":82,"agilityRating":75}},
    "Unpredictable":     {"tier":"B","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"blockShedRating":85}},
    "B.O.G.O.":          {"tier":"B","positions":["LEDGE","REDGE","DT","MIKE","SAM","WILL"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":80}},
    "Instant Rebate":    {"tier":"B","positions":["LEDGE","REDGE"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":82,"accelRating":86}},
    "Goal Line Stuff":   {"tier":"B","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"strengthRating":88,"blockShedRating":82}},
    "Mr. Big Stop":      {"tier":"B","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"blockShedRating":85,"tackleRating":80}},
    "Run Committed":     {"tier":"B","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"blockShedRating":82,"strengthRating":85}},
    "Extra Credit":      {"tier":"B","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"blockShedRating":80}},
    "Max Effort":        {"tier":"B","positions":["LEDGE","REDGE","DT"],"archetype":None,
                          "thresholds":{"_edge_pmv_or_fmv":80,"staminaRating":88}},
    "Run Stopper":       {"tier":"B","positions":["LEDGE","REDGE","DT","MIKE","WILL"],"archetype":None,
                          "thresholds":{"blockShedRating":82,"tackleRating":78}},
    "Demoralizer":       {"tier":"C","positions":["LEDGE","REDGE","DT","MIKE","SAM","WILL"],"archetype":None,"thresholds":{}},
    "Defensive Rally":   {"tier":"C","positions":["LEDGE","REDGE","DT","FS","MIKE"],"archetype":None,"thresholds":{}},
    "Adrenaline Rush":   {"tier":"C","positions":["LEDGE","REDGE","DT","MIKE","SAM","WILL"],"archetype":None,"thresholds":{}},
    "Momentum Shift":    {"tier":"C","positions":["LEDGE","REDGE","DT","FS","MIKE"],"archetype":None,"thresholds":{}},
    "Secure Tackler":    {"tier":"C","positions":["LEDGE","REDGE","DT","FS","MIKE","SS"],"archetype":None,"thresholds":{}},

    # ── DEFENSIVE TACKLES ─────────────────────────────────────────────────────
    "Interior Threat":   {"tier":"S","positions":["DT"],"archetype":None,
                          "thresholds":{"blockShedRating":95,"powerMovesRating":88}},
    "Inside Stuff":      {"tier":"A","positions":["DT","LEDGE","MIKE"],"archetype":"Power/Run Stopper",
                          "thresholds":{"blockShedRating":92,"strengthRating":90}},
    "Enforcer Supreme":  {"tier":"A","positions":["DT"],"archetype":None,
                          "thresholds":{"hitPowerRating":92,"strengthRating":92,"weight":280}},
    "Run Stuffer":       {"tier":"B","positions":["DT","MIKE","REDGE","SAM","WILL"],"archetype":None,
                          "thresholds":{"blockShedRating":82,"strengthRating":85}},
    "Stonewall":         {"tier":"B","positions":["DT","MIKE","REDGE","SAM","SS","WILL"],"archetype":None,
                          "thresholds":{"tackleRating":85,"strengthRating":82}},
    "Reinforcement":     {"tier":"C","positions":["LEDGE","REDGE","DT","CB","FS","MIKE","SS","WILL","WR"],"archetype":None,"thresholds":{}},
    "Under Pressure":    {"tier":"C","positions":["LEDGE","REDGE","DT","MIKE","WILL"],"archetype":None,"thresholds":{}},

    # ── LINEBACKERS ───────────────────────────────────────────────────────────
    "Lurk Artist":       {"tier":"A","positions":["MIKE","SAM","WILL"],"archetype":None,
                          "thresholds":{"changeOfDirectionRating":80,"speedRating":85}},
    "Mind Reader":       {"tier":"A","positions":["MIKE"],"archetype":None,
                          "thresholds":{"awareRating":92,"zoneCoverRating":82}},
    "Avalanche":         {"tier":"A","positions":["MIKE","WILL"],"archetype":None,
                          "thresholds":{"tackleRating":88,"strengthRating":82}},
    "Crusher":           {"tier":"A","positions":["MIKE"],"archetype":None,
                          "thresholds":{"tackleRating":90,"hitPowerRating":88}},
    "Extra Pop":         {"tier":"B","positions":["MIKE","SAM","SS","WILL","FS"],"archetype":None,
                          "thresholds":{"hitPowerRating":85,"tackleRating":82}},
    "Outmatched":        {"tier":"B","positions":["MIKE","WILL","FS","LEDGE"],"archetype":None,
                          "thresholds":{"manCoverRating":78,"speedRating":82}},
    "Deflator":          {"tier":"B","positions":["MIKE","WILL","FS","SS"],"archetype":None,
                          "thresholds":{"hitPowerRating":83,"tackleRating":82}},
    "Lumberjack":        {"tier":"B","positions":["MIKE","SAM","FS","SS"],"archetype":None,
                          "thresholds":{"blockShedRating":78,"tackleRating":82}},
    "Tackle Supreme":    {"tier":"B","positions":["MIKE","WILL","REDGE"],"archetype":None,
                          "thresholds":{"tackleRating":88,"hitPowerRating":82}},
    "Form Tackler":      {"tier":"B","positions":["MIKE","SAM","WILL","FS","SS"],"archetype":None,
                          "thresholds":{"tackleRating":85}},
    "Out My Way":        {"tier":"C","positions":["MIKE","SAM","WILL","SS","LEDGE"],"archetype":None,"thresholds":{}},
    "Selfless":          {"tier":"C","positions":["MIKE","SAM","WILL","CB","FS","SS"],"archetype":None,"thresholds":{}},
    "Persistent":        {"tier":"C","positions":[],"archetype":None,"thresholds":{}},  # truly universal

    # ── CORNERBACKS ───────────────────────────────────────────────────────────
    "Shutdown":          {"tier":"S","positions":["CB","FS","MIKE","SS","WILL"],"archetype":None,
                          "thresholds":{"manCoverRating":94,"speedRating":90}},
    "One Step Ahead":    {"tier":"S","positions":["CB"],"archetype":"Man Coverage",
                          "thresholds":{"manCoverRating":95,"accelRating":92}},
    "Universal Coverage":{"tier":"A","positions":["CB","FS","SS"],"archetype":None,
                          "thresholds":{"manCoverRating":88,"zoneCoverRating":88,"speedRating":90}},
    "Deep Route KO":     {"tier":"A","positions":["CB"],"archetype":None,
                          "thresholds":{"zoneCoverRating":90,"speedRating":90}},
    "Deep Out Zone KO":  {"tier":"A","positions":["CB","FS"],"archetype":None,
                          "thresholds":{"zoneCoverRating":90,"speedRating":92}},
    "Zone Hawk":         {"tier":"A","positions":["CB","FS","MIKE","SAM","SS","WILL"],"archetype":None,
                          "thresholds":{"zoneCoverRating":88,"awareRating":85}},
    "Acrobat":           {"tier":"A","positions":["CB","FS","SS","WR"],"archetype":None,
                          "thresholds":{"jumpRating":90,"speedRating":92}},
    "On The Ball":       {"tier":"A","positions":["CB","SS"],"archetype":None,
                          "thresholds":{"manCoverRating":88,"pressRating":82}},
    "Bottleneck":        {"tier":"A","positions":["CB"],"archetype":None,
                          "thresholds":{"pressRating":88,"manCoverRating":85}},
    "Pick Artist":       {"tier":"B","positions":["CB","FS"],"archetype":None,
                          "thresholds":{"catchRating":80,"manCoverRating":80}},
    "Flat Zone KO":      {"tier":"B","positions":["CB","FS","MIKE","SS","WILL"],"archetype":None,
                          "thresholds":{"zoneCoverRating":88,"accelRating":88}},
    "Medium Route KO":   {"tier":"B","positions":["CB","FS","MIKE","SS","WILL"],"archetype":None,
                          "thresholds":{"zoneCoverRating":88,"manCoverRating":82}},
    "Short Route KO":    {"tier":"B","positions":["CB","FS","MIKE","SS","WILL"],"archetype":None,
                          "thresholds":{"manCoverRating":82,"accelRating":88}},
    "Mid Zone KO":       {"tier":"B","positions":["CB","FS","SS","MIKE","LEDGE","REDGE"],"archetype":None,
                          "thresholds":{"zoneCoverRating":82}},
    "Inside Shade":      {"tier":"B","positions":["CB"],"archetype":None,
                          "thresholds":{"manCoverRating":85,"pressRating":78}},
    "Outside Shade":     {"tier":"B","positions":["CB","SS"],"archetype":None,
                          "thresholds":{"manCoverRating":82,"speedRating":86}},
    "Tip Drill":         {"tier":"B","positions":["CB","FS","MIKE","SS"],"archetype":None,
                          "thresholds":{"playRecRating":82,"jumpRating":82}},
    "Bench Press":       {"tier":"B","positions":["CB","FS","SS"],"archetype":None,
                          "thresholds":{"pressRating":82,"strengthRating":68}},
    "Film Study":        {"tier":"B","positions":["SS","FS"],"archetype":None,
                          "thresholds":{"awareRating":88,"zoneCoverRating":82}},
    "No Outsiders":      {"tier":"B","positions":["CB","FS","MIKE","LEDGE","REDGE","SS"],"archetype":None,
                          "thresholds":{"awareRating":82}},
    "Enforcer":          {"tier":"B","positions":["FS","SS"],"archetype":"Run Support",
                          "thresholds":{"hitPowerRating":88,"weight":210}},
    "Strip Specialist":  {"tier":"C","positions":[],"archetype":None,"thresholds":{}},  # universal
    "Unfakeable":        {"tier":"C","positions":[],"archetype":None,"thresholds":{}},  # universal
    "Recuperation":      {"tier":"C","positions":[],"archetype":None,"thresholds":{}},  # universal

    # ── KICKERS / PUNTERS ─────────────────────────────────────────────────────
    "Zen Kicker":        {"tier":"S","positions":["K","P"],"archetype":None,
                          "thresholds":{"kickPowerRating":96,"kickAccRating":90}},
    "Precision Kicker":  {"tier":"A","positions":["K","P"],"archetype":None,
                          "thresholds":{"kickAccRating":90,"kickPowerRating":88}},
    "Clutch Kicker":     {"tier":"A","positions":["K","P"],"archetype":None,
                          "thresholds":{"kickAccRating":92}},
    "Focused Kicker":    {"tier":"B","positions":["K","P"],"archetype":None,
                          "thresholds":{"kickAccRating":85}},
}

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: HELPER FUNCTIONS
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_dev(player: dict) -> str:
    """Return canonical dev string regardless of which field/format is present."""
    dev = player.get("dev", "")
    if dev in DEV_BUDGET:
        return dev
    dev_int = player.get("devTrait", 0)
    return DEV_INT_TO_STR.get(int(dev_int), "Normal")


def calculate_true_archetype(player: dict) -> str:
    """
    Determine a player's true archetype by comparing arch rating fields.
    Uses the game's own pre-computed archetype ratings rather than raw stats
    so the result is consistent with what Madden itself uses.
    Returns a canonical archetype string or 'Unknown'.
    """
    pos = player.get("pos", "")

    if pos == "QB":
        arch_scores = {
            "Strong Arm":   player.get("strongArmQBArchRating", 0),
            "Field General":player.get("fieldGeneralQBArchRating", 0),
            "Scrambler":    player.get("scramblerQBArchRating", 0),
            "Improviser":   player.get("improviserQBArchRating", 0),
        }
    elif pos == "WR":
        arch_scores = {
            "Deep Threat":  player.get("deepThreatWRArchRating", 0),
            "Slot":         player.get("slotWRArchRating", 0),
            "Physical":     player.get("physicalWRArchRating", 0),
            "Route Runner": player.get("routeRunnerWRArchRating", 0),
        }
    elif pos == "HB":
        arch_scores = {
            "Elusive":      player.get("elusiveBackHBArchRating", 0),
            "Power Back":   player.get("powerBackHBArchRating", 0),
            "Receiving":    player.get("receivingBackHBArchRating", 0),
        }
    elif pos in ("LEDGE", "REDGE"):
        arch_scores = {
            "Speed Rusher": player.get("speedRusherDLArchRating", 0),
            "Power Rusher": player.get("powerRusherDLArchRating", 0),
            "Run Stopper":  player.get("runStopperDLArchRating", 0),
        }
    elif pos == "DT":
        arch_scores = {
            "Speed Rusher": player.get("speedRusherDLArchRating", 0),
            "Power Rusher": player.get("powerRusherDLArchRating", 0),
            "Run Stopper":  player.get("runStopperDLArchRating", 0),
        }
    elif pos == "CB":
        arch_scores = {
            "Man Coverage": player.get("mantoManCBArchRating", 0),
            "Zone":         player.get("zoneCBArchRating", 0),
            "Slot":         player.get("slotCBArchRating", 0),
        }
    elif pos in ("FS", "SS"):
        arch_scores = {
            "Hybrid":       player.get("hybridSArchRating", 0),
            "Zone":         player.get("zoneSArchRating", 0),
            "Run Support":  player.get("runSupportSArchRating", 0),
        }
    elif pos in ("MIKE", "SAM", "WILL"):
        arch_scores = {
            "Field General":player.get("fieldGeneralLBArchRating", 0),
            "Pass Coverage":player.get("passCoverageLBArchRating", 0),
            "Run Stopper":  player.get("runStopperLBArchRating", 0),
            "Speed Rusher": player.get("speedRusherLBArchRating", 0),
            "Power Rusher": player.get("powerRusherLBArchRating", 0),
        }
    elif pos in ("LT", "LG", "C", "RG", "RT"):
        arch_scores = {
            "Pass Protector":player.get("passProtectorOLArchRating", 0),
            "Power":         player.get("powerOLArchRating", 0),
            "Agile":         player.get("agileOLArchRating", 0),
        }
    elif pos == "K":
        arch_scores = {
            "Power":    player.get("powerKArchRating", 0),
            "Accurate": player.get("accurateKArchRating", 0),
        }
    elif pos == "P":
        arch_scores = {
            "Power":    player.get("powerPArchRating", 0),
            "Accurate": player.get("accuratePArchRating", 0),
        }
    else:
        return "Unknown"

    return max(arch_scores, key=arch_scores.get) if arch_scores else "Unknown"


def check_physics_floor(player: dict, ability_name: str) -> tuple[bool, str]:
    """
    Check all thresholds for an ability against a player's stats.
    Special keys:
      _edge_pmv_or_fmv: passes if EITHER powerMovesRating OR finesseMovesRating >= value
      weight: checks player['weight'] directly (lbs)
    Returns (passes: bool, reason: str)
    """
    entry = ABILITY_TABLE.get(ability_name)
    if not entry:
        return True, ""  # unknown ability = don't flag (can't audit what we don't know)

    thresholds = entry.get("thresholds", {})
    for key, min_val in thresholds.items():
        if key == "_edge_pmv_or_fmv":
            pmv = player.get("powerMovesRating", 0)
            fmv = player.get("finesseMovesRating", 0)
            # Use the higher of the two (players specialize in one; finesse most common)
            best = max(pmv, fmv)
            if best < min_val:
                return False, f"PMV({pmv}) or FMV({fmv}) must be ≥{min_val} (best={best})"
        elif key == "weight":
            actual = player.get("weight", 0)
            if actual < min_val:
                return False, f"weight {actual}lbs < {min_val}lbs floor"
        else:
            actual = int(player.get(key, 0) or 0)
            if actual < min_val:
                stat_label = key.replace("Rating", "").replace("Trait", "★")
                return False, f"{stat_label}={actual} < {min_val}"
    return True, ""


def _archetype_ok(player: dict, ability_name: str) -> tuple[bool, str]:
    """Check archetype requirement if any."""
    entry = ABILITY_TABLE.get(ability_name)
    if not entry:
        return True, ""
    required = entry.get("archetype")
    if required is None:
        return True, ""
    # Some abilities accept multiple archetypes (e.g., "Power/Run Stopper")
    required_options = [r.strip() for r in required.split("/")]
    true_arch = calculate_true_archetype(player)
    if true_arch in required_options:
        return True, ""
    return False, f"archetype={true_arch} (needs {required})"


def _position_ok(player: dict, ability_name: str) -> bool:
    """Return True if player's position is eligible for this ability."""
    entry = ABILITY_TABLE.get(ability_name)
    if not entry:
        return True
    eligible = entry.get("positions", [])
    if not eligible:
        return True  # universal
    return player.get("pos", "") in eligible


def is_ability_earned(player: dict, ability_name: str) -> tuple[bool, list[str]]:
    """
    Master gate: returns (earned: bool, reasons: list[str])
    An ability is earned only if ALL three checks pass:
      1. Player's position is eligible
      2. Stat/physics thresholds met
      3. Archetype requirement met (if any)
    """
    reasons = []

    if not _position_ok(player, ability_name):
        pos = player.get("pos","?")
        entry = ABILITY_TABLE.get(ability_name, {})
        eligible = entry.get("positions", [])
        reasons.append(f"position {pos} ineligible (allowed: {', '.join(eligible) if eligible else 'any'})")

    passes_stats, stat_reason = check_physics_floor(player, ability_name)
    if not passes_stats:
        reasons.append(stat_reason)

    passes_arch, arch_reason = _archetype_ok(player, ability_name)
    if not passes_arch:
        reasons.append(arch_reason)

    return (len(reasons) == 0), reasons


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: BUDGET CHECKER
# ─────────────────────────────────────────────────────────────────────────────

def check_budget(dev: str, equipped_abilities: list[str]) -> tuple[bool, str]:
    """
    Validate that a player's equipped S/A/B abilities don't exceed their
    dev trait budget. C-tier abilities are unlimited and never flagged here.
    Returns (within_budget: bool, detail: str)
    """
    budget = DEV_BUDGET.get(dev, DEV_BUDGET["Normal"])
    counts = {"S": 0, "A": 0, "B": 0, "C": 0}

    for ab in equipped_abilities:
        if not ab:
            continue
        entry = ABILITY_TABLE.get(ab)
        tier = entry["tier"] if entry else "C"  # unknown = treat as C, don't flag budget
        counts[tier] = counts.get(tier, 0) + 1

    violations = []
    for tier in ("S", "A", "B"):
        allowed = budget.get(tier, 0)
        actual = counts.get(tier, 0)
        if actual > allowed:
            violations.append(f"{tier}-tier: has {actual}, max {allowed}")

    return (len(violations) == 0), "; ".join(violations)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5: CORE AUDIT FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PlayerAuditResult:
    roster_id: int
    name: str
    team: str
    pos: str
    dev: str
    archetype: str
    equipped: list[str]
    illegal_abilities: list[dict]   # [{ability, reasons}]  — stat/position fail
    budget_violation: str           # empty string if clean
    is_clean: bool

    def to_dict(self) -> dict:
        return {
            "rosterId": self.roster_id,
            "name": self.name,
            "team": self.team,
            "pos": self.pos,
            "dev": self.dev,
            "archetype": self.archetype,
            "equippedAbilities": self.equipped,
            "illegalAbilities": self.illegal_abilities,
            "budgetViolation": self.budget_violation,
            "isClean": self.is_clean,
        }

    def action_lines(self) -> list[str]:
        """Human-readable commissioner action items."""
        lines = []
        for item in self.illegal_abilities:
            reasons = "; ".join(item["reasons"])
            lines.append(f"REMOVE **{item['ability']}** — {reasons}")
        if self.budget_violation:
            lines.append(f"TRIM ABILITIES — over budget ({self.budget_violation})")
        return lines


def audit_roster(players: list[dict], player_abilities: list[dict],
                 team_filter: str | None = None) -> list[PlayerAuditResult]:
    """
    Full roster audit.

    Args:
        players:          List of player dicts from players.json
        player_abilities: List of ability dicts from playerAbilities.json
        team_filter:      If provided, audit only this team (case-insensitive partial match)

    Returns:
        List of PlayerAuditResult — only players with Star+ dev who have abilities equipped.
        Results are sorted: violations first, then clean, alphabetically by team+name.
    """

    # NOTE ON DATA SOURCES:
    # playerAbilities.json contains ALL historical ability slots across all seasons,
    # including expired, locked, and duplicate entries. It is NOT a reliable source
    # for currently-equipped abilities.
    #
    # players.json ability1–ability6 fields are the canonical currently-equipped set.
    # These are what the audit uses exclusively.
    #
    # player_abilities is accepted as a parameter for future use (e.g., history queries)
    # but is intentionally unused in the core audit path.

    results: list[PlayerAuditResult] = []

    for p in players:
        dev = _normalize_dev(p)

        # Skip Normal dev — they can't have superstar abilities
        if dev == "Normal":
            continue

        # Team filter
        if team_filter:
            team_name = (p.get("teamName") or "").lower()
            if team_filter.lower() not in team_name:
                continue

        rid = p.get("rosterId")
        # ability1-6 = canonical currently-equipped abilities
        equipped = [p.get(f"ability{i}", "") for i in range(1, 7)
                    if p.get(f"ability{i}", "")]

        if not equipped:
            continue  # no abilities equipped, nothing to audit

        archetype = calculate_true_archetype(p)

        # Pass 1: Per-ability eligibility check
        illegal = []
        for ab in equipped:
            earned, reasons = is_ability_earned(p, ab)
            if not earned:
                illegal.append({"ability": ab, "reasons": reasons})

        # Pass 2: Budget check
        budget_ok, budget_detail = check_budget(dev, equipped)

        is_clean = (len(illegal) == 0) and budget_ok

        results.append(PlayerAuditResult(
            roster_id   = rid,
            name        = f"{p.get('firstName','')} {p.get('lastName','')}".strip(),
            team        = p.get("teamName", "Free Agent"),
            pos         = p.get("pos", "?"),
            dev         = dev,
            archetype   = archetype,
            equipped    = equipped,
            illegal_abilities = illegal,
            budget_violation  = budget_detail if not budget_ok else "",
            is_clean    = is_clean,
        ))

    # Sort: violations first, then by team + name
    results.sort(key=lambda r: (r.is_clean, r.team, r.name))
    return results


def summarize_audit(results: list[PlayerAuditResult]) -> dict:
    """Return high-level summary counts for a full-league audit."""
    total        = len(results)
    violations   = [r for r in results if not r.is_clean]
    illegal_only = [r for r in violations if r.illegal_abilities]
    budget_only  = [r for r in violations if r.budget_violation and not r.illegal_abilities]
    both         = [r for r in violations if r.illegal_abilities and r.budget_violation]

    teams_affected = len(set(r.team for r in violations))

    return {
        "totalPlayersAudited": total,
        "cleanPlayers":        total - len(violations),
        "violations":          len(violations),
        "illegalStatViolations": len(illegal_only) + len(both),
        "budgetViolationsOnly":  len(budget_only),
        "teamsAffected":         teams_affected,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6: DATA_MANAGER INTEGRATION NOTES
# ─────────────────────────────────────────────────────────────────────────────
# Confirmed MaddenStats API endpoints (TSL league slug):
#
#   Full roster with OVR/dev/abilities:
#     GET /lg/tsl/export/players
#     Fields include: rosterId, firstName, lastName, pos (or position),
#                     teamId, devTrait, overallRating, ability1-ability6,
#                     contractYearsLeft, capPercent, and all stat/arch ratings.
#
#   Player ability assignments (historical, NOT used for current audit):
#     GET /lg/tsl/export/playerAbilities
#
# data_manager.py already implements:
#   _players_cache   — populated via _paginate("/export/players")
#   _abilities_cache — populated via _paginate("/export/playerAbilities")
#   get_players()           → _players_cache
#   get_player_abilities()  → _abilities_cache
#
# IMPORTANT: The stat-leader endpoints (/stats/players/passStats etc.) do NOT
# include devTrait, ability1-6, or OVR — ability auditing requires /export/players.
# If _players_cache is empty, the audit will silently skip all players (all appear
# as Normal dev). Use /wittsync to reload, or check bot startup logs.
# ─────────────────────────────────────────────────────────────────────────────


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 7: POSITION CHANGE VALIDATION
# Merged from ability_engine_additions.py
#
# check_position_change(player, from_pos, to_pos) → (legal: bool, reasons: list)
# position_change_embed_lines(player, from_pos, to_pos) → list[str] for Discord
#
# All threshold checks use AND logic (every threshold must be met).
# max_thresholds fields are UPPER bounds (player must NOT exceed them).
# ss_ability_reset=True means the game engine clears SS/XF abilities on swap —
# the commissioner must strip abilities in-game before approving the change.
# ═════════════════════════════════════════════════════════════════════════════

POSITION_CHANGE_RULES: dict[tuple[str, str], dict] = {

    # ── Safety → Linebacker ───────────────────────────────────────────────────
    ("SS", "MIKE"): {
        "thresholds":    {"heightInches": 71, "weight": 215, "tackleRating": 75, "hitPowerRating": 75},
        "max_thresholds": {"speedRating": 92, "agilityRating": 90},
        "requires_commissioner": False,
        "banned_from": ["CB"],
        "ss_ability_reset": True,
        "note": "S→LB: Speed/Agility are MAXIMUM caps (not too fast/agile for LB). CB→LB permanently banned.",
    },
    ("FS", "MIKE"): {
        "thresholds":    {"heightInches": 71, "weight": 215, "tackleRating": 75, "hitPowerRating": 75},
        "max_thresholds": {"speedRating": 92, "agilityRating": 90},
        "requires_commissioner": False,
        "banned_from": ["CB"],
        "ss_ability_reset": True,
        "note": "FS→MIKE: Same requirements as SS→LB.",
    },
    ("SS", "WILL"): {
        "thresholds":    {"heightInches": 71, "weight": 215, "tackleRating": 75, "hitPowerRating": 75},
        "max_thresholds": {"speedRating": 92, "agilityRating": 90},
        "requires_commissioner": False,
        "banned_from": ["CB"],
        "ss_ability_reset": True,
        "note": "SS→WILL: Same requirements as SS→MIKE.",
    },
    ("FS", "WILL"): {
        "thresholds":    {"heightInches": 71, "weight": 215, "tackleRating": 75, "hitPowerRating": 75},
        "max_thresholds": {"speedRating": 92, "agilityRating": 90},
        "requires_commissioner": False,
        "banned_from": ["CB"],
        "ss_ability_reset": True,
        "note": "FS→WILL: Same requirements as SS→LB.",
    },

    # ── Wide Receiver → Tight End ─────────────────────────────────────────────
    ("WR", "TE"): {
        "thresholds":    {"heightInches": 74, "weight": 225, "strengthRating": 65,
                          "runBlockRating": 60, "impactBlockRating": 60},
        "max_thresholds": {"speedRating": 90},
        "requires_commissioner": False,
        "banned_from": [],
        "ss_ability_reset": True,
        "note": "WR→TE: Speed cap of 90 prevents speed-mismatch exploits.",
    },

    # ── Halfback → Fullback ───────────────────────────────────────────────────
    ("HB", "FB"): {
        "thresholds":    {"heightInches": 70, "weight": 225,
                          "leadBlockRating": 60, "impactBlockRating": 60, "carryRating": 75},
        "max_thresholds": {"speedRating": 89, "agilityRating": 88},
        "requires_commissioner": True,
        "banned_from": [],
        "ss_ability_reset": True,
        "note": "HB→FB: Requires commissioner override. Speed/agility caps prevent elusive HBs converting.",
    },
}

# CB → any LB slot is permanently banned (no exceptions, no commissioner override)
_CB_TO_LB_BAN: set[tuple[str, str]] = {
    ("CB", "MIKE"), ("CB", "WILL"), ("CB", "SAM"),
    ("CB", "LOLB"), ("CB", "ROLB"), ("CB", "MLB"),
}


def check_position_change(player: dict, from_pos: str, to_pos: str) -> tuple[bool, list[str]]:
    """
    Validate whether a player may legally change from from_pos to to_pos.

    Returns (legal: bool, reasons: list[str])
      legal=True  → change is allowed (may still need commissioner override)
      legal=False → change is blocked; reasons explains every failure

    Notes:
      - CB → any LB is permanently banned regardless of stats.
      - SS/XF players will have abilities reset by the game engine on position
        swap — this is flagged as a warning (⚠️), not a hard block.
        Commissioner should remove abilities in-game before approving.
      - Positions not in POSITION_CHANGE_RULES require commissioner discretion.
    """
    reasons: list[str] = []

    # ── Permanent CB → LB ban ─────────────────────────────────────────────────
    if (from_pos.upper(), to_pos.upper()) in _CB_TO_LB_BAN:
        reasons.append("CB → LB position change is permanently banned in TSL.")
        return False, reasons

    # ── SS/XF ability reset warning ───────────────────────────────────────────
    dev = _normalize_dev(player)
    if dev in ("Superstar", "Superstar X-Factor"):
        reasons.append(
            f"⚠️ {dev} abilities will be RESET by the game engine on position change. "
            "Remove all SS/XF abilities in-game before converting."
        )
        # Warning only — commissioner decides whether to proceed

    # ── Look up rule ──────────────────────────────────────────────────────────
    rule = POSITION_CHANGE_RULES.get((from_pos.upper(), to_pos.upper()))
    if rule is None:
        reasons.append(
            f"No specific threshold rule for {from_pos} → {to_pos}. "
            "Commissioner discretion applies."
        )
        return True, reasons

    thresholds     = rule.get("thresholds", {})
    max_thresholds = rule.get("max_thresholds", {})

    # ── Minimum thresholds ────────────────────────────────────────────────────
    for stat, min_val in thresholds.items():
        if stat == "heightInches":
            actual = int(player.get("height", 0) or player.get("heightInches", 0) or 0)
            if actual < min_val:
                feet, inches = divmod(min_val, 12)
                reasons.append(f"Height {actual}\" < {min_val}\" ({feet}'{inches}\" required)")
        else:
            actual = int(player.get(stat, 0) or 0)
            if actual < min_val:
                label = stat.replace("Rating", "").replace("Inches", "")
                reasons.append(f"{label}={actual} < {min_val} required")

    # ── Maximum thresholds (upper caps) ───────────────────────────────────────
    for stat, max_val in max_thresholds.items():
        actual = int(player.get(stat, 0) or 0)
        if actual > max_val:
            label = stat.replace("Rating", "")
            reasons.append(f"{label}={actual} > {max_val} cap (too high for {to_pos})")

    # ── Commissioner override requirement ─────────────────────────────────────
    hard_blocks = [r for r in reasons if not r.startswith("⚠️")]
    if hard_blocks:
        return False, reasons

    if rule.get("requires_commissioner") and not hard_blocks:
        reasons.append(f"ℹ️ Commissioner override required for {from_pos} → {to_pos}.")

    return True, reasons


def position_change_embed_lines(player: dict, from_pos: str, to_pos: str) -> list[str]:
    """
    Return formatted lines for a Discord embed showing position change eligibility.
    Used by positionchange_cog.py and ability_cog.py.

    Example output:
      **John Smith** (WR → TE): ✅ ELIGIBLE
        ⚠️ Superstar abilities will be RESET — remove in-game first.
        📋 WR→TE: Speed cap of 90 prevents speed-mismatch exploits.
    """
    legal, reasons = check_position_change(player, from_pos, to_pos)
    name = f"{player.get('firstName', '')} {player.get('lastName', '')}".strip()

    lines = [
        f"**{name}** ({from_pos} → {to_pos}): {'✅ ELIGIBLE' if legal else '❌ BLOCKED'}"
    ]
    for r in reasons:
        if r.startswith("⚠️"):
            prefix = "  ⚠️"
            body   = r.removeprefix("⚠️").lstrip()
        elif r.startswith("ℹ️"):
            prefix = "  ℹ️"
            body   = r.removeprefix("ℹ️").lstrip()
        else:
            prefix = "  ❌"
            body   = r
        lines.append(f"{prefix} {body}")

    rule = POSITION_CHANGE_RULES.get((from_pos.upper(), to_pos.upper()))
    if rule and rule.get("note"):
        lines.append(f"  📋 {rule['note']}")

    return lines
