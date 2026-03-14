"""
build_member_db.py — TSL Member Registry
─────────────────────────────────────────────────────────────────────────────
Creates and seeds the tsl_members table in tsl_history.db.

This is the single source of truth for:
  - All current and historical TSL members
  - Mapping current Discord usernames → historical DB usernames
  - Nickname / alias resolution for /ask and all ATLAS queries
  - PSN / Xbox handles for cross-platform lookup

Run manually:    python build_member_db.py
Import in bot:   from build_member_db import sync_members, get_known_users, get_alias_map
─────────────────────────────────────────────────────────────────────────────
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tsl_history.db")

# ─────────────────────────────────────────────────────────────────────────────
#  MEMBER REGISTRY
#
#  Fields:
#    discord_id        — Discord snowflake ID (immutable — canonical anchor)
#    discord_username  — current Discord username (changes; secondary key)
#    db_username       — username as it appears in tsl_history.db game records
#                        (may differ due to name changes — NULL = never played)
#                        AUTO-FILLED from teams table on every sync if NULL
#    nickname          — league nickname (JT, Killa, Witt etc.)
#    display_name      — current Discord display name / real name if known
#    psn               — PSN handle
#    xbox              — Xbox handle
#    twitch            — Twitch URL
#    team              — current team abbreviation
#    status            — League Owner / Admin / Member / Inactive
#    joined_date       — date joined TSL
#    active            — 1 = current member, 0 = departed/historical
#    notes             — any extra context
# ─────────────────────────────────────────────────────────────────────────────

MEMBERS = [
    # ── LEAGUE OWNER ─────────────────────────────────────────────────────────
    {
        "discord_id":       "208978020210442240",
        "discord_username": "kickerbog10",
        "db_username":       "kickerbog10",
        "nickname":          "Jason",
        "display_name":      "Signman",
        "psn":               "kickerbog10",
        "xbox":              None,
        "twitch":            None,
        "team":              "TB",
        "status":            "League Owner",
        "joined_date":       "2018-06-27",
        "active":            1,
        "notes":             "League founder",
    },

    # ── ADMINS ────────────────────────────────────────────────────────────────
    {
        "discord_id":       "322498632542846987",
        "discord_username": "TheWitt",
        "db_username":       "TheWitt",
        "nickname":          "Witt",
        "display_name":      "TheWitt",
        "psn":               "TheWitt",
        "xbox":              None,
        "twitch":            None,
        "team":              "DET",
        "status":            "Admin",
        "joined_date":       "2018-08-12",
        "active":            1,
        "notes":             "Bot developer / commissioner",
    },
    {
        "discord_id":       "705567998710382722",
        "discord_username": "Bdiddy86",
        "db_username":       "BDiddy86",
        "nickname":          "Bdiddy",
        "display_name":      "Bdiddy86",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "GB",
        "status":            "Admin",
        "joined_date":       "2018-08-12",
        "active":            1,
        "notes":             "DB username: BDiddy86",
    },
    {
        "discord_id":       "478233196408995850",
        "discord_username": "I2onDon",
        "db_username":       "Ronfk",
        "nickname":          "Ron",
        "display_name":      "I2onDon",
        "psn":               "Ronfk",
        "xbox":              None,
        "twitch":            None,
        "team":              "WAS",
        "status":            "Admin",
        "joined_date":       "2022-10-11",
        "active":            1,
        "notes":             "Discord changed from Ronfk — DB still has Ronfk",
    },
    {
        "discord_id":       "871448457414598737",
        "discord_username": "Jordantromberg",
        "db_username":       "TrombettaThanYou",
        "nickname":          "JT",
        "display_name":      "JT",
        "psn":               "Jordantromberg",
        "xbox":              None,
        "twitch":            None,
        "team":              "CIN",
        "status":            "Admin",
        "joined_date":       "2024-08-19",
        "active":            1,
        "notes":             "Current username for JT (TrombettaThanYou in DB)",
    },
    {
        "discord_id":       None,
        "discord_username": "DcNation_21",
        "db_username":       None,
        "nickname":          None,
        "display_name":      "DcNation_21",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "CAR",
        "status":            "Admin",
        "joined_date":       None,
        "active":            1,
        "notes":             "DB username unknown — needs confirmation",
    },

    # ── ACTIVE MEMBERS ────────────────────────────────────────────────────────
    {
        "discord_id":       None,
        "discord_username": "Find_the_Door",
        "db_username":       "Find_the_Door",
        "nickname":          None,
        "display_name":      "Find_the_Door",
        "psn":               "Find_the_Door",
        "xbox":              "Find the Door",
        "twitch":            None,
        "team":              "DAL",
        "status":            "Member",
        "joined_date":       "2024-08-19",
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       "1012890489114083329",
        "discord_username": "troypeska",
        "db_username":       "troypeska",
        "nickname":          "Troy",
        "display_name":      "Troy",
        "psn":               "troypeska",
        "xbox":              None,
        "twitch":            None,
        "team":              "CHI",
        "status":            "Member",
        "joined_date":       "2018-08-12",
        "active":            1,
        "notes":             "Bears owner. Discord ID: 1012890489114083329",
    },
    {
        "discord_id":       "138759200812695554",
        "discord_username": "chokolate",
        "db_username":       "Chokolate_Thunda",
        "nickname":          "Chok",
        "display_name":      "Chokolate",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "NYG",
        "status":            "Member",
        "joined_date":       "2022-08-28",
        "active":            1,
        "notes":             "DB username: Chokolate_Thunda",
    },
    {
        "discord_id":       "590340736705363978",
        "discord_username": "Bennygalactic",
        "db_username":       "BennyGalactic",
        "nickname":          None,
        "display_name":      "Benny",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "IND",
        "status":            "Member",
        "joined_date":       "2020-02-13",
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       "762900687536390154",
        "discord_username": "gio071499",
        "db_username":       "Gi0D0g88",
        "nickname":          "Gio",
        "display_name":      "Gio",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "LAC",
        "status":            "Member",
        "joined_date":       "2021-03-15",
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       "710648515566633052",
        "discord_username": "Odyssey63",
        "db_username":       "NEFF",
        "nickname":          "Neff",
        "display_name":      "Odyssey63",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "ATL",
        "status":            "Member",
        "joined_date":       "2021-09-21",
        "active":            1,
        "notes":             "Same person as NEFF in DB — old username",
    },
    {
        "discord_id":       "808838150083706920",
        "discord_username": "Bjohnson919",
        "db_username":       "Bjohnson919",
        "nickname":          "BJ",
        "display_name":      "Johnson",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "MIN",
        "status":            "Member",
        "joined_date":       "2022-08-28",
        "active":            1,
        "notes":             "JB3v3 is a separate departed member",
    },
    {
        "discord_id":       None,
        "discord_username": "JB3v3",
        "db_username":       "JB3v3",
        "nickname":          None,
        "display_name":      "JB3v3",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Departed member — not same as Bjohnson919",
    },
    {
        "discord_id":       "402604212732821504",
        "discord_username": "cfar89",
        "db_username":       "cfar89",
        "nickname":          None,
        "display_name":      "cfar89",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "LAR",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       "634221098250010634",
        "discord_username": "Topshotta338",
        "db_username":       None,
        "nickname":          "Shottaz",
        "display_name":      "Shottaz",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "NE",
        "status":            "Member",
        "joined_date":       "2023-09-30",
        "active":            1,
        "notes":             "New enough that DB username may not exist",
    },
    {
        "discord_id":       "934556990045310996",
        "discord_username": "Sheldon_Scott",
        "db_username":       "Swole_Shell50",
        "nickname":          "Shelly",
        "display_name":      "Sheldon Scott",
        "psn":               "Shelly_shell",
        "xbox":              None,
        "twitch":            None,
        "team":              "LV",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             "DB username Swole_Shell50 — PSN is Shelly_shell",
    },
    {
        "discord_id":       "520354406001016833",
        "discord_username": "Clutch_Cowboys",
        "db_username":       "Mr_Clutch723",
        "nickname":          None,
        "display_name":      "Clutch",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "ARI",
        "status":            "Member",
        "joined_date":       "2023-11-09",
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       "432242024163442688",
        "discord_username": "rissa",
        "db_username":       None,
        "nickname":          "Rissa",
        "display_name":      "Rissa",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "NO",
        "status":            "Member",
        "joined_date":       "2023-12-20",
        "active":            1,
        "notes":             "DB username unknown — auto-discovery via teams table will fill",
    },
    {
        "discord_id":       None,
        "discord_username": "MellowFire",
        "db_username":       "MeLLoW_FiRe",
        "nickname":          None,
        "display_name":      "Mellow",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "JAX",
        "status":            "Member",
        "joined_date":       "2024-01-31",
        "active":            1,
        "notes":             "DB username: MeLLoW_FiRe",
    },
    {
        "discord_id":       "406316042076422155",
        "discord_username": "A1_Shaun",
        "db_username":       "SuaveShaunTTV",
        "nickname":          "Tuna",
        "display_name":      "A1_Shaun",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "BAL",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             "Nickname Tuna confirmed",
    },
    {
        "discord_id":       "606222129779965972",
        "discord_username": "Will_Chamberlain",
        "db_username":       "Will_Chamberlain",
        "nickname":          None,
        "display_name":      "Will_Chamberlain",
        "psn":               "will_chamberlain",
        "xbox":              None,
        "twitch":            None,
        "team":              "PHI",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       "374225201501700097",
        "discord_username": "Dtowndon",
        "db_username":       "D-TownDon",
        "nickname":          "Don",
        "display_name":      "Don",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "DEN",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             "DB username: D-TownDon",
    },
    {
        "discord_id":       None,
        "discord_username": "bucsrule21",
        "db_username":       None,
        "nickname":          None,
        "display_name":      "bucsrule21",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Left/removed from league",
    },
    {
        "discord_id":       None,
        "discord_username": "kermitdeeefrog",
        "db_username":       "DrewBreesus2192",
        "nickname":          None,
        "display_name":      "Kerm",
        "psn":               None,
        "xbox":              "DrewBreesus2192",
        "twitch":            None,
        "team":              "KC",
        "status":            "Member",
        "joined_date":       "2025-09-08",
        "active":            1,
        "notes":             "Xbox: DrewBreesus2192 matches DB username",
    },
    {
        "discord_id":       None,
        "discord_username": "jbrks2011",
        "db_username":       None,
        "nickname":          None,
        "display_name":      "jbrks2011",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             "No team assigned yet",
    },
    {
        "discord_id":       None,
        "discord_username": "nickpapura23",
        "db_username":       None,
        "nickname":          None,
        "display_name":      "nickpapura23",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "SEA",
        "status":            "Member",
        "joined_date":       "2025-09-14",
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "TheNotoriousLTH",
        "db_username":       "TheNotoriousLTH",
        "nickname":          "LTH",
        "display_name":      "TheNotoriousLTH",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "HOU",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             "DANGERESQUE_2 is a separate old entry — TheNotoriousLTH is current",
    },
    {
        "discord_id":       None,
        "discord_username": "Drakee_GG",
        "db_username":       "Drakee_GG",
        "nickname":          None,
        "display_name":      "Drake",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "SF",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "bigmizz716",
        "db_username":       None,
        "nickname":          "BigMizz",
        "display_name":      "BigMizz",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "MIA",
        "status":            "Member",
        "joined_date":       "2026-02-07",
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "BurrowsMVP9",
        "db_username":       None,
        "nickname":          None,
        "display_name":      "BurrowsMVP9",
        "psn":               "BurrowsMVP9",
        "xbox":              None,
        "twitch":            "https://twitch.tv/wrkconkoz",
        "team":              "CLE",
        "status":            "Member",
        "joined_date":       "2026",
        "active":            1,
        "notes":             "Die hard Bengals fan, 50yo, father of 3. New Season 7.",
    },

    # ── NEW MEMBERS (seen in Discord, not yet in DB) ──────────────────────────
    {
        "discord_id":       None,
        "discord_username": "BabaYaga",
        "db_username":       None,
        "nickname":          "BabaYaga",
        "display_name":      "BabaYaga",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "PIT",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             "Steelers owner — Discord username unconfirmed",
    },
    {
        "discord_id":       "694316056206114827",
        "discord_username": "NewmanO64",
        "db_username":       None,
        "nickname":          None,
        "display_name":      "NewmanO64",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Not a league member — server lurker",
    },
    {
        "discord_id":       None,
        "discord_username": "Max",
        "db_username":       None,
        "nickname":          "Max",
        "display_name":      "Max",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "TEN",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       "966861614546559086",
        "discord_username": "Bryan_TSL",
        "db_username":       None,
        "nickname":          "Bryan",
        "display_name":      "Bryan",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Kicked from league",
    },
    {
        "discord_id":       None,
        "discord_username": "Pam_TSL",
        "db_username":       None,
        "nickname":          "Pam",
        "display_name":      "Pam",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             "Identity unknown — needs confirmation from commissioner",
    },
    {
        "discord_id":       None,
        "discord_username": "TheBabado",
        "db_username":       None,
        "nickname":          "Babado",
        "display_name":      "The Babado",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             "Identity unknown — display truncated in screenshot, needs confirmation",
    },

    # ── HISTORICAL / ALL-TIME MEMBERS (departed but in DB) ────────────────────
    {
        "discord_id":       None,
        "discord_username": "TrombettaThanYou",
        "db_username":       "TrombettaThanYou",
        "nickname":          "JT",
        "display_name":      "TrombettaThanYou",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Old Discord username for Jordantromberg (same person)",
    },
    {
        "discord_id":       "209416082786746368",
        "discord_username": "KillaE94",
        "db_username":       "KillaE94",
        "nickname":          "Killa",
        "display_name":      "KillaE94",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "Villanova46",
        "db_username":       "Villanova46",
        "nickname":          "Nova",
        "display_name":      "Villanova46",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       "968230853920559114",
        "discord_username": "PNick12",
        "db_username":       "PNick12",
        "nickname":          "PNick",
        "display_name":      "PNick12",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       "308657934815068161",
        "discord_username": "KJJ205",
        "db_username":       "KJJ205",
        "nickname":          "Ken",
        "display_name":      "KJJ205",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       "1253510201626329208",
        "discord_username": "OliveiraYourFace",
        "db_username":       "OliveiraYourFace",
        "nickname":          "Jo",
        "display_name":      "OliveiraYourFace",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "MR_C-A-N-A-D-A",
        "db_username":       "MR_C-A-N-A-D-A",
        "nickname":          "MrCanada",
        "display_name":      "MR_C-A-N-A-D-A",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "AFFINIZE",
        "db_username":       "AFFINIZE",
        "nickname":          "John",
        "display_name":      "AFFINIZE",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       "600087875970924557",
        "discord_username": "NutzonJorge",
        "db_username":       "NutzonJorge",
        "nickname":          "Jorge",
        "display_name":      "NutzonJorge",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "SBAEZ",
        "db_username":       "SBAEZ",
        "nickname":          "Baez",
        "display_name":      "SBAEZ",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "Rahjeet",
        "db_username":       "Rahjeet",
        "nickname":          "Rahj",
        "display_name":      "Rahjeet",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "DANGERESQUE_2",
        "db_username":       "DANGERESQUE_2",
        "nickname":          "LTH",
        "display_name":      "DANGERESQUE_2",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Old Discord username for TheNotoriousLTH — confirmed same person",
    },
    {
        "discord_id":       None,
        "discord_username": "ChokolateThunda",
        "db_username":       "Chokolate_Thunda",
        "nickname":          "Chok",
        "display_name":      "ChokolateThunda",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Old username for chokolate — same person",
    },
    {
        "discord_id":       None,
        "discord_username": "WithoutRemorse",
        "db_username":       "WithoutRemorse",
        "nickname":          "Remo",
        "display_name":      "WithoutRemorse",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "KEEM",
        "db_username":       "Keem_50kFG",
        "nickname":          "Keem",
        "display_name":      "KEEM",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "DoceQuatro24",
        "db_username":       "DoceQuatro24",
        "nickname":          "Pope",
        "display_name":      "DoceQuatro24",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "SHARLOND",
        "db_username":       "SHARLOND",
        "nickname":          "Sharlond",
        "display_name":      "SHARLOND",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "Hester2003",
        "db_username":       "Hester2003",
        "nickname":          "Hester",
        "display_name":      "Hester2003",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "Unbeatable00",
        "db_username":       "Unbeatable00",
        "nickname":          "Unbeatable",
        "display_name":      "Unbeatable00",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "ShellyShell",
        "db_username":       "Swole_Shell50",
        "nickname":          "Shelly",
        "display_name":      "ShellyShell",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Old username — now Sheldon_Scott",
    },
    {
        "discord_id":       None,
        "discord_username": "Epone",
        "db_username":       "Epone",
        "nickname":          "Epone",
        "display_name":      "Epone",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "MStutts2799",
        "db_username":       "MStutts2799",
        "nickname":          "Stutts",
        "display_name":      "MStutts2799",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "AIRFLIGHT_OC",
        "db_username":       "AIRFLIGHT_OC",
        "nickname":          "Airflight",
        "display_name":      "AIRFLIGHT_OC",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "Strikernaut",
        "db_username":       "Strikernaut",
        "nickname":          "Strikernaut",
        "display_name":      "Strikernaut",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "ROBBYD192",
        "db_username":       "ROBBYD192",
        "nickname":          "RobbyD",
        "display_name":      "ROBBYD192",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "RUCKDOESWORK",
        "db_username":       "quickcroom",
        "nickname":          "Ruck",
        "display_name":      "RUCKDOESWORK",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "DB username: quickcroom",
    },
    {
        "discord_id":       "346817461527642112",
        "discord_username": "THE_KG_518",
        "db_username":       "The_KG_518",
        "nickname":          "KG",
        "display_name":      "THE_KG_518",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       "217340612452679682",
        "discord_username": "Khaled",
        "db_username":       "Khaled",
        "nickname":          "Khaled",
        "display_name":      "Khaled",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "ERIC",
        "db_username":       "ERIC",
        "nickname":          "Eric",
        "display_name":      "ERIC",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             None,
    },
    {
        "discord_id":       None,
        "discord_username": "Jnolte",
        "db_username":       "Jnolte",
        "nickname":          "Nolte",
        "display_name":      "Nolte",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              "NYJ",
        "status":            "Member",
        "joined_date":       None,
        "active":            1,
        "notes":             None,
    },
    {
        "discord_id":       None,    # Same person as Odyssey63 — ID lives on active entry
        "discord_username": "NEFF",
        "db_username":       "NEFF",
        "nickname":          "Neff",
        "display_name":      "NEFF",
        "psn":               None,
        "xbox":              None,
        "twitch":            None,
        "team":              None,
        "status":            "Inactive",
        "joined_date":       None,
        "active":            0,
        "notes":             "Old Discord username for Odyssey63 (same person)",
    },
]
def build_member_table(db_path: str = DB_PATH):
    """Create tsl_members table (if needed) and upsert all known members.

    Uses CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT so that
    runtime team assignments (set via /commish assign) survive bot restarts.
    Seed data fills empty fields but never overwrites a runtime team assignment.
    """
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tsl_members (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_id       TEXT UNIQUE,
            discord_username TEXT NOT NULL UNIQUE,
            db_username      TEXT,
            nickname         TEXT,
            display_name     TEXT,
            psn              TEXT,
            xbox             TEXT,
            twitch           TEXT,
            team             TEXT,
            status           TEXT DEFAULT 'Member',
            joined_date      TEXT,
            active           INTEGER DEFAULT 1,
            notes            TEXT
        )
    """)

    # Upsert each member: insert new rows, update existing ones.
    # Key: team uses COALESCE(tsl_members.team, excluded.team) so runtime
    # assignments (set via /commish assign) are never overwritten by seed data.
    for m in MEMBERS:
        defaults = {
            "discord_id": None, "discord_username": None, "db_username": None,
            "nickname": None, "display_name": None, "psn": None, "xbox": None,
            "twitch": None, "team": None, "status": "Member", "joined_date": None,
            "active": 1, "notes": None,
        }
        row = {**defaults, **m}
        cur.execute("""
            INSERT INTO tsl_members
                (discord_id, discord_username, db_username, nickname, display_name,
                 psn, xbox, twitch, team, status, joined_date, active, notes)
            VALUES
                (:discord_id, :discord_username, :db_username, :nickname, :display_name,
                 :psn, :xbox, :twitch, :team, :status, :joined_date, :active, :notes)
            ON CONFLICT(discord_username) DO UPDATE SET
                discord_id   = COALESCE(excluded.discord_id,   tsl_members.discord_id),
                db_username  = COALESCE(excluded.db_username,  tsl_members.db_username),
                nickname     = COALESCE(excluded.nickname,     tsl_members.nickname),
                display_name = COALESCE(excluded.display_name, tsl_members.display_name),
                psn          = COALESCE(excluded.psn,          tsl_members.psn),
                xbox         = COALESCE(excluded.xbox,         tsl_members.xbox),
                twitch       = COALESCE(excluded.twitch,       tsl_members.twitch),
                team         = COALESCE(tsl_members.team,      excluded.team),
                status       = COALESCE(excluded.status,       tsl_members.status),
                joined_date  = COALESCE(excluded.joined_date,  tsl_members.joined_date),
                active       = excluded.active,
                notes        = COALESCE(excluded.notes,        tsl_members.notes)
        """, row)

    conn.commit()
    count  = cur.execute("SELECT COUNT(*) FROM tsl_members").fetchone()[0]
    active = cur.execute("SELECT COUNT(*) FROM tsl_members WHERE active=1").fetchone()[0]
    verify = cur.execute("SELECT COUNT(*) FROM tsl_members WHERE notes LIKE '%VERIFY%'").fetchone()[0]
    conn.close()

    return {"total": count, "active": active, "needs_verify": verify}


