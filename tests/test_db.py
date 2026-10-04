import duckdb

from token_dashboard.db import Database


def test_existing_database_watermarks_gain_file_identity(tmp_path):
    path = str(tmp_path / "existing.duckdb")
    with duckdb.connect(path) as connection:
        connection.execute("""
            CREATE TABLE ingest_state (
                source_file VARCHAR PRIMARY KEY, last_offset BIGINT,
                last_mtime DOUBLE, last_size BIGINT, rows BIGINT, updated_at TIMESTAMPTZ
            )
        """)
        connection.execute(
            "INSERT INTO ingest_state VALUES ('old.jsonl', 123, 1, 123, 2, now())"
        )
    db = Database(path)
    try:
        assert db.query(
            "SELECT last_offset, rows, file_identity FROM ingest_state"
        ) == [(123, 2, None)]
    finally:
        db.close()
    # The migration is safe on subsequent starts too.
    db = Database(path)
    db.close()
