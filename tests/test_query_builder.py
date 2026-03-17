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