def sync_db_usernames_from_teams(db_path: str = DB_PATH) -> dict:
    """
    Auto-fill missing db_usernames by cross-referencing the live teams table.

    The teams table (populated by sync_tsl_db) has userName = the current owner's
    game username, and abbrName = team abbreviation (e.g. 'CHI', 'DET').

    For every tsl_members row where db_username IS NULL and team IS NOT NULL,
    we look up teams.userName where teams.abbrName matches — and write that back
    as the db_username.

    This runs automatically on every startup/sync so new members self-populate
    without manual registry edits.
    """
    conn = sqlite3.connect(db_path)
    cur  = conn.cursor()

    # Check teams table exists
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "teams" not in tables:
        conn.close()
        return {"filled": 0, "skipped": 0, "reason": "teams table not yet populated"}

    # Find members missing db_username but with a team assignment
    missing = cur.execute("""
        SELECT discord_username, team FROM tsl_members
        WHERE db_username IS NULL AND team IS NOT NULL AND active = 1
    """).fetchall()

    filled = 0
    skipped = []
    for discord_u, team_abbr in missing:
        row = cur.execute(
            "SELECT userName FROM teams WHERE abbrName = ? AND userName IS NOT NULL AND userName != ''",
            (team_abbr,)
        ).fetchone()
        if row:
            db_u = row[0]
            cur.execute(
                "UPDATE tsl_members SET db_username = ? WHERE discord_username = ?",
                (db_u, discord_u)
            )
            filled += 1
        else:
            skipped.append(f"{discord_u} ({team_abbr})")

    conn.commit()
    conn.close()

    if filled:
        print(f"[MemberDB] Auto-filled {filled} db_username(s) from teams table")
    if skipped:
        print(f"[MemberDB] Could not auto-fill: {', '.join(skipped)}")

    return {"filled": filled, "skipped": skipped}


