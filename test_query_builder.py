"""
test_query_builder.py — Unit tests for Oracle v3 QueryBuilder + DomainKnowledge
═══════════════════════════════════════════════════════════════════════════════════
Phase 1 gate: all tests must pass with no behavior changes to existing code.

Tests validate SQL generation correctness — no database required.
"""

from __future__ import annotations

import pytest

from oracle_query_builder import (
    DomainKnowledge,
    Query,
    StatDef,
    # Domain functions
    h2h,
    owner_record,
    team_record,
    standings_query,
    streak_query,
    stat_leaders,
    team_stat_leaders,
    roster_query,
    free_agents_query,
    draft_picks_query,
    abilities_query,
    trades_query,
    owner_history_query,
    game_extremes,
    recent_games_query,
    compare_seasons,
    improvement_leaders,
    career_trajectory,
    # Utilities
    compare_datasets,
    summarize,
)


# ══════════════════════════════════════════════════════════════════════════════
#  DomainKnowledge Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDomainKnowledge:
    """Verify the stat registry is complete and correct."""

    def test_all_original_stats_present(self):
        """Every stat from codex_intents.STAT_REGISTRY must exist in DomainKnowledge."""
        original_keys = {
            "passing touchdowns", "passing yards", "passing tds", "pass tds",
            "pass yards", "interceptions thrown", "passer rating",
            "completion percentage", "completions",
            "rushing touchdowns", "rushing yards", "rushing tds",
            "rush yards", "rush tds", "fumbles",
            "receiving touchdowns", "receiving yards", "receiving tds",
            "receptions", "catches", "drops", "yards after catch",
            "forced fumbles", "fumble recoveries", "defensive tds",
            "defensive touchdowns", "pass deflections", "deflections",
            "tackles", "sacks", "interceptions",
        }
        for key in original_keys:
            sd = DomainKnowledge.get(key)
            assert sd is not None, f"Missing stat: {key}"
            assert isinstance(sd, StatDef)

    def test_stat_count_at_least_39(self):
        """At least 39 stat definitions (original count)."""
        assert len(DomainKnowledge.STATS) >= 39

    def test_passing_stats_are_qb_filtered(self):
        """All passing stats must have pos_filter='QB'."""
        passing_keys = [
            "passing touchdowns", "passing yards", "passing tds", "pass tds",
            "pass yards", "interceptions thrown", "passer rating",
            "completion percentage", "completions",
        ]
        for key in passing_keys:
            sd = DomainKnowledge.get(key)
            assert sd.pos_filter == "QB", f"{key} should filter to QB"

    def test_rushing_receiving_no_pos_filter(self):
        """Rushing/receiving stats should NOT filter by position."""
        keys = ["rushing yards", "receiving yards", "receptions", "fumbles"]
        for key in keys:
            sd = DomainKnowledge.get(key)
            assert sd.pos_filter is None, f"{key} should not have pos filter"

    def test_defense_individual_not_inverted(self):
        """Individual defensive stats (tackles, sacks) are NOT inverted — more is better."""
        for key in ["tackles", "sacks", "interceptions", "forced fumbles"]:
            sd = DomainKnowledge.get(key)
            assert sd.invert_sort is False, f"{key} should not be inverted"

    def test_team_defense_yards_inverted(self):
        """Team defensive yards allowed stats ARE inverted — fewer is better."""
        for key in ["team total yards allowed", "team pass yards allowed", "team rush yards allowed"]:
            sd = DomainKnowledge.get(key)
            assert sd is not None, f"Missing stat: {key}"
            assert sd.invert_sort is True, f"{key} should be inverted (fewer = better)"

    def test_efficiency_stats_use_avg(self):
        """Passer rating and completion percentage use AVG, not SUM."""
        for key in ["passer rating", "completion percentage"]:
            sd = DomainKnowledge.get(key)
            assert sd.agg == "AVG", f"{key} should use AVG"
            assert sd.cast_type == "REAL", f"{key} should cast to REAL"

    def test_volume_stats_use_sum(self):
        """Volume stats (yards, TDs) use SUM."""
        for key in ["passing yards", "rushing yards", "receiving yards", "tackles"]:
            sd = DomainKnowledge.get(key)
            assert sd.agg == "SUM", f"{key} should use SUM"

    def test_passing_yards_has_efficiency_alt(self):
        """passYds should have passerRating as efficiency alternative."""
        sd = DomainKnowledge.get("passing yards")
        assert sd.efficiency_alt == "passerRating"

    def test_lookup_longest_first(self):
        """Lookup should prefer 'passing touchdowns' over 'pass tds'."""
        result = DomainKnowledge.lookup("who has the most passing touchdowns")
        assert result is not None
        key, sd = result
        assert key == "passing touchdowns"
        assert sd.column == "passTDs"

    def test_lookup_returns_none_for_unknown(self):
        """Lookup should return None for unknown stats."""
        assert DomainKnowledge.lookup("blitz percentage") is None

    def test_by_category(self):
        """by_category returns only stats in the given category."""
        offense = DomainKnowledge.by_category("offense")
        for sd in offense.values():
            assert sd.category == "offense"
        defense = DomainKnowledge.by_category("defense")
        for sd in defense.values():
            assert sd.category == "defense"

    def test_columns_returns_unique_set(self):
        """columns() should return unique column names."""
        cols = DomainKnowledge.columns()
        assert "passTDs" in cols
        assert "defSacks" in cols
        assert isinstance(cols, set)

    def test_stat_keys_sorted_longest_first(self):
        """Pre-sorted keys should be longest first."""
        keys = DomainKnowledge.STAT_KEYS_SORTED
        for i in range(len(keys) - 1):
            assert len(keys[i]) >= len(keys[i + 1])


