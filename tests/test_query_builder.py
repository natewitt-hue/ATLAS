# tests/test_query_builder.py
"""Unit tests for Oracle v3 QueryBuilder API."""
import pytest


def test_domain_knowledge_has_entries():
    from oracle_query_builder import DomainKnowledge
    # Must have at least the original 39 oracle-v3 stat entries
    assert len(DomainKnowledge.STATS) >= 39


def test_stat_def_passing_yards():
    from oracle_query_builder import DomainKnowledge
    sd = DomainKnowledge.STATS["passing yards"]
    assert sd.table == "offensive_stats"
    assert sd.column == "passYds"
    assert sd.agg == "SUM"
    assert sd.pos_filter == "QB"
    assert sd.category == "offense"
    assert sd.efficiency_alt == "passerRating"


def test_stat_def_defense_inverts():
    from oracle_query_builder import DomainKnowledge
    sd = DomainKnowledge.STATS["team total yards allowed"]
    assert sd.category == "team"
    assert sd.invert_sort is True


def test_stat_def_passer_rating_is_avg():
    from oracle_query_builder import DomainKnowledge
    sd = DomainKnowledge.STATS["passer rating"]
    assert sd.agg == "AVG"
    assert sd.category == "offense"


def test_stat_keyword_lookup():
    """Verify keyword-to-StatDef resolution works."""
    from oracle_query_builder import DomainKnowledge
    result = DomainKnowledge.lookup("passing yards")
    assert result is not None
    _, sd = result
    assert sd.column == "passYds"

    result2 = DomainKnowledge.lookup("sacks")
    assert result2 is not None
    _, sd2 = result2
    assert sd2.column == "defSacks"

    assert DomainKnowledge.lookup("nonexistent_xyz_999") is None


def test_query_builder_basic_select():
    """QueryBuilder generates correct SELECT with filter."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").filter(season=6, stage="regular").select("extendedName", "teamName")
    sql, params = q.build()
    assert "SELECT" in sql
    assert "offensive_stats" in sql
    assert "seasonIndex = ?" in sql or "seasonIndex=?" in sql
    assert "stageIndex = ?" in sql or "stageIndex=?" in sql
    assert params == ("6", "1")  # season 6, stage 1 = regular


def test_query_builder_aggregate():
    from oracle_query_builder import Query
    q = (Query("offensive_stats")
         .filter(season=6)
         .aggregate(passYds="SUM")
         .group_by("extendedName")
         .limit(10))
    sql, params = q.build()
    assert "SUM(CAST(passYds AS INTEGER))" in sql
    assert "GROUP BY" in sql
    assert "LIMIT 10" in sql


def test_query_builder_sort_best_offense():
    """Best offense = most yards = DESC."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").aggregate(passYds="SUM").sort_by("passYds", "best")
    sql, _ = q.build()
    assert "DESC" in sql


def test_query_builder_sort_best_defense():
    """Best individual defense = most tackles = DESC."""
    from oracle_query_builder import Query
    q = Query("defensive_stats").aggregate(defTotalTackles="SUM").sort_by("defTotalTackles", "best")
    sql, _ = q.build()
    assert "DESC" in sql


def test_query_builder_sort_worst_passer_uses_efficiency():
    """Worst passer switches to passerRating AVG instead of passYds SUM."""
    from oracle_query_builder import Query
    q = (Query("offensive_stats")
         .aggregate(passYds="SUM")
         .sort_by("passYds", "worst"))
    sql, _ = q.build()
    assert "passerRating" in sql  # Switched to efficiency metric
    assert "AVG" in sql
    assert "HAVING COUNT(*) >= 4" in sql  # Min games filter


def test_query_builder_sort_worst_rusher_no_efficiency_alt():
    """Worst rusher has no efficiency alt — uses rushYds ASC."""
    from oracle_query_builder import Query
    q = (Query("offensive_stats")
         .aggregate(rushYds="SUM")
         .sort_by("rushYds", "worst"))
    sql, _ = q.build()
    assert "rushYds" in sql
    assert "ASC" in sql
    assert "HAVING COUNT(*) >= 4" in sql


def test_query_builder_pos_filter_auto():
    """pos() adds WHERE pos='QB'."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").aggregate(passYds="SUM").pos("QB").sort_by("passYds", "best")
    sql, params = q.build()
    assert "pos = ?" in sql or "pos=?" in sql
    assert "QB" in params


def test_query_builder_read_only():
    """QueryBuilder only generates SELECT statements."""
    from oracle_query_builder import Query
    sql, _ = Query("games").filter(season=6).build()
    assert sql.strip().upper().startswith("SELECT")


def test_query_builder_auto_cast():
    """Numeric aggregations auto-wrap CAST."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").aggregate(passYds="SUM")
    sql, _ = q.build()
    assert "CAST(passYds AS INTEGER)" in sql


def test_current_season():
    from oracle_query_builder import current_season
    s = current_season()
    assert isinstance(s, int)
    assert s >= 1


def test_current_week():
    from oracle_query_builder import current_week
    w = current_week()
    assert isinstance(w, int)
    assert w >= 0


def test_resolve_team_exact():
    from oracle_query_builder import resolve_team
    assert resolve_team("lions") == "Lions"
    assert resolve_team("det") == "Lions"
    assert resolve_team("detroit") == "Lions"
    assert resolve_team("nonexistent") is None


def test_resolve_user():
    from oracle_query_builder import resolve_user
    result = resolve_user("Witt")
    assert result is None or isinstance(result, str)


