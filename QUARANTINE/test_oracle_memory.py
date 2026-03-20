# tests/test_oracle_memory.py
"""Tests for Oracle v3 permanent memory schema."""
import sqlite3
import pytest
from pathlib import Path


def test_init_memory_tables(tmp_path):
    """Memory tables are created correctly."""
    db_path = tmp_path / "test.db"
    from oracle_memory import init_memory_tables
    init_memory_tables(str(db_path))

    conn = sqlite3.connect(str(db_path))
    # Check conversation_memory exists
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_memory'")
    assert cur.fetchone() is not None

    # Check oracle_query_log exists
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='oracle_query_log'")
    assert cur.fetchone() is not None

    # Check FTS5 virtual table exists
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='conversation_memory_fts'")
    assert cur.fetchone() is not None

    conn.close()


def test_store_and_retrieve_memory(tmp_path):
    """Can store and retrieve a conversation turn."""
    db_path = tmp_path / "test.db"
    from oracle_memory import init_memory_tables, store_memory, get_recent_turns
    init_memory_tables(str(db_path))

    store_memory(
        db_path=str(db_path),
        discord_id=12345,
        message_id=99999,
        question="who has the most wins?",
        sql_query="SELECT winner_user, COUNT(*) ...",
        answer="TheWitt leads with 67 wins.",
        tier=1,
        intent="leaderboard",
        entities='{"users": ["TheWitt"]}',
    )

    turns = get_recent_turns(str(db_path), discord_id=12345, limit=5)
    assert len(turns) == 1
    assert turns[0]["question"] == "who has the most wins?"
    assert turns[0]["answer"] == "TheWitt leads with 67 wins."


def test_fts5_search(tmp_path):
    """FTS5 keyword search finds relevant conversations."""
    db_path = tmp_path / "test.db"
    from oracle_memory import init_memory_tables, store_memory, search_memory_fts
    init_memory_tables(str(db_path))

    store_memory(str(db_path), 12345, None, "who has the best defense?",
                 None, "Bears lead with 267 yards allowed.", 1, "team_stats", None)
    store_memory(str(db_path), 12345, None, "top passers this season",
                 None, "Mahomes leads with 4200 yards.", 1, "leaderboard", None)

    results = search_memory_fts(str(db_path), discord_id=12345, query="defense yards")
    assert len(results) >= 1
    assert "defense" in results[0]["question"].lower()