# ══════════════════════════════════════════════════════════════════════════════
#  Query Builder Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestQueryBuilder:
    """Verify the fluent Query builder produces correct SQL."""

    def test_basic_select(self):
        sql, params = Query("offensive_stats").select("fullName", "teamName").build()
        assert "SELECT fullName, teamName" in sql
        assert "FROM offensive_stats" in sql
        assert params == ()

    def test_invalid_table_raises(self):
        with pytest.raises(ValueError, match="Invalid table"):
            Query("nonexistent_table")

    def test_all_valid_tables(self):
        """Every valid table should be constructable."""
        valid = [
            "games", "teams", "standings", "offensive_stats",
            "defensive_stats", "team_stats", "trades", "players",
            "player_abilities", "owner_tenure", "player_draft_map",
        ]
        for table in valid:
            q = Query(table)
            sql, _ = q.build()
            assert f"FROM {table}" in sql

    def test_filter_season(self):
        sql, params = Query("offensive_stats").filter(season=6).build()
        assert "seasonIndex = ?" in sql
        assert "6" in params

    def test_filter_stage_regular(self):
        sql, params = Query("offensive_stats").filter(stage="regular").build()
        assert "stageIndex = ?" in sql
        assert "1" in params

    def test_filter_stage_playoffs(self):
        sql, params = Query("offensive_stats").filter(stage="playoffs").build()
        assert "2" in params

    def test_filter_team(self):
        sql, params = Query("players").filter(team="Lions").build()
        assert "teamName = ?" in sql
        assert "Lions" in params

    def test_filter_user_games_table(self):
        """User filter on games table should use homeUser OR awayUser."""
        sql, params = Query("games").filter(user="TheWitt").build()
        assert "homeUser = ?" in sql
        assert "awayUser = ?" in sql
        assert params.count("TheWitt") == 2

    def test_filter_user_non_games_table(self):
        """User filter on non-games tables should use subquery."""
        sql, params = Query("offensive_stats").filter(user="TheWitt").build()
        assert "SELECT teamName FROM teams" in sql

    def test_filter_status(self):
        sql, params = Query("games").filter(status=True).build()
        assert "status IN ('2','3')" in sql

    def test_where_clause(self):
        sql, params = Query("players").where("pos = ?", "QB").build()
        assert "pos = ?" in sql
        assert "QB" in params

    def test_multiple_where_clauses(self):
        sql, params = (
            Query("games")
            .where("homeUser = ?", "A")
            .where("awayUser = ?", "B")
            .build()
        )
        assert sql.count("AND") >= 1
        assert params == ("A", "B")

    def test_aggregate_sum_with_cast(self):
        sql, params = Query("offensive_stats").aggregate(passYds="SUM").build()
        assert "SUM(CAST(passYds AS INTEGER)) AS passYds" in sql

    def test_aggregate_avg_with_cast_real(self):
        """passerRating uses AVG and should CAST to REAL."""
        sql, params = Query("offensive_stats").aggregate(passerRating="AVG").build()
        assert "AVG(CAST(passerRating AS REAL)) AS passerRating" in sql

    def test_invalid_aggregation_raises(self):
        with pytest.raises(ValueError, match="Invalid aggregation"):
            Query("offensive_stats").aggregate(passYds="MEDIAN").build()

    def test_group_by(self):
        sql, _ = Query("offensive_stats").group_by("fullName", "teamName").build()
        assert "GROUP BY fullName, teamName" in sql

    def test_having(self):
        sql, params = Query("offensive_stats").having("COUNT(*) >= ?", 4).build()
        assert "HAVING COUNT(*) >= ?" in sql
        assert 4 in params

    def test_sort_by_desc(self):
        sql, _ = Query("offensive_stats").sort_by("passYds", "DESC").build()
        assert "ORDER BY CAST(passYds AS INTEGER) DESC" in sql

    def test_sort_by_asc(self):
        sql, _ = Query("offensive_stats").sort_by("passYds", "ASC").build()
        assert "ORDER BY CAST(passYds AS INTEGER) ASC" in sql

    def test_sort_by_best_offense(self):
        """'best' on offensive stats = DESC (more is better)."""
        sql, _ = (
            Query("offensive_stats")
            .aggregate(passYds="SUM")
            .sort_by("passYds", "best")
            .build()
        )
        assert "ORDER BY passYds DESC" in sql

    def test_sort_by_best_team_defense_inverted(self):
        """'best' on team yards allowed = ASC (fewer is better)."""
        sql, _ = (
            Query("team_stats")
            .aggregate(defTotalYds="SUM")
            .sort_by("defTotalYds", "best")
            .build()
        )
        assert "ORDER BY defTotalYds ASC" in sql

    def test_sort_by_best_individual_defense_not_inverted(self):
        """'best' on individual sacks = DESC (more is better)."""
        sql, _ = (
            Query("defensive_stats")
            .aggregate(defSacks="SUM")
            .sort_by("defSacks", "best")
            .build()
        )
        assert "ORDER BY defSacks DESC" in sql

    def test_sort_by_worst_with_efficiency_alt(self):
        """'worst' on passYds should switch to passerRating AVG + HAVING."""
        sql, _ = (
            Query("offensive_stats")
            .aggregate(passYds="SUM")
            .group_by("fullName")
            .sort_by("passYds", "worst")
            .build()
        )
        # Should have switched to passerRating
        assert "passerRating" in sql
        assert "AVG" in sql
        assert "COUNT(*) >= 4" in sql

    def test_sort_by_worst_no_efficiency_alt(self):
        """'worst' on a stat without efficiency_alt should just invert direction."""
        sql, _ = (
            Query("offensive_stats")
            .aggregate(rushYds="SUM")
            .sort_by("rushYds", "worst")
            .build()
        )
        assert "ORDER BY rushYds ASC" in sql

    def test_limit(self):
        sql, _ = Query("offensive_stats").limit(10).build()
        assert "LIMIT 10" in sql

    def test_limit_zero_raises(self):
        with pytest.raises(ValueError, match="Limit must be >= 1"):
            Query("offensive_stats").limit(0)

    def test_pos_filter(self):
        sql, params = Query("offensive_stats").pos("QB").build()
        assert "pos = ?" in sql
        assert "QB" in params

    def test_read_only_select_only(self):
        """build() should always produce SELECT statements."""
        sql, _ = Query("games").build()
        assert sql.strip().startswith("SELECT")

    def test_full_chain(self):
        """Full fluent chain should produce valid SQL."""
        sql, params = (
            Query("offensive_stats")
            .select("fullName", "teamName")
            .filter(season=6, stage="regular")
            .pos("QB")
            .aggregate(passYds="SUM", passTDs="SUM")
            .group_by("fullName", "teamName")
            .having("COUNT(*) >= ?", 4)
            .sort_by("passYds", "best")
            .limit(10)
            .build()
        )
        assert "SELECT fullName, teamName" in sql
        assert "SUM(CAST(passYds AS INTEGER)) AS passYds" in sql
        assert "SUM(CAST(passTDs AS INTEGER)) AS passTDs" in sql
        assert "FROM offensive_stats" in sql
        assert "seasonIndex = ?" in sql
        assert "stageIndex = ?" in sql
        assert "pos = ?" in sql
        assert "GROUP BY fullName, teamName" in sql
        assert "HAVING COUNT(*) >= ?" in sql
        assert "ORDER BY passYds DESC" in sql
        assert "LIMIT 10" in sql
        assert "6" in params
        assert "1" in params
        assert "QB" in params
        assert 4 in params

    def test_empty_select_defaults_to_star(self):
        sql, _ = Query("games").build()
        assert "SELECT *" in sql

    def test_aggregate_alias_in_order_by(self):
        """When sorting by an aggregated column, use the alias (not CAST)."""
        sql, _ = (
            Query("offensive_stats")
            .aggregate(passYds="SUM")
            .sort_by("passYds", "DESC")
            .build()
        )
        assert "ORDER BY passYds DESC" in sql
        assert "ORDER BY CAST(passYds" not in sql

    def test_non_aggregate_order_by_uses_cast(self):
        """When sorting by a non-aggregated column, use CAST."""
        sql, _ = Query("games").sort_by("homeScore", "DESC").build()
        assert "ORDER BY CAST(homeScore AS INTEGER) DESC" in sql