def test_query_builder_execute(tmp_path):
    """Execute returns (rows, err) — correct return signature."""
    from oracle_query_builder import Query
    q = (Query("games")
         .select("homeTeamName", "awayTeamName")
         .filter(season=1, stage="regular")
         .limit(3))
    result = q.execute()
    # Must return a 2-tuple of (list, str|None)
    assert isinstance(result, tuple) and len(result) == 2
    rows, err = result
    assert isinstance(rows, list)
    assert err is None or isinstance(err, str)
    assert len(rows) <= 3
    if rows:
        assert "homeTeamName" in rows[0]


def test_stat_leaders_passing_yards():
    from oracle_query_builder import stat_leaders
    sql, params = stat_leaders("passing yards", season=None, sort="best", limit=5)
    assert isinstance(sql, str) and "SELECT" in sql.upper()
    assert "passYds" in sql
    assert "LIMIT 5" in sql


def test_stat_leaders_worst_passer():
    """Worst passer should use passerRating AVG with min games filter."""
    from oracle_query_builder import stat_leaders
    sql, params = stat_leaders("passing yards", sort="worst", limit=5)
    assert isinstance(sql, str)
    assert "passerRating" in sql
    assert "HAVING COUNT(*) >= 4" in sql


def test_team_stat_leaders_best_offense():
    from oracle_query_builder import team_stat_leaders
    sql, params = team_stat_leaders("team total yards", sort="best", limit=5)
    assert isinstance(sql, str) and "SELECT" in sql.upper()


def test_team_stat_leaders_best_defense():
    """Best defense = fewest yards = should sort ASC."""
    from oracle_query_builder import team_stat_leaders
    sql, params = team_stat_leaders("team total yards allowed", sort="best", limit=5)
    assert isinstance(sql, str)
    assert "ASC" in sql  # Invert sort: fewer yards allowed = better


def test_owner_record():
    from oracle_query_builder import owner_record
    sql, params = owner_record("TheWitt")
    assert isinstance(sql, str) and "SELECT" in sql.upper()
    assert isinstance(params, tuple)
    assert "TheWitt" in params


def test_owner_record_season():
    from oracle_query_builder import owner_record
    sql, params = owner_record("TheWitt", season=6)
    assert "seasonIndex" in sql
    assert "6" in params


def test_h2h():
    from oracle_query_builder import h2h
    sql, params = h2h("TheWitt", "KillaE94")
    assert isinstance(sql, str) and "SELECT" in sql.upper()
    assert isinstance(params, tuple)


def test_team_record():
    from oracle_query_builder import team_record
    sql, params = team_record("Lions")
    assert isinstance(sql, str) and "SELECT" in sql.upper()
    assert isinstance(params, tuple)


def test_streak():
    from oracle_query_builder import streak_query
    sql, params = streak_query("TheWitt")
    assert isinstance(sql, str) and "SELECT" in sql.upper()


def test_standings_division():
    from oracle_query_builder import standings_query
    sql, params = standings_query(division="NFC East")
    assert isinstance(sql, str) and "SELECT" in sql.upper()
    assert isinstance(params, tuple)


def test_recent_games():
    from oracle_query_builder import recent_games_query
    sql, params = recent_games_query("TheWitt", limit=5)
    assert isinstance(sql, str)
    assert "LIMIT" in sql


def test_roster():
    from oracle_query_builder import roster_query
    sql, params = roster_query("Lions")
    assert isinstance(sql, str) and "SELECT" in sql.upper()


def test_free_agents():
    from oracle_query_builder import free_agents_query
    sql, params = free_agents_query(pos="QB")
    assert isinstance(sql, str) and "SELECT" in sql.upper()


def test_draft_picks():
    from oracle_query_builder import draft_picks_query
    sql, params = draft_picks_query(team="Lions")
    assert isinstance(sql, str) and "SELECT" in sql.upper()


def test_trades():
    from oracle_query_builder import trades_query
    sql, params = trades_query(team="Lions")
    assert isinstance(sql, str) and "SELECT" in sql.upper()


def test_game_extremes_blowout():
    from oracle_query_builder import game_extremes
    sql, params = game_extremes("blowout", limit=5)
    assert isinstance(sql, str) and "SELECT" in sql.upper()
    assert isinstance(params, tuple)


def test_querybuilder_matches_tier1_sort_directions():
    """Verify QueryBuilder domain guards match Tier 1 regex sort logic."""
    from oracle_query_builder import Query

    # "top 5 passers" → DESC (best offense)
    q = Query("offensive_stats").aggregate(passYds="SUM").sort_by("passYds", "best")
    sql, _ = q.build()
    assert "DESC" in sql, "Best passers should sort DESC"

    # "worst passer" → efficiency metric (passerRating AVG, ASC, min games)
    q = Query("offensive_stats").aggregate(passYds="SUM").sort_by("passYds", "worst")
    sql, _ = q.build()
    assert "passerRating" in sql, "Worst passer should use efficiency metric"
    assert "ASC" in sql, "Worst should sort ASC"
    assert "HAVING COUNT(*)" in sql, "Worst should have min games filter"

    # "best defense" → ASC (fewest yards allowed = best, invert_sort=True)
    q = Query("team_stats").aggregate(defTotalYds="SUM").sort_by("defTotalYds", "best")
    sql, _ = q.build()
    assert "ASC" in sql, "Best defense should sort ASC (fewest yards)"

    # "worst defense" → DESC (most yards allowed = worst)
    q = Query("team_stats").aggregate(defTotalYds="SUM").sort_by("defTotalYds", "worst")
    sql, _ = q.build()
    assert "DESC" in sql, "Worst defense should sort DESC (most yards)"