def validate_db_usernames(db_path: str = DB_PATH) -> list[dict]:
    """
    Cross-check every db_username in tsl_members against actual game records.
    Returns list of members whose db_username appears in ZERO games — likely wrong.
    Run this after sync to surface bad entries early.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "games" not in tables:
        conn.close()
        return []

    members = conn.execute(
        "SELECT discord_username, db_username, nickname, team FROM tsl_members "
        "WHERE db_username IS NOT NULL AND active = 1"
    ).fetchall()

    ghosts = []
    for discord_u, db_u, nick, team in members:
        count = conn.execute(
            "SELECT COUNT(*) FROM games WHERE homeUser = ? OR awayUser = ?",
            (db_u, db_u)
        ).fetchone()[0]
        if count == 0:
            ghosts.append({
                "discord_username": discord_u,
                "db_username": db_u,
                "nickname": nick,
                "team": team,
            })

    conn.close()
    return ghosts


def get_db_username_for_discord_id(discord_id: int | str, db_path: str = DB_PATH) -> str | None:
    """
    Look up db_username by Discord ID — the most reliable resolver.
    Used in /ask to map interaction.user.id → exact DB username for 'me/my/I' queries.
    Returns None if not found or db_username is NULL.
    """
    conn = sqlite3.connect(db_path)
    row  = conn.execute(
        "SELECT db_username FROM tsl_members WHERE discord_id = ?",
        (str(discord_id),)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None


def get_known_users(db_path: str = DB_PATH) -> list[str]:
    """
    Return list of all db_usernames for use as KNOWN_USERS in history_cog.
    Includes both current and historical members who have DB records.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT db_username FROM tsl_members WHERE db_username IS NOT NULL"
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_alias_map(db_path: str = DB_PATH) -> dict[str, str]:
    """
    Return full alias map for fuzzy_resolve_user():
      nickname -> db_username
      discord_username -> db_username
      psn -> db_username
      xbox -> db_username
      display_name -> db_username
    All keys lowercased for case-insensitive matching.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT discord_username, db_username, nickname,
               display_name, psn, xbox
        FROM tsl_members
        WHERE db_username IS NOT NULL
    """).fetchall()
    conn.close()

    alias_map = {}
    for discord_u, db_u, nick, display, psn, xbox in rows:
        target = db_u
        for alias in [discord_u, nick, display, psn, xbox]:
            if alias:
                alias_map[alias.lower()] = target
    return alias_map