# ══════════════════════════════════════════════════════════════════════════════
#  Domain Function Tests (Layer 1)
# ══════════════════════════════════════════════════════════════════════════════

class TestDomainFunctions:
    """Test high-level domain functions produce valid SQL."""

    def test_h2h(self):
        sql, params = h2h("TheWitt", "Ronfk")
        assert "winner_user = ?" in sql
        assert "status IN ('2','3')" in sql
        assert "TheWitt" in params
        assert "Ronfk" in params

    def test_h2h_with_season(self):
        sql, params = h2h("TheWitt", "Ronfk", season=5)
        assert "seasonIndex = ?" in sql
        assert "5" in params

    def test_owner_record(self):
        sql, params = owner_record("TheWitt")
        assert "owner_tenure" in sql
        assert "winner_user = ?" in sql
        assert "status IN ('2','3')" in sql
        assert "TheWitt" in params

    def test_owner_record_with_season(self):
        sql, params = owner_record("TheWitt", season=6)
        assert "seasonIndex = ?" in sql

    def test_team_record(self):
        sql, params = team_record("Lions")
        assert "winner_team = ?" in sql
        assert "Lions" in params

    def test_standings(self):
        sql, params = standings_query()
        assert "FROM standings" in sql
        assert "ORDER BY" in sql

    def test_standings_with_division(self):
        sql, params = standings_query(division="NFC North")
        assert "divisionName = ?" in sql
        assert "NFC North" in params

    def test_streak(self):
        sql, params = streak_query("TheWitt")
        assert "LIMIT 20" in sql
        assert "ORDER BY" in sql

    def test_stat_leaders_passing_yards(self):
        sql, params = stat_leaders("passing yards")
        assert "SUM(CAST(passYds AS INTEGER))" in sql
        assert "pos = ?" in sql
        assert "QB" in params
        assert "stageIndex = ?" in sql

    def test_stat_leaders_tackles(self):
        sql, params = stat_leaders("tackles")
        assert "SUM(CAST(defTotalTackles AS INTEGER))" in sql
        assert "FROM defensive_stats" in sql

    def test_stat_leaders_with_season(self):
        sql, params = stat_leaders("passing yards", season=5)
        assert "seasonIndex = ?" in sql

    def test_stat_leaders_best_sort(self):
        sql, _ = stat_leaders("passing yards", sort="best")
        assert "DESC" in sql

    def test_stat_leaders_worst_sort_efficiency_switch(self):
        """'worst' on passing yards should switch to passerRating."""
        sql, _ = stat_leaders("passing yards", sort="worst")
        assert "passerRating" in sql
        assert "AVG" in sql

    def test_stat_leaders_unknown_stat_raises(self):
        with pytest.raises(ValueError, match="Unknown stat"):
            stat_leaders("blitz percentage")

    def test_team_stat_leaders(self):
        sql, params = team_stat_leaders("team total yards")
        assert "FROM team_stats" in sql
        assert "teamName" in sql

    def test_roster(self):
        sql, params = roster_query("Lions")
        assert "FROM players" in sql
        assert "firstName || ' ' || lastName" in sql
        assert "teamName != 'Free Agent'" in sql
        assert "Lions" in params

    def test_roster_with_pos(self):
        sql, params = roster_query("Lions", pos="QB")
        assert "pos = ?" in sql
        assert "QB" in params

    def test_free_agents(self):
        sql, params = free_agents_query()
        assert "isFA = '1'" in sql

    def test_free_agents_with_min_ovr(self):
        sql, params = free_agents_query(min_ovr=80)
        assert "playerBestOvr" in sql
        assert 80 in params

    def test_draft_picks_uses_player_draft_map(self):
        """Draft queries must use player_draft_map, NOT players table."""
        sql, params = draft_picks_query()
        assert "FROM player_draft_map" in sql
        assert "players" not in sql.lower().replace("player_draft_map", "")

    def test_draft_picks_with_team(self):
        sql, params = draft_picks_query(team="Lions")
        assert "drafting_team = ?" in sql

    def test_abilities(self):
        sql, params = abilities_query(team="Lions")
        assert "FROM player_abilities" in sql
        assert "Lions" in params

    def test_trades(self):
        sql, params = trades_query()
        assert "status = 'approved'" in sql

    def test_owner_history(self):
        sql, params = owner_history_query(user="TheWitt")
        assert "FROM owner_tenure" in sql
        assert "userName = ?" in sql

    def test_game_extremes_blowout(self):
        sql, params = game_extremes("blowout")
        assert "margin DESC" in sql
        assert "status IN ('2','3')" in sql

    def test_game_extremes_closest(self):
        sql, _ = game_extremes("closest")
        assert "margin ASC" in sql

    def test_game_extremes_highest_scoring(self):
        sql, _ = game_extremes("highest_scoring")
        assert "total_pts DESC" in sql

    def test_recent_games(self):
        sql, params = recent_games_query("TheWitt")
        assert "homeUser = ?" in sql
        assert "ORDER BY" in sql

    def test_recent_games_with_opponent(self):
        sql, params = recent_games_query("TheWitt", opponent="Ronfk")
        assert params.count("Ronfk") == 2

    # ── Cross-season functions ────────────────────────────────────────────

    def test_compare_seasons(self):
        sql, params = compare_seasons("passing yards", "TheWitt", 5, 6)
        assert "seasonIndex IN (?, ?)" in sql
        assert "5" in params
        assert "6" in params

    def test_improvement_leaders(self):
        sql, params = improvement_leaders("passing yards", 5, 6)
        assert "improvement" in sql
        assert "HAVING COUNT(*) >= 4" in sql

    def test_career_trajectory(self):
        sql, params = career_trajectory("TheWitt", "passing yards")
        assert "owner_tenure" in sql
        assert "TheWitt" in params


