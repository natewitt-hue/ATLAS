# tests/test_query_builder.py
"""Unit tests for Oracle v3 QueryBuilder API."""
import pytest


def test_stat_defs_has_all_39_entries():
    from oracle_query_builder import STAT_DEFS
    assert len(STAT_DEFS) == 39


def test_stat_def_passing_yards():
    from oracle_query_builder import STAT_DEFS
    sd = STAT_DEFS["passYds"]
    assert sd.table == "offensive_stats"
    assert sd.column == "passYds"
    assert sd.agg == "SUM"
    assert sd.pos == "QB"
    assert sd.category == "offense"
    assert sd.efficiency_alt == "passerRating"


def test_stat_def_defense_inverts():
    from oracle_query_builder import STAT_DEFS
    sd = STAT_DEFS["defTotalYds_team"]
    assert sd.category == "defense"
    assert sd.pos is None


def test_stat_def_passer_rating_is_avg():
    from oracle_query_builder import STAT_DEFS
    sd = STAT_DEFS["passerRating"]
    assert sd.agg == "AVG"
    assert sd.category == "offense"


def test_stat_keyword_lookup():
    """Verify keyword-to-StatDef resolution works."""
    from oracle_query_builder import resolve_stat_keyword
    sd = resolve_stat_keyword("passing yards")
    assert sd.column == "passYds"
    sd2 = resolve_stat_keyword("sacks")
    assert sd2.column == "defSacks"
    assert resolve_stat_keyword("nonexistent") is None


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
    q = Query("offensive_stats").stat("passYds").sort("best")
    sql, _ = q.build()
    assert "DESC" in sql


def test_query_builder_sort_best_defense():
    """Best individual defense = most tackles = DESC."""
    from oracle_query_builder import Query
    q = Query("defensive_stats").stat("defTotalTackles").sort("best")
    sql, _ = q.build()
    # Individual defense "best" = most tackles = DESC
    assert "DESC" in sql


def test_query_builder_sort_worst_passer_uses_efficiency():
    """Worst passer switches to passerRating AVG instead of passYds SUM."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").stat("passYds").sort("worst")
    sql, _ = q.build()
    assert "passerRating" in sql  # Switched to efficiency metric
    assert "AVG" in sql
    assert "HAVING COUNT(*) >= 4" in sql  # Min games filter


def test_query_builder_sort_worst_rusher_no_efficiency_alt():
    """Worst rusher has no efficiency alt — uses rushYds ASC."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").stat("rushYds").sort("worst")
    sql, _ = q.build()
    assert "rushYds" in sql
    assert "ASC" in sql
    assert "HAVING COUNT(*) >= 4" in sql


def test_query_builder_pos_filter_auto():
    """stat('passYds') auto-adds WHERE pos='QB'."""
    from oracle_query_builder import Query
    q = Query("offensive_stats").stat("passYds").sort("best")
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


def test_query_builder_execute(tmp_path):
    """Execute runs the built SQL and returns (rows, err) — correct return signature."""
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
    # If data exists, validate shape
    assert len(rows) <= 3
    if rows:
        assert "homeTeamName" in rows[0]
