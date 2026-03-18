# conftest.py — root-level pytest configuration
"""
Creates an in-memory (temp-file) tsl_history.db with all required tables for tests.
The worktree DB may be empty; this fixture ensures Layer 1 tests get err=None.
"""
import sqlite3
import tempfile
from pathlib import Path
import pytest
import oracle_query_builder as oqb

_DDL = """
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    scheduleId TEXT,
    seasonIndex TEXT,
    stageIndex TEXT,
    weekIndex TEXT,
    homeTeamId TEXT,
    awayTeamId TEXT,
    homeTeamName TEXT,
    awayTeamName TEXT,
    homeScore TEXT,
    awayScore TEXT,
    status TEXT,
    homeUser TEXT,
    awayUser TEXT,
    winner_user TEXT,
    loser_user TEXT,
    winner_team TEXT,
    loser_team TEXT
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    teamName TEXT,
    nickName TEXT,
    cityName TEXT,
    abbrName TEXT,
    divisionName TEXT,
    conferenceName TEXT
);
CREATE TABLE IF NOT EXISTS standings (
    id INTEGER PRIMARY KEY,
    teamName TEXT,
    totalWins TEXT,
    totalLosses TEXT,
    totalTies TEXT,
    divisionName TEXT,
    conferenceName TEXT,
    seed TEXT,
    winPct TEXT,
    ptsFor TEXT,
    ptsAgainst TEXT
);
CREATE TABLE IF NOT EXISTS offensive_stats (
    id INTEGER PRIMARY KEY,
    seasonIndex TEXT,
    stageIndex TEXT,
    weekIndex TEXT,
    teamName TEXT,
    fullName TEXT,
    extendedName TEXT,
    gameId TEXT,
    teamId TEXT,
    rosterId TEXT,
    pos TEXT,
    passAtt TEXT,
    passComp TEXT,
    passCompPct TEXT,
    passTDs TEXT,
    passInts TEXT,
    passYds TEXT,
    passSacks TEXT,
    passerRating TEXT,
    rushAtt TEXT,
    rushYds TEXT,
    rushTDs TEXT,
    rushFum TEXT,
    recCatches TEXT,
    recDrops TEXT,
    recYds TEXT,
    recTDs TEXT,
    recYdsAfterCatch TEXT
);
CREATE TABLE IF NOT EXISTS defensive_stats (
    id INTEGER PRIMARY KEY,
    seasonIndex TEXT,
    stageIndex TEXT,
    weekIndex TEXT,
    teamName TEXT,
    fullName TEXT,
    extendedName TEXT,
    gameId TEXT,
    teamId TEXT,
    rosterId TEXT,
    statId TEXT,
    pos TEXT,
    defTotalTackles TEXT,
    defSacks TEXT,
    defInts TEXT,
    defForcedFum TEXT,
    defFumRec TEXT,
    defTDs TEXT,
    defDeflections TEXT
);
CREATE TABLE IF NOT EXISTS team_stats (
    id INTEGER PRIMARY KEY,
    seasonIndex TEXT,
    stageIndex TEXT,
    weekIndex TEXT,
    teamName TEXT,
    offTotalYds TEXT,
    offPassYds TEXT,
    offRushYds TEXT,
    offPassTDs TEXT,
    offRushTDs TEXT,
    off1stDowns TEXT,
    offSacks TEXT,
    defTotalYds TEXT,
    defPassYds TEXT,
    defRushYds TEXT,
    defSacks TEXT,
    ptsFor TEXT,
    ptsAgainst TEXT,
    tODiff TEXT,
    tOGiveAways TEXT,
    tOTakeaways TEXT,
    penalties TEXT,
    penaltyYds TEXT
);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    teamName TEXT,
    firstName TEXT,
    lastName TEXT,
    pos TEXT,
    playerBestOvr TEXT,
    age TEXT,
    dev TEXT,
    isFA TEXT,
    isOnIR TEXT,
    jerseyNum TEXT,
    college TEXT,
    yearsPro TEXT,
    capHit TEXT
);
CREATE TABLE IF NOT EXISTS player_abilities (
    id INTEGER PRIMARY KEY,
    teamName TEXT,
    firstName TEXT,
    lastName TEXT,
    title TEXT,
    description TEXT
);
CREATE TABLE IF NOT EXISTS owner_tenure (
    id INTEGER PRIMARY KEY,
    teamName TEXT,
    userName TEXT,
    seasonIndex TEXT,
    games_played INTEGER
);
CREATE TABLE IF NOT EXISTS player_draft_map (
    id INTEGER PRIMARY KEY,
    extendedName TEXT,
    drafting_team TEXT,
    drafting_season TEXT,
    draftRound TEXT,
    draftPick TEXT,
    pos TEXT,
    playerBestOvr TEXT,
    dev TEXT,
    was_traded TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    team1Name TEXT,
    team2Name TEXT,
    team1Sent TEXT,
    team2Sent TEXT,
    seasonIndex TEXT,
    weekIndex TEXT,
    status TEXT
);
"""


@pytest.fixture(autouse=True, scope="session")
def patch_db_path(tmp_path_factory):
    """Replace oracle_query_builder.DB_PATH with a temp DB that has all tables."""
    tmp_dir = tmp_path_factory.mktemp("db")
    db_file = tmp_dir / "tsl_history_test.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()
    # Patch the module-level DB_PATH
    original = oqb.DB_PATH
    oqb.DB_PATH = db_file
    yield db_file
    oqb.DB_PATH = original