# ══════════════════════════════════════════════════════════════════════════════
#  Utility Function Tests (Layer 3)
# ══════════════════════════════════════════════════════════════════════════════

class TestUtilities:

    def test_compare_datasets_delta(self):
        d1 = [{"name": "A", "val": 10}]
        d2 = [{"name": "A", "val": 15}]
        result = compare_datasets(d1, d2, key="name", metric="delta")
        assert len(result) == 1
        assert result[0]["val_delta"] == 5

    def test_compare_datasets_pct_change(self):
        d1 = [{"name": "A", "val": 100}]
        d2 = [{"name": "A", "val": 150}]
        result = compare_datasets(d1, d2, key="name", metric="pct_change")
        assert result[0]["val_pct"] == 50.0

    def test_summarize_empty(self):
        assert summarize([]) == {"rows": 0}

    def test_summarize_numeric(self):
        data = [{"pts": 10}, {"pts": 20}, {"pts": 30}]
        result = summarize(data)
        assert result["rows"] == 3
        assert result["pts"]["min"] == 10
        assert result["pts"]["max"] == 30
        assert result["pts"]["avg"] == 20.0

    def test_summarize_string_numbers(self):
        """Summarize should handle string-encoded numbers."""
        data = [{"score": "100"}, {"score": "200"}]
        result = summarize(data)
        assert "score" in result
        assert result["score"]["sum"] == 300.0