def get_username_to_nick_map(db_path: str = DB_PATH) -> dict[str, str]:
    """
    Return db_username → nickname map for stats_hub_cog's _USERNAME_TO_NICK.
    Used for ring lookups and display name resolution.
    Only includes members with both a db_username and a nickname.
    """
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT db_username, nickname FROM tsl_members WHERE db_username IS NOT NULL AND nickname IS NOT NULL"
    ).fetchall()
    conn.close()
    return {db_u: nick for db_u, nick in rows}


def get_active_members(db_path: str = DB_PATH) -> list[dict]:
    """Return all active members as dicts."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM tsl_members WHERE active=1 ORDER BY status DESC, discord_username"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_member(member: dict, db_path: str = DB_PATH):
    """
    Add or update a single member record.
    member dict must contain discord_username at minimum.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO tsl_members
            (discord_id, discord_username, db_username, nickname, display_name,
             psn, xbox, twitch, team, status, joined_date, active, notes)
        VALUES
            (:discord_id, :discord_username, :db_username, :nickname, :display_name,
             :psn, :xbox, :twitch, :team, :status, :joined_date, :active, :notes)
        ON CONFLICT(discord_username) DO UPDATE SET
            discord_id   = COALESCE(excluded.discord_id,   discord_id),
            db_username  = COALESCE(excluded.db_username,  db_username),
            nickname     = COALESCE(excluded.nickname,     nickname),
            display_name = COALESCE(excluded.display_name, display_name),
            psn          = COALESCE(excluded.psn,          psn),
            xbox         = COALESCE(excluded.xbox,         xbox),
            twitch       = COALESCE(excluded.twitch,       twitch),
            team         = COALESCE(excluded.team,         team),
            status       = COALESCE(excluded.status,       status),
            joined_date  = COALESCE(excluded.joined_date,  joined_date),
            active       = COALESCE(excluded.active,       active),
            notes        = COALESCE(excluded.notes,        notes)
    """, {**{
        "discord_id": None, "discord_username": None, "db_username": None,
        "nickname": None, "display_name": None, "psn": None, "xbox": None,
        "twitch": None, "team": None, "status": "Member", "joined_date": None,
        "active": 1, "notes": None,
    }, **member})
    conn.commit()
    conn.close()


if __name__ == "__main__":
    print(f"Building tsl_members table in: {DB_PATH}")
    result = build_member_table()
    print(f"✅ {result['total']} members seeded ({result['active']} active)")
    print(f"⚠️  {result['needs_verify']} entries flagged VERIFY — need your confirmation")
    print()

    # Print items needing verification
    conn = sqlite3.connect(DB_PATH)
    needs = conn.execute(
        "SELECT discord_username, db_username, notes FROM tsl_members WHERE notes LIKE '%VERIFY%'"
    ).fetchall()
    conn.close()
    print("Items needing verification:")
    for discord_u, db_u, notes in needs:
        print(f"  {discord_u:25} → assumed db_username: {db_u}")
        print(f"    Notes: {notes}")
    print()

    # Show full alias map
    aliases = get_alias_map()
    print(f"Alias map has {len(aliases)} entries — sample:")
    for k, v in list(aliases.items())[:10]:
        print(f"  {k!r:30} → {v!r}")