# ══════════════════════════════════════════════════════════════════════════════
#  SQL Safety Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSQLSafety:
    """Verify SQL injection prevention and read-only enforcement."""

    def test_parameterized_no_string_interpolation(self):
        """Filters should use ? params, never string interpolation."""
        sql, params = (
            Query("games")
            .filter(season=6, user="TheWitt")
            .build()
        )
        assert "TheWitt" not in sql  # Should be in params, not SQL
        assert "TheWitt" in params

    def test_all_stat_definitions_produce_valid_sql(self):
        """Every stat in DomainKnowledge should produce parseable SQL."""
        for key, sd in DomainKnowledge.STATS.items():
            q = Query(sd.table).aggregate(**{sd.column: sd.agg}).group_by("fullName")
            sql, params = q.build()
            assert sql.startswith("SELECT"), f"Invalid SQL for {key}"
            assert "FROM " in sql
            assert "CAST(" in sql, f"Missing CAST for {key}"

    def test_cast_wrapping_on_all_aggregates(self):
        """Every aggregate must be CAST-wrapped."""
        for key, sd in DomainKnowledge.STATS.items():
            q = Query(sd.table).aggregate(**{sd.column: sd.agg})
            sql, _ = q.build()
            expected_cast = f"CAST({sd.column} AS {sd.cast_type})"
            assert expected_cast in sql, f"Missing CAST for {key}: {sql}"
